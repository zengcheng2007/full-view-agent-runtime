from __future__ import annotations

import asyncio
from collections import Counter

import pytest

from full_view_agent.application.analysis_graph import (
    AnalysisGraphPreparation,
    AnalysisReduction,
    AnalysisRunOutcome,
    AnalysisStepCheckpoint,
)
from full_view_agent.application.errors import (
    ReauthenticationRequired,
    ResourceNotFound,
    RunStateConflict,
)
from full_view_agent.domain.models import AuthContext
from full_view_agent.infrastructure.checkpoint_mapping_store import (
    InMemoryCheckpointMappingStore,
)
from full_view_agent.infrastructure.langgraph_analysis_orchestrator import (
    LangGraphAnalysisOrchestrator,
)
from full_view_agent.infrastructure.langgraph_checkpoint import InMemoryCheckpointManager

from .test_harness import auth_context


class _Lifecycle:
    def __init__(self) -> None:
        self.wait_calls = 0
        self.waiting = False
        self.consumed = False
        self.input_request_id = "inreq-analysis"
        self.run_state_version = 2

    async def wait_for_reauthentication(self, **_kwargs):
        if not self.waiting:
            self.wait_calls += 1
            self.waiting = True
        return object(), object()

    async def resume_from_input(
        self, *, input_request_id: str, run_state_version: int, **_kwargs
    ):
        if input_request_id != self.input_request_id or (
            run_state_version != self.run_state_version
        ):
            raise RunStateConflict("stale analysis resume input")
        if self.consumed:
            return object()
        if (
            not self.waiting
            or run_state_version != self.run_state_version
        ):
            raise RunStateConflict("stale analysis resume input")
        self.waiting = False
        self.consumed = True
        return object()


class _StepwiseExecution:
    def __init__(self, *, interrupt_once: bool = False, crash_once: bool = False) -> None:
        self.interrupt_once = interrupt_once
        self.crash_once = crash_once
        self.attempts: Counter[str] = Counter()
        self.upstream_calls: Counter[str] = Counter()
        self.persisted: dict[str, AnalysisStepCheckpoint] = {}

    async def prepare(self, **_kwargs) -> AnalysisGraphPreparation:
        return AnalysisGraphPreparation(
            expected_step_count=3,
            max_parallel=2,
            max_tool_calls=3,
            total_timeout_ms=30_000,
        )

    async def reduce(
        self,
        *,
        completed: tuple[AnalysisStepCheckpoint, ...],
        tool_call_count: int,
        deadline_exceeded: bool,
        **_kwargs,
    ) -> AnalysisReduction:
        done = {item.step_id for item in completed}
        remaining = [item for item in ("population", "housing", "event") if item not in done]
        if deadline_exceeded or tool_call_count >= 3:
            return AnalysisReduction(
                additions=tuple(
                    AnalysisStepCheckpoint(
                        step_id=item,
                        status="timeout" if deadline_exceeded else "skipped",
                        reason_code=(
                            "TOTAL_TIMEOUT_EXCEEDED"
                            if deadline_exceeded
                            else "TOOL_CALL_BUDGET_EXCEEDED"
                        ),
                        tool_call_consumed=False,
                    )
                    for item in remaining
                ),
                terminal=True,
            )
        if not remaining:
            return AnalysisReduction(terminal=True)
        if not {"population", "housing"} <= done:
            ready = tuple(
                item for item in ("population", "housing") if item not in done
            )
        else:
            ready = ("event",)
        return AnalysisReduction(ready_step_ids=ready, terminal=False)

    async def execute_step(
        self,
        *,
        analysis_run_id: str,
        step_id: str,
        auth_context: AuthContext,
        **_kwargs,
    ) -> AnalysisStepCheckpoint:
        assert analysis_run_id == auth_context.run_id
        self.attempts[step_id] += 1
        if self.interrupt_once and step_id == "population" and self.attempts[step_id] == 1:
            raise ReauthenticationRequired("refresh required")
        existing = self.persisted.get(step_id)
        if existing is not None:
            return existing.model_copy(update={"tool_call_consumed": False})
        self.upstream_calls[step_id] += 1
        result = AnalysisStepCheckpoint(
            step_id=step_id,
            status="success",
            reason_code="STEP_SUCCEEDED",
            result_id=f"res-{step_id}",
            evidence_ids=(f"evd-{step_id}",),
        )
        self.persisted[step_id] = result
        if self.crash_once and step_id == "population":
            self.crash_once = False
            raise RuntimeError("crash after durable result write")
        return result

    async def finalize(
        self,
        *,
        analysis_run_id: str,
        plan_id: str,
        request_id: str,
        completed: tuple[AnalysisStepCheckpoint, ...],
        **_kwargs,
    ) -> AnalysisRunOutcome:
        statuses = {item.status for item in completed}
        status = "completed" if statuses == {"success"} else "partial"
        return AnalysisRunOutcome(
            analysis_run_id=analysis_run_id,
            plan_id=plan_id,
            request_id=request_id,
            status=status,
            reason_code=f"ANALYSIS_{status.upper()}",
            report_result_id="res-analysis-report",
        )


def _current_auth() -> AuthContext:
    return auth_context().model_copy(update={"run_id": "analysis-run-01"})


def _orchestrator(
    execution: _StepwiseExecution,
    *,
    lifecycle: _Lifecycle | None = None,
    checkpoints: InMemoryCheckpointManager | None = None,
    mappings: InMemoryCheckpointMappingStore | None = None,
) -> LangGraphAnalysisOrchestrator:
    return LangGraphAnalysisOrchestrator(
        execution=execution,
        lifecycle=lifecycle or _Lifecycle(),
        checkpoint_manager=checkpoints,
        checkpoint_mappings=mappings,
    )


async def _run(
    orchestrator: LangGraphAnalysisOrchestrator,
    *,
    plan_id: str = "plan-01",
    request_id: str = "request-01",
):
    return await orchestrator.run(
        user_id="user-01",
        session_id="session-01",
        analysis_run_id="analysis-run-01",
        plan_id=plan_id,
        request_id=request_id,
        auth_context=_current_auth(),
    )


async def _resume(
    orchestrator: LangGraphAnalysisOrchestrator,
    *,
    plan_id: str = "plan-01",
    auth: AuthContext | None = None,
):
    return await orchestrator.resume(
        user_id="user-01",
        session_id="session-01",
        analysis_run_id="analysis-run-01",
        plan_id=plan_id,
        request_id="request-01",
        input_request_id="inreq-analysis",
        run_state_version=2,
        auth_context=auth or _current_auth(),
    )


@pytest.mark.asyncio
async def test_analysis_graph_executes_ready_waves_and_returns_report_reference() -> None:
    execution = _StepwiseExecution()
    outcome = await _run(_orchestrator(execution))
    assert outcome.report_result_id == "res-analysis-report"
    assert execution.upstream_calls == Counter(
        {"population": 1, "housing": 1, "event": 1}
    )


@pytest.mark.asyncio
async def test_checkpoint_replay_rejects_changed_plan_or_request_identity() -> None:
    orchestrator = _orchestrator(_StepwiseExecution())
    await _run(orchestrator)
    with pytest.raises(RunStateConflict, match="invocation identity changed"):
        await _run(orchestrator, plan_id="plan-other")
    with pytest.raises(RunStateConflict, match="invocation identity changed"):
        await _run(orchestrator, request_id="request-other")


@pytest.mark.asyncio
async def test_reauthentication_requires_explicit_ledger_gated_resume() -> None:
    execution = _StepwiseExecution(interrupt_once=True)
    lifecycle = _Lifecycle()
    orchestrator = _orchestrator(execution, lifecycle=lifecycle)

    with pytest.raises(ReauthenticationRequired):
        await _run(orchestrator)
    assert lifecycle.wait_calls == 1
    with pytest.raises(ReauthenticationRequired, match="controlled"):
        await _run(orchestrator)
    assert lifecycle.wait_calls == 1

    outcome = await _resume(orchestrator)
    assert outcome.status == "completed"
    assert execution.upstream_calls == Counter(
        {"population": 1, "housing": 1, "event": 1}
    )


@pytest.mark.asyncio
async def test_resume_preflight_does_not_consume_input_for_changed_plan() -> None:
    lifecycle = _Lifecycle()
    orchestrator = _orchestrator(
        _StepwiseExecution(interrupt_once=True), lifecycle=lifecycle
    )
    with pytest.raises(ReauthenticationRequired):
        await _run(orchestrator)

    with pytest.raises(RunStateConflict, match="invocation identity changed"):
        await _resume(orchestrator, plan_id="plan-other")

    assert lifecycle.waiting is True
    assert lifecycle.consumed is False


@pytest.mark.asyncio
async def test_checkpoint_replay_rejects_same_user_from_another_tenant() -> None:
    orchestrator = _orchestrator(_StepwiseExecution())
    await _run(orchestrator)
    other_principal = _current_auth().principal.model_copy(
        update={"tenant_id": "tenant-other"}
    )
    other_auth = _current_auth().model_copy(update={"principal": other_principal})

    with pytest.raises(ResourceNotFound):
        await orchestrator.run(
            user_id="user-01",
            session_id="session-01",
            analysis_run_id="analysis-run-01",
            plan_id="plan-01",
            request_id="request-01",
            auth_context=other_auth,
        )


@pytest.mark.asyncio
async def test_analysis_graph_recovery_relies_on_durable_step_idempotency() -> None:
    execution = _StepwiseExecution(crash_once=True)
    orchestrator = _orchestrator(execution)
    with pytest.raises(RuntimeError, match="crash after durable result write"):
        await _run(orchestrator)
    outcome = await _run(orchestrator)
    assert outcome.status == "completed"
    assert execution.attempts["population"] == 2
    assert execution.upstream_calls["population"] == 1


@pytest.mark.asyncio
async def test_concurrent_same_run_is_serialized_before_graph_execution() -> None:
    execution = _StepwiseExecution()
    orchestrator = _orchestrator(execution)
    first, second = await asyncio.gather(_run(orchestrator), _run(orchestrator))
    assert first == second
    assert execution.upstream_calls == Counter(
        {"population": 1, "housing": 1, "event": 1}
    )


@pytest.mark.asyncio
async def test_analysis_invocation_must_match_live_auth_context() -> None:
    orchestrator = _orchestrator(_StepwiseExecution())
    with pytest.raises(RunStateConflict, match="does not match auth context"):
        await orchestrator.run(
            user_id="user-01",
            session_id="session-01",
            analysis_run_id="another-run",
            plan_id="plan-01",
            request_id="request-01",
            auth_context=_current_auth(),
        )


@pytest.mark.asyncio
async def test_checkpoint_and_pending_writes_exclude_sensitive_runtime_state() -> None:
    execution = _StepwiseExecution()
    checkpoints = InMemoryCheckpointManager()
    await _run(_orchestrator(execution, checkpoints=checkpoints))
    async with checkpoints.saver() as saver:
        snapshots = [
            item
            async for item in saver.alist(
                {
                    "configurable": {
                        "thread_id": "fva:run:analysis-run-01",
                        "checkpoint_ns": "",
                    }
                }
            )
        ]
    serialized = repr(snapshots)
    for secret in (
        "cred-01",
        "governance.population.aggregate.read",
        "semantic_query",
        "adapter://",
    ):
        assert secret not in serialized
