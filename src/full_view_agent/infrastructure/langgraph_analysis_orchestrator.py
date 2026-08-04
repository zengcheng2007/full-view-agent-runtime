"""Checkpointed LangGraph adapter for dedicated analysis runs."""

import operator
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, Send, interrupt

from full_view_agent.application.analysis_graph import (
    ANALYSIS_GRAPH_STATE_VERSION,
    AnalysisGraphExecutionPort,
    AnalysisRunLeaseManager,
    AnalysisRunLifecycle,
    AnalysisRunOutcome,
    AnalysisStepCheckpoint,
)
from full_view_agent.application.checkpoint_mapping import CheckpointMappingStore
from full_view_agent.application.errors import ReauthenticationRequired, RunStateConflict
from full_view_agent.application.fingerprints import canonical_fingerprint
from full_view_agent.domain.models import AuthContext
from full_view_agent.infrastructure.analysis_run_lease import (
    InMemoryAnalysisRunLeaseManager,
)
from full_view_agent.infrastructure.checkpoint_mapping_store import (
    InMemoryCheckpointMappingStore,
)
from full_view_agent.infrastructure.langgraph_checkpoint import (
    InMemoryCheckpointManager,
    LangGraphCheckpointManager,
)

_MAX_ANALYSIS_STEPS = 64


class _AnalysisState(TypedDict):
    schema_version: Literal["1.0"]
    invocation_fingerprint: str
    analysis_run_id: str
    plan_id: str
    request_id: str
    deadline_at: datetime | None
    expected_step_count: int
    max_parallel: int
    max_tool_calls: int
    tool_call_count: Annotated[int, operator.add]
    completed: Annotated[list[AnalysisStepCheckpoint], operator.add]
    ready_step_ids: list[str]
    terminal: bool
    phase: Literal["preparing", "reducing", "executing", "finalizing", "done"]
    outcome: AnalysisRunOutcome | None


class _StepDispatch(TypedDict):
    analysis_run_id: str
    plan_id: str
    request_id: str
    step_id: str


class LangGraphAnalysisOrchestrator:
    """Runs a trusted plan with checkpoint identity and explicit resume gates.

    ``AuthContext`` is live invocation state and is never checkpointed. A
    process-local lease prevents duplicate execution in development; production
    composition must inject one shared orchestrator per worker and is completed
    by the persistent execution adapter/lease task before this path is enabled.
    """

    def __init__(
        self,
        *,
        execution: AnalysisGraphExecutionPort,
        lifecycle: AnalysisRunLifecycle,
        lease_manager: AnalysisRunLeaseManager | None = None,
        checkpoint_manager: LangGraphCheckpointManager | None = None,
        checkpoint_mappings: CheckpointMappingStore | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._execution = execution
        self._lifecycle = lifecycle
        self._lease_manager = lease_manager or InMemoryAnalysisRunLeaseManager()
        self._checkpoint_manager = checkpoint_manager or InMemoryCheckpointManager()
        self._checkpoint_mappings = (
            checkpoint_mappings or InMemoryCheckpointMappingStore()
        )
        self._clock = clock

    async def run(
        self,
        *,
        user_id: str,
        session_id: str,
        analysis_run_id: str,
        plan_id: str,
        request_id: str,
        auth_context: AuthContext,
    ) -> AnalysisRunOutcome:
        """Start or crash-resume a run; never unlock a reauth interrupt."""

        self._validate_live_identity(
            user_id=user_id,
            session_id=session_id,
            analysis_run_id=analysis_run_id,
            auth_context=auth_context,
        )
        lease_id = _tenant_run_key(
            tenant_id=auth_context.principal.tenant_id,
            analysis_run_id=analysis_run_id,
        )
        # Schema setup may run CREATE INDEX CONCURRENTLY. It must finish before
        # a worker enters the per-run advisory lease; otherwise a competing
        # lease waiter can hold the virtual transaction that index setup waits
        # for, creating a cross-worker lock cycle.
        await self._checkpoint_manager.initialize()
        async with self._lease_manager.lease(analysis_run_id=lease_id):
            return await self._invoke(
                user_id=user_id,
                session_id=session_id,
                analysis_run_id=analysis_run_id,
                plan_id=plan_id,
                request_id=request_id,
                auth_context=auth_context,
                resume_input=None,
            )

    async def resume(
        self,
        *,
        user_id: str,
        session_id: str,
        analysis_run_id: str,
        plan_id: str,
        request_id: str,
        input_request_id: str,
        run_state_version: int,
        auth_context: AuthContext,
    ) -> AnalysisRunOutcome:
        """Resume only after the product ledger consumes the exact input gate."""

        self._validate_live_identity(
            user_id=user_id,
            session_id=session_id,
            analysis_run_id=analysis_run_id,
            auth_context=auth_context,
        )
        lease_id = _tenant_run_key(
            tenant_id=auth_context.principal.tenant_id,
            analysis_run_id=analysis_run_id,
        )
        await self._checkpoint_manager.initialize()
        async with self._lease_manager.lease(analysis_run_id=lease_id):
            return await self._invoke(
                user_id=user_id,
                session_id=session_id,
                analysis_run_id=analysis_run_id,
                plan_id=plan_id,
                request_id=request_id,
                auth_context=auth_context,
                resume_input=(input_request_id, run_state_version),
            )

    async def _invoke(
        self,
        *,
        user_id: str,
        session_id: str,
        analysis_run_id: str,
        plan_id: str,
        request_id: str,
        auth_context: AuthContext,
        resume_input: tuple[str, int] | None,
    ) -> AnalysisRunOutcome:
        invocation_fingerprint = _invocation_fingerprint(
            tenant_id=auth_context.principal.tenant_id,
            user_id=user_id,
            session_id=session_id,
            analysis_run_id=analysis_run_id,
            plan_id=plan_id,
            request_id=request_id,
        )

        async def prepare(state: _AnalysisState) -> dict[str, object]:
            prepared = await self._execution.prepare(
                analysis_run_id=state["analysis_run_id"],
                plan_id=state["plan_id"],
                request_id=state["request_id"],
                auth_context=auth_context,
            )
            return {
                "deadline_at": self._clock()
                + timedelta(milliseconds=prepared.total_timeout_ms),
                "expected_step_count": prepared.expected_step_count,
                "max_parallel": prepared.max_parallel,
                "max_tool_calls": prepared.max_tool_calls,
                "phase": "reducing",
            }

        async def reduce_wave(state: _AnalysisState) -> dict[str, object]:
            deadline = state["deadline_at"]
            if deadline is None:
                raise RuntimeError("analysis reduce ran before preparation")
            reduction = await self._execution.reduce(
                analysis_run_id=state["analysis_run_id"],
                plan_id=state["plan_id"],
                request_id=state["request_id"],
                completed=_unique_completed(state["completed"]),
                tool_call_count=state["tool_call_count"],
                deadline_exceeded=self._clock() >= deadline,
                auth_context=auth_context,
            )
            return {
                "completed": list(reduction.additions),
                "ready_step_ids": list(reduction.ready_step_ids),
                "terminal": reduction.terminal,
                "phase": "finalizing" if reduction.terminal else "executing",
            }

        def route_reduction(state: _AnalysisState) -> str | list[Send]:
            if state["terminal"]:
                return "finalize"
            remaining = state["max_tool_calls"] - state["tool_call_count"]
            if remaining <= 0:
                # The execution port must reduce budget exhaustion into
                # synthetic skipped checkpoints on the next wave.
                return "reduce_wave"
            ready = state["ready_step_ids"][: min(state["max_parallel"], remaining)]
            if not ready:
                return "reduce_wave"
            return [
                Send(
                    "execute_step",
                    {
                        "analysis_run_id": state["analysis_run_id"],
                        "plan_id": state["plan_id"],
                        "request_id": state["request_id"],
                        "step_id": step_id,
                    },
                )
                for step_id in ready
            ]

        async def execute_step(state: _StepDispatch) -> dict[str, object]:
            while True:
                try:
                    result = await self._execution.execute_step(
                        analysis_run_id=state["analysis_run_id"],
                        plan_id=state["plan_id"],
                        request_id=state["request_id"],
                        step_id=state["step_id"],
                        auth_context=auth_context,
                    )
                    return {
                        "completed": [result],
                        "tool_call_count": int(result.tool_call_consumed),
                    }
                except ReauthenticationRequired as exc:
                    interrupt(
                        {
                            "kind": "reauth",
                            "reason": "credential_required",
                            "message": str(exc),
                        }
                    )

        async def finalize(state: _AnalysisState) -> dict[str, object]:
            outcome = await self._execution.finalize(
                analysis_run_id=state["analysis_run_id"],
                plan_id=state["plan_id"],
                request_id=state["request_id"],
                completed=_unique_completed(state["completed"]),
                auth_context=auth_context,
            )
            return {"outcome": outcome, "phase": "done"}

        graph = StateGraph(_AnalysisState)
        graph.add_node("prepare_analysis", prepare)
        graph.add_node("reduce_wave", reduce_wave)
        graph.add_node("execute_step", execute_step)
        graph.add_node("finalize", finalize)
        graph.add_edge(START, "prepare_analysis")
        graph.add_edge("prepare_analysis", "reduce_wave")
        graph.add_conditional_edges(
            "reduce_wave", route_reduction, {"finalize": "finalize", "reduce_wave": "reduce_wave"}
        )
        graph.add_edge("execute_step", "reduce_wave")
        graph.add_edge("finalize", END)

        checkpoint_owner = _checkpoint_owner(
            tenant_id=auth_context.principal.tenant_id,
            user_id=user_id,
        )
        mapping = await self._checkpoint_mappings.ensure_mapping(
            user_id=checkpoint_owner,
            run_id=analysis_run_id,
            session_id=session_id,
            graph_kind="analysis",
        )
        config: RunnableConfig = {
            "configurable": {
                "thread_id": mapping.thread_id,
                "checkpoint_ns": mapping.checkpoint_ns,
            },
            "recursion_limit": (4 * _MAX_ANALYSIS_STEPS) + 16,
        }
        async with self._checkpoint_manager.saver() as saver:
            compiled = graph.compile(checkpointer=saver)
            checkpoint = await saver.aget_tuple(config)
            snapshot = await compiled.aget_state(config) if checkpoint else None
            if snapshot is not None:
                _validate_snapshot_identity(snapshot.values, invocation_fingerprint)
            pending_interrupt = bool(
                snapshot is not None and any(task.interrupts for task in snapshot.tasks)
            )
            if pending_interrupt and resume_input is None:
                # Idempotent ledger transition repairs a crash after the graph
                # interrupt was checkpointed but before the input request was
                # durably exposed.
                await self._lifecycle.wait_for_reauthentication(
                    user_id=user_id,
                    run_id=analysis_run_id,
                )
                raise ReauthenticationRequired(
                    "analysis graph is waiting for controlled reauthentication input"
                )
            if resume_input is not None and not pending_interrupt:
                terminal_outcome = (
                    AnalysisRunOutcome.model_validate(snapshot.values.get("outcome"))
                    if snapshot is not None
                    and snapshot.values.get("phase") == "done"
                    and snapshot.values.get("outcome") is not None
                    else None
                )
                if terminal_outcome is None:
                    raise RunStateConflict(
                        "analysis run is not waiting for reauthentication"
                    )
                # A successful response may be lost after the graph reached a
                # durable terminal checkpoint. Validate the exact closed input
                # token through the product ledger before replaying the stored
                # outcome; a different or stale token remains fail-closed.
                input_request_id, run_state_version = resume_input
                await self._lifecycle.resume_from_input(
                    user_id=user_id,
                    run_id=analysis_run_id,
                    input_request_id=input_request_id,
                    run_state_version=run_state_version,
                )
                return terminal_outcome

            graph_input: _AnalysisState | Command[Any] | None
            if resume_input is not None:
                # Identity/pending preflight happens before consuming the
                # ledger input. The ledger operation is itself idempotent, so
                # a crash before Command(resume) can safely retry.
                input_request_id, run_state_version = resume_input
                await self._lifecycle.resume_from_input(
                    user_id=user_id,
                    run_id=analysis_run_id,
                    input_request_id=input_request_id,
                    run_state_version=run_state_version,
                )
                graph_input = Command(resume={"type": "reauthenticated"})
            elif checkpoint is not None:
                graph_input = None
            else:
                graph_input = {
                    "schema_version": ANALYSIS_GRAPH_STATE_VERSION,
                    "invocation_fingerprint": invocation_fingerprint,
                    "analysis_run_id": analysis_run_id,
                    "plan_id": plan_id,
                    "request_id": request_id,
                    "deadline_at": None,
                    "expected_step_count": 0,
                    "max_parallel": 1,
                    "max_tool_calls": 1,
                    "tool_call_count": 0,
                    "completed": [],
                    "ready_step_ids": [],
                    "terminal": False,
                    "phase": "preparing",
                    "outcome": None,
                }
            try:
                final = await compiled.ainvoke(graph_input, config)
            finally:
                latest = await saver.aget_tuple(config)
                if latest is not None:
                    checkpoint_id = latest.config.get("configurable", {}).get(
                        "checkpoint_id"
                    )
                    if checkpoint_id is None:
                        raise RuntimeError("analysis checkpoint is missing checkpoint_id")
                    await self._checkpoint_mappings.record_checkpoint(
                        user_id=checkpoint_owner,
                        run_id=analysis_run_id,
                        checkpoint_id=str(checkpoint_id),
                        expected_version=mapping.version,
                        graph_kind="analysis",
                    )
            current = await compiled.aget_state(config)
            if any(task.interrupts for task in current.tasks):
                await self._lifecycle.wait_for_reauthentication(
                    user_id=user_id,
                    run_id=analysis_run_id,
                )
                raise ReauthenticationRequired(
                    "analysis graph is waiting for reauthentication"
                )
        outcome = final["outcome"]
        if outcome is None:
            raise RuntimeError("analysis graph finalized without an outcome")
        return outcome

    @staticmethod
    def _validate_live_identity(
        *,
        user_id: str,
        session_id: str,
        analysis_run_id: str,
        auth_context: AuthContext,
    ) -> None:
        if (
            auth_context.principal.user_id != user_id
            or auth_context.session_id != session_id
            or auth_context.run_id != analysis_run_id
        ):
            raise RunStateConflict("analysis invocation identity does not match auth context")


def _invocation_fingerprint(
    *,
    user_id: str,
    session_id: str,
    analysis_run_id: str,
    plan_id: str,
    request_id: str,
    tenant_id: str,
) -> str:
    return canonical_fingerprint(
        domain="analysis-graph-invocation:1.0",
        value={
            "tenant_id": tenant_id,
            "user_id": user_id,
            "session_id": session_id,
            "analysis_run_id": analysis_run_id,
            "plan_id": plan_id,
            "request_id": request_id,
        },
    )


def _checkpoint_owner(*, tenant_id: str, user_id: str) -> str:
    return canonical_fingerprint(
        domain="analysis-checkpoint-owner:1.0",
        value={"tenant_id": tenant_id, "user_id": user_id},
    )


def _tenant_run_key(*, tenant_id: str, analysis_run_id: str) -> str:
    return canonical_fingerprint(
        domain="analysis-run-lease:1.0",
        value={"tenant_id": tenant_id, "analysis_run_id": analysis_run_id},
    )


def _validate_snapshot_identity(values: dict[str, object], expected: str) -> None:
    if values.get("schema_version") != ANALYSIS_GRAPH_STATE_VERSION:
        raise RunStateConflict("analysis checkpoint schema version is unsupported")
    if values.get("invocation_fingerprint") != expected:
        raise RunStateConflict("analysis checkpoint invocation identity changed")


def _unique_completed(
    completed: list[AnalysisStepCheckpoint],
) -> tuple[AnalysisStepCheckpoint, ...]:
    unique: dict[str, AnalysisStepCheckpoint] = {}
    for item in completed:
        existing = unique.get(item.step_id)
        if existing is not None and existing != item:
            raise RuntimeError("analysis step checkpoint changed after persistence")
        unique[item.step_id] = item
    return tuple(unique.values())
