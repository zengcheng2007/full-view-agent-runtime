"""Checkpointed LangGraph adapter for dedicated analysis runs."""

import operator
from typing import Annotated, Any, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, Send, interrupt

from full_view_agent.application.analysis_graph import (
    AnalysisGraphExecutionPort,
    AnalysisRunOutcome,
    AnalysisStepCheckpoint,
)
from full_view_agent.application.checkpoint_mapping import CheckpointMappingStore
from full_view_agent.application.errors import ReauthenticationRequired
from full_view_agent.domain.models import AuthContext
from full_view_agent.infrastructure.checkpoint_mapping_store import (
    InMemoryCheckpointMappingStore,
)
from full_view_agent.infrastructure.langgraph_checkpoint import (
    InMemoryCheckpointManager,
    LangGraphCheckpointManager,
)


class _AnalysisState(TypedDict):
    analysis_run_id: str
    plan_id: str
    request_id: str
    expected_step_count: int
    max_parallel: int
    completed: Annotated[list[AnalysisStepCheckpoint], operator.add]
    outcome: AnalysisRunOutcome | None


class _StepDispatch(TypedDict):
    plan_id: str
    request_id: str
    step_id: str


class LangGraphAnalysisOrchestrator:
    """Runs one trusted plan as a dedicated, resumable analysis graph.

    AuthContext is deliberately captured by the live invocation and never put
    in graph state. The checkpoint contains only opaque identities and Result /
    Evidence references, never credentials, plan bodies or result payloads.
    """

    def __init__(
        self,
        *,
        execution: AnalysisGraphExecutionPort,
        checkpoint_manager: LangGraphCheckpointManager | None = None,
        checkpoint_mappings: CheckpointMappingStore | None = None,
    ) -> None:
        self._execution = execution
        self._checkpoint_manager = checkpoint_manager or InMemoryCheckpointManager()
        self._checkpoint_mappings = (
            checkpoint_mappings or InMemoryCheckpointMappingStore()
        )

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
        async def prepare(state: _AnalysisState) -> dict[str, object]:
            prepared = await self._execution.prepare(
                plan_id=state["plan_id"],
                request_id=state["request_id"],
                auth_context=auth_context,
            )
            return {
                "expected_step_count": prepared.expected_step_count,
                "max_parallel": prepared.max_parallel,
            }

        def dispatch(state: _AnalysisState) -> dict[str, object]:
            del state
            return {}

        async def route_dispatch(state: _AnalysisState) -> str | list[Send]:
            unique = _unique_completed(state["completed"])
            if len(unique) >= state["expected_step_count"]:
                return "finalize"
            ready = await self._execution.ready(
                plan_id=state["plan_id"],
                request_id=state["request_id"],
                completed=unique,
                auth_context=auth_context,
            )
            if not ready:
                return "finalize"
            return [
                Send(
                    "execute_step",
                    {
                        "plan_id": state["plan_id"],
                        "request_id": state["request_id"],
                        "step_id": step_id,
                    },
                )
                for step_id in ready[: state["max_parallel"]]
            ]

        async def execute_step(state: _StepDispatch) -> dict[str, object]:
            while True:
                try:
                    result = await self._execution.execute_step(
                        plan_id=state["plan_id"],
                        request_id=state["request_id"],
                        step_id=state["step_id"],
                        auth_context=auth_context,
                    )
                    return {"completed": [result]}
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
            return {"outcome": outcome}

        graph = StateGraph(_AnalysisState)
        graph.add_node("prepare_analysis", prepare)
        graph.add_node("dispatch_ready", dispatch)
        graph.add_node("execute_step", execute_step)
        graph.add_node("finalize", finalize)
        graph.add_edge(START, "prepare_analysis")
        graph.add_edge("prepare_analysis", "dispatch_ready")
        graph.add_conditional_edges(
            "dispatch_ready",
            route_dispatch,
            {"finalize": "finalize"},
        )
        graph.add_edge("execute_step", "dispatch_ready")
        graph.add_edge("finalize", END)

        mapping = await self._checkpoint_mappings.ensure_mapping(
            user_id=user_id,
            run_id=analysis_run_id,
            session_id=session_id,
        )
        config: RunnableConfig = {
            "configurable": {
                "thread_id": mapping.thread_id,
                "checkpoint_ns": mapping.checkpoint_ns,
            },
            "recursion_limit": 256,
        }
        async with self._checkpoint_manager.saver() as saver:
            compiled = graph.compile(checkpointer=saver)
            checkpoint = await saver.aget_tuple(config)
            snapshot = await compiled.aget_state(config) if checkpoint else None
            pending_interrupt = bool(
                snapshot is not None and any(task.interrupts for task in snapshot.tasks)
            )
            graph_input: _AnalysisState | Command[Any] | None
            if pending_interrupt:
                graph_input = Command(resume={"type": "reauthenticated"})
            elif checkpoint is not None:
                graph_input = None
            else:
                graph_input = {
                    "analysis_run_id": analysis_run_id,
                    "plan_id": plan_id,
                    "request_id": request_id,
                    "expected_step_count": 0,
                    "max_parallel": 1,
                    "completed": [],
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
                        user_id=user_id,
                        run_id=analysis_run_id,
                        checkpoint_id=str(checkpoint_id),
                        expected_version=mapping.version,
                    )
            current = await compiled.aget_state(config)
            if any(task.interrupts for task in current.tasks):
                raise ReauthenticationRequired(
                    "analysis graph is waiting for reauthentication"
                )
        outcome = final["outcome"]
        if outcome is None:
            raise RuntimeError("analysis graph finalized without an outcome")
        return outcome


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
