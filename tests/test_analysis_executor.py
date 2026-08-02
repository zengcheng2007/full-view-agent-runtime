"""P1-2 AnalysisPlan 执行器的确定性、安全和预算契约。"""

import asyncio
from collections.abc import Mapping
from typing import Literal

import pytest

from full_view_agent.application.analysis_executor import (
    AnalysisExecutionRejected,
    AnalysisPlanExecutor,
)
from full_view_agent.application.analysis_planner import AnalysisPlanner
from full_view_agent.application.errors import ReauthenticationRequired
from full_view_agent.application.semantic_executor import SemanticToolExecutor
from full_view_agent.application.semantic_wiring import build_semantic_capability_stack
from full_view_agent.application.tool_registry import ToolRegistry
from full_view_agent.domain.analysis_plan import AnalysisPlan, AnalysisStep, PlanBudget
from full_view_agent.domain.models import AuthContext, ToolResult
from full_view_agent.infrastructure.governance_adapter import InMemoryGovernanceAdapter
from full_view_agent.semantic.action_resolver import (
    SEMANTIC_QUERY_TOOL_ID,
    SemanticActionResolver,
)
from full_view_agent.semantic.authorization import SubjectAuthorization
from full_view_agent.semantic.catalog import CapabilityBinding, SemanticCatalog

from .test_analysis_planner import area_request
from .test_policy import population_auth_context


def _full_auth_context() -> AuthContext:
    base = population_auth_context()
    return base.model_copy(
        update={
            "entitlements": [
                "governance.area.read",
                "governance.event.aggregate.read",
                "governance.housing.aggregate.read",
                "governance.population.aggregate.read",
            ],
            "data_scopes": base.data_scopes.model_copy(
                update={
                    "datasets": ["administrative_area", "event", "housing", "population"],
                    "field_policy_set": "governance_analyst_v1",
                }
            ),
        }
    )


class _ControlledSemanticPort(SemanticToolExecutor):
    """仅在调度测试中控制延迟/结果；成功仍走真实语义执行器。"""

    def __init__(
        self,
        inner: SemanticToolExecutor,
        *,
        delays: Mapping[str, float] | None = None,
        statuses: Mapping[str, Literal["failed", "denied"]] | None = None,
        summaries: Mapping[str, str] | None = None,
        exceptions: Mapping[str, Exception] | None = None,
    ) -> None:
        self.inner = inner
        self.delays = dict(delays or {})
        self.statuses: dict[str, Literal["failed", "denied"]] = dict(
            statuses or {}
        )
        self.summaries = dict(summaries or {})
        self.exceptions = dict(exceptions or {})
        self.calls: list[str] = []
        self.active = 0
        self.max_active = 0
        self.entered = asyncio.Event()
        self.cancelled_subjects: list[str] = []

    @property
    def resolver(self) -> SemanticActionResolver:
        return self.inner.resolver

    async def execute(
        self,
        *,
        tool_call_id: str,
        tool_id: str,
        raw_arguments: dict[str, object],
        auth_context: AuthContext,
    ) -> ToolResult:
        assert tool_id == SEMANTIC_QUERY_TOOL_ID
        spec = raw_arguments["spec"]
        assert isinstance(spec, dict)
        subject = str(spec["subject"])
        self.calls.append(subject)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.entered.set()
        try:
            await asyncio.sleep(self.delays.get(subject, 0))
            if subject in self.exceptions:
                raise self.exceptions[subject]
            status = self.statuses.get(subject)
            if status is not None:
                return ToolResult(
                    tool_call_id=tool_call_id,
                    tool_id=SEMANTIC_QUERY_TOOL_ID,
                    tool_version="1.0.0",
                    status=status,
                    summary=self.summaries.get(
                        subject, f"{subject} controlled {status}"
                    ),
                    warnings=[f"{subject.upper()}_{status.upper()}"],
                )
            return await self.inner.execute(
                tool_call_id=tool_call_id,
                tool_id=tool_id,
                raw_arguments=raw_arguments,
                auth_context=auth_context,
            )
        except asyncio.CancelledError:
            self.cancelled_subjects.append(subject)
            raise
        finally:
            self.active -= 1


def _executor(
    *,
    catalog: SemanticCatalog | None = None,
    delays: Mapping[str, float] | None = None,
    statuses: Mapping[str, Literal["failed", "denied"]] | None = None,
    summaries: Mapping[str, str] | None = None,
    exceptions: Mapping[str, Exception] | None = None,
) -> tuple[AnalysisPlanExecutor, _ControlledSemanticPort, SemanticCatalog]:
    effective_catalog = catalog or SemanticCatalog.default()
    stack = build_semantic_capability_stack(
        registry=ToolRegistry.default(),
        adapter=InMemoryGovernanceAdapter(),
        catalog=effective_catalog,
    )
    port = _ControlledSemanticPort(
        stack.executor,
        delays=delays,
        statuses=statuses,
        summaries=summaries,
        exceptions=exceptions,
    )
    return (
        AnalysisPlanExecutor(
            catalog=stack.catalog,
            resolver=stack.resolver,
            semantic_executor=port,
        ),
        port,
        stack.catalog,
    )


def _overview_plan(
    catalog: SemanticCatalog,
    *,
    constraints: PlanBudget | None = None,
) -> AnalysisPlan:
    auth = _full_auth_context()
    return AnalysisPlanner(
        catalog,
        default_budget=constraints or PlanBudget(),
    ).plan(
        area_request("overview"),
        authorization=SubjectAuthorization.from_auth_context(auth),
    )


def _replace_steps(
    plan: AnalysisPlan,
    steps: tuple[AnalysisStep, ...],
    *,
    constraints: PlanBudget | None = None,
) -> AnalysisPlan:
    return AnalysisPlan(
        plan_id=plan.plan_id,
        catalog_version=plan.catalog_version,
        catalog_fingerprint=plan.catalog_fingerprint,
        request_id=plan.request_id,
        goals=plan.goals,
        scope_ref=plan.scope_ref,
        steps=steps,
        omissions=plan.omissions,
        constraints=constraints or plan.constraints,
    )


@pytest.mark.asyncio
async def test_independent_ready_steps_run_with_bounded_parallelism_and_plan_order() -> None:
    executor, port, catalog = _executor(
        delays={"event": 0.06, "housing": 0.01, "population": 0.03}
    )
    plan = _overview_plan(
        catalog,
        constraints=PlanBudget(max_parallel=2, total_timeout_ms=2_000),
    )

    result = await executor.execute(plan, auth_context=_full_auth_context())

    assert result.status == "completed"
    assert result.reason_code == "ANALYSIS_COMPLETED"
    assert result.tool_call_count == 3
    assert 1 < port.max_active <= 2
    assert [step.step_id for step in result.steps] == [
        step.step_id for step in plan.steps
    ]
    assert [step.status for step in result.steps] == ["success"] * 3


@pytest.mark.asyncio
async def test_dependency_waits_for_successful_parent() -> None:
    executor, port, catalog = _executor(delays={"event": 0.03})
    plan = _overview_plan(catalog)
    event, housing, population = plan.steps
    dependent_housing = housing.model_copy(update={"depends_on": (event.step_id,)})
    plan = _replace_steps(plan, (event, dependent_housing, population))

    result = await executor.execute(plan, auth_context=_full_auth_context())

    assert result.status == "completed"
    assert port.calls.index("event") < port.calls.index("housing")


@pytest.mark.asyncio
async def test_failed_subject_does_not_stop_independent_subjects_and_is_partial() -> None:
    executor, port, catalog = _executor(statuses={"housing": "failed"})
    plan = _overview_plan(catalog)

    result = await executor.execute(plan, auth_context=_full_auth_context())

    assert result.status == "partial"
    assert result.reason_code == "ANALYSIS_PARTIAL"
    assert port.calls == ["event", "housing", "population"]
    by_subject = {step.subject: step for step in result.steps}
    assert by_subject["housing"].status == "failed"
    assert by_subject["housing"].reason_code == "HOUSING_FAILED"
    assert by_subject["event"].status == "success"
    assert by_subject["population"].status == "success"


@pytest.mark.asyncio
async def test_failed_dependency_is_skipped_without_tool_call() -> None:
    executor, port, catalog = _executor(statuses={"event": "denied"})
    plan = _overview_plan(catalog)
    event, housing, population = plan.steps
    dependent_housing = housing.model_copy(update={"depends_on": (event.step_id,)})
    plan = _replace_steps(plan, (event, dependent_housing, population))

    result = await executor.execute(plan, auth_context=_full_auth_context())

    assert result.status == "partial"
    assert port.calls == ["event", "population"]
    by_subject = {step.subject: step for step in result.steps}
    assert by_subject["event"].status == "denied"
    assert by_subject["housing"].status == "skipped"
    assert by_subject["housing"].reason_code == "DEPENDENCY_NOT_SUCCESSFUL"


@pytest.mark.asyncio
async def test_per_step_timeout_is_structured_and_other_subjects_complete() -> None:
    executor, _port, catalog = _executor(delays={"event": 0.2})
    plan = _overview_plan(catalog)
    steps = tuple(
        step.model_copy(update={"timeout_ms": 100}) if step.subject == "event" else step
        for step in plan.steps
    )
    plan = _replace_steps(plan, steps)

    result = await executor.execute(plan, auth_context=_full_auth_context())

    assert result.status == "partial"
    by_subject = {step.subject: step for step in result.steps}
    assert by_subject["event"].status == "timeout"
    assert by_subject["event"].reason_code == "STEP_TIMEOUT_EXCEEDED"
    assert by_subject["housing"].status == "success"


@pytest.mark.asyncio
async def test_total_timeout_cancels_inflight_and_marks_unfinished_steps() -> None:
    executor, port, catalog = _executor(
        delays={"event": 0.3, "housing": 0.3, "population": 0.3}
    )
    budget = PlanBudget(max_parallel=2, total_timeout_ms=100)
    plan = _overview_plan(catalog, constraints=budget)
    steps = tuple(step.model_copy(update={"timeout_ms": 100}) for step in plan.steps)
    plan = _replace_steps(plan, steps, constraints=budget)

    result = await executor.execute(plan, auth_context=_full_auth_context())

    assert result.status == "failed"
    assert result.tool_call_count == 2
    assert set(port.cancelled_subjects) == {"event", "housing"}
    assert all(step.status == "timeout" for step in result.steps)
    assert all(step.reason_code == "TOTAL_TIMEOUT_EXCEEDED" for step in result.steps)


@pytest.mark.asyncio
async def test_external_cancellation_propagates_and_cancels_inflight_tasks() -> None:
    executor, port, catalog = _executor(
        delays={"event": 10, "housing": 10, "population": 10}
    )
    plan = _overview_plan(catalog)
    task = asyncio.create_task(
        executor.execute(plan, auth_context=_full_auth_context())
    )
    await asyncio.wait_for(port.entered.wait(), timeout=1)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert port.active == 0
    assert port.cancelled_subjects


@pytest.mark.asyncio
async def test_catalog_version_or_fingerprint_drift_rejects_before_any_call() -> None:
    executor, port, catalog = _executor()
    plan = _overview_plan(catalog)

    for drifted in (
        plan.model_copy(update={"catalog_version": "stale"}),
        plan.model_copy(update={"catalog_fingerprint": "sha256:" + "f" * 64}),
    ):
        with pytest.raises(AnalysisExecutionRejected) as exc_info:
            await executor.execute(drifted, auth_context=_full_auth_context())
        assert exc_info.value.code in {
            "CATALOG_VERSION_MISMATCH",
            "CATALOG_FINGERPRINT_MISMATCH",
        }
    assert port.calls == []


@pytest.mark.asyncio
async def test_binding_drift_rejects_before_any_call() -> None:
    executor, port, catalog = _executor()
    plan = _overview_plan(catalog)
    first = plan.steps[0]
    tampered = first.model_copy(update={"capability_version": "9.9.9"})
    plan = _replace_steps(plan, (tampered, *plan.steps[1:]))

    with pytest.raises(AnalysisExecutionRejected) as exc_info:
        await executor.execute(plan, auth_context=_full_auth_context())

    assert exc_info.value.code == "CAPABILITY_BINDING_MISMATCH"
    assert port.calls == []


@pytest.mark.asyncio
async def test_catalog_binding_adapter_change_is_detected_by_fingerprint() -> None:
    base = SemanticCatalog.default()
    plan = _overview_plan(base)
    altered = SemanticCatalog(
        catalog_version=base.catalog_version,
        supported_spec_versions=base.supported_spec_versions,
        subjects=base.subjects,
        bindings={
            subject: CapabilityBinding(
                capability_id=binding.capability_id,
                capability_version=binding.capability_version,
                adapter_ref=f"adapter://changed/{subject}",
            )
            for subject, binding in base.bindings.items()
        },
    )
    executor, port, _catalog = _executor(catalog=altered)

    with pytest.raises(AnalysisExecutionRejected) as exc_info:
        await executor.execute(plan, auth_context=_full_auth_context())

    assert exc_info.value.code == "CATALOG_FINGERPRINT_MISMATCH"
    assert port.calls == []


@pytest.mark.asyncio
async def test_max_tool_calls_is_enforced_at_runtime() -> None:
    executor, port, catalog = _executor()
    plan = _overview_plan(catalog)
    # model_copy 模拟持久化层绕过 Pydantic 构造校验的污染对象；
    # 执行器仍必须在边界 fail closed。
    plan = plan.model_copy(
        update={"constraints": PlanBudget(max_parallel=2, max_tool_calls=1)}
    )

    result = await executor.execute(plan, auth_context=_full_auth_context())

    assert result.tool_call_count == 1
    assert port.calls == ["event"]
    assert [step.reason_code for step in result.steps[1:]] == [
        "TOOL_CALL_BUDGET_EXCEEDED",
        "TOOL_CALL_BUDGET_EXCEEDED",
    ]


@pytest.mark.asyncio
async def test_long_tool_summary_is_bounded_without_breaking_other_steps() -> None:
    executor, _port, catalog = _executor(
        statuses={"housing": "failed"}, summaries={"housing": "x" * 2_000}
    )
    plan = _overview_plan(catalog)

    result = await executor.execute(plan, auth_context=_full_auth_context())

    assert result.status == "partial"
    housing = next(step for step in result.steps if step.subject == "housing")
    assert housing.status == "failed"
    assert len(housing.detail) == 500


@pytest.mark.asyncio
async def test_reauthentication_control_flow_propagates_and_cancels_peers() -> None:
    executor, port, catalog = _executor(
        delays={"event": 0.01, "housing": 10, "population": 10},
        exceptions={"event": ReauthenticationRequired("credential expired")},
    )
    plan = _overview_plan(catalog)

    with pytest.raises(ReauthenticationRequired, match="credential expired"):
        await executor.execute(plan, auth_context=_full_auth_context())

    assert port.active == 0
    assert set(port.cancelled_subjects) == {"housing", "population"}


def test_executor_rejects_semantic_executor_with_a_different_resolver() -> None:
    catalog = SemanticCatalog.default()
    first = build_semantic_capability_stack(
        registry=ToolRegistry.default(),
        adapter=InMemoryGovernanceAdapter(),
        catalog=catalog,
    )
    second = build_semantic_capability_stack(
        registry=ToolRegistry.default(),
        adapter=InMemoryGovernanceAdapter(),
        catalog=SemanticCatalog.default(),
    )

    with pytest.raises(ValueError, match="same resolver"):
        AnalysisPlanExecutor(
            catalog=first.catalog,
            resolver=first.resolver,
            semantic_executor=second.executor,
        )
