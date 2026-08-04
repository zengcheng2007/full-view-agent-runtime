from __future__ import annotations

from collections import Counter

import pytest

from full_view_agent.application.analysis_graph import (
    AnalysisGraphPreparation,
    AnalysisRunOutcome,
    AnalysisStepCheckpoint,
)
from full_view_agent.application.errors import ReauthenticationRequired
from full_view_agent.domain.models import AuthContext
from full_view_agent.infrastructure.checkpoint_mapping_store import (
    InMemoryCheckpointMappingStore,
)
from full_view_agent.infrastructure.langgraph_analysis_orchestrator import (
    LangGraphAnalysisOrchestrator,
)
from full_view_agent.infrastructure.langgraph_checkpoint import (
    InMemoryCheckpointManager,
)

from .test_harness import auth_context


class _StepwiseExecution:
    def __init__(self, *, interrupt_once: bool = False, crash_once: bool = False) -> None:
        self.interrupt_once = interrupt_once
        self.crash_once = crash_once
        self.attempts: Counter[str] = Counter()
        self.upstream_calls: Counter[str] = Counter()
        self.persisted: dict[str, AnalysisStepCheckpoint] = {}

    async def prepare(self, **_kwargs) -> AnalysisGraphPreparation:
        return AnalysisGraphPreparation(expected_step_count=3, max_parallel=2)

    async def ready(
        self,
        *,
        completed: tuple[AnalysisStepCheckpoint, ...],
        **_kwargs,
    ) -> tuple[str, ...]:
        done = {item.step_id for item in completed}
        if not {"population", "housing"} <= done:
            return tuple(item for item in ("population", "housing") if item not in done)
        return () if "event" in done else ("event",)

    async def execute_step(
        self,
        *,
        step_id: str,
        auth_context: AuthContext,
        **_kwargs,
    ) -> AnalysisStepCheckpoint:
        assert auth_context.credential_ref == "cred-01"
        self.attempts[step_id] += 1
        if self.interrupt_once and step_id == "population" and self.attempts[step_id] == 1:
            raise ReauthenticationRequired("refresh required")
        existing = self.persisted.get(step_id)
        if existing is not None:
            return existing
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
        assert {item.step_id for item in completed} == {
            "population",
            "housing",
            "event",
        }
        return AnalysisRunOutcome(
            analysis_run_id=analysis_run_id,
            plan_id=plan_id,
            request_id=request_id,
            status="completed",
            reason_code="ANALYSIS_COMPLETED",
            report_result_id="res-analysis-report",
        )


def _orchestrator(
    execution: _StepwiseExecution,
    *,
    checkpoints: InMemoryCheckpointManager | None = None,
    mappings: InMemoryCheckpointMappingStore | None = None,
) -> LangGraphAnalysisOrchestrator:
    return LangGraphAnalysisOrchestrator(
        execution=execution,
        checkpoint_manager=checkpoints,
        checkpoint_mappings=mappings,
    )


async def _run(orchestrator: LangGraphAnalysisOrchestrator):
    return await orchestrator.run(
        user_id="user-01",
        session_id="session-01",
        analysis_run_id="analysis-run-01",
        plan_id="plan-01",
        request_id="request-01",
        auth_context=auth_context(),
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
async def test_analysis_graph_resumes_reauthentication_without_repeating_successes() -> None:
    execution = _StepwiseExecution(interrupt_once=True)
    checkpoints = InMemoryCheckpointManager()
    mappings = InMemoryCheckpointMappingStore()
    orchestrator = _orchestrator(
        execution, checkpoints=checkpoints, mappings=mappings
    )

    with pytest.raises(ReauthenticationRequired):
        await _run(orchestrator)
    outcome = await _run(orchestrator)

    assert outcome.status == "completed"
    assert execution.upstream_calls == Counter(
        {"population": 1, "housing": 1, "event": 1}
    )


@pytest.mark.asyncio
async def test_analysis_graph_recovery_relies_on_durable_step_idempotency() -> None:
    execution = _StepwiseExecution(crash_once=True)
    checkpoints = InMemoryCheckpointManager()
    mappings = InMemoryCheckpointMappingStore()
    orchestrator = _orchestrator(
        execution, checkpoints=checkpoints, mappings=mappings
    )

    with pytest.raises(RuntimeError, match="crash after durable result write"):
        await _run(orchestrator)
    outcome = await _run(orchestrator)

    assert outcome.status == "completed"
    assert execution.attempts["population"] == 2
    assert execution.upstream_calls["population"] == 1


@pytest.mark.asyncio
async def test_analysis_checkpoint_does_not_serialize_auth_or_plan_body() -> None:
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
    serialized = repr([item.checkpoint for item in snapshots])
    assert "cred-01" not in serialized
    assert "governance.population.aggregate.read" not in serialized
    assert "semantic_query" not in serialized
    assert "adapter://" not in serialized
