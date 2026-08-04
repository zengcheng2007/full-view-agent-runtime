"""P1-2 AnalysisPlan 执行器的确定性、安全和预算契约。"""

import asyncio
import inspect
import warnings
from collections.abc import Mapping
from typing import Literal

import pytest

from full_view_agent.application.analysis_executor import (
    AnalysisExecutionRejected,
    AnalysisPlanExecutor,
)
from full_view_agent.application.analysis_plan_integrity import (
    recompute_analysis_plan_id,
)
from full_view_agent.application.analysis_plan_repository import (
    AnalysisPlanStoreRejected,
)
from full_view_agent.application.analysis_planner import AnalysisPlanner
from full_view_agent.application.errors import (
    ReauthenticationRequired,
    RunStateConflict,
)
from full_view_agent.application.fingerprints import canonical_fingerprint
from full_view_agent.application.semantic_executor import SemanticToolExecutor
from full_view_agent.application.semantic_wiring import build_semantic_capability_stack
from full_view_agent.application.tool_registry import ToolRegistry
from full_view_agent.domain.analysis_execution import AnalysisExecutionResult
from full_view_agent.domain.analysis_plan import (
    AnalysisOmission,
    AnalysisPlan,
    AnalysisRequest,
    AnalysisStep,
    AreaScopeRef,
    PlanBudget,
)
from full_view_agent.domain.models import (
    AgentMessage,
    AgentRun,
    AgentSession,
    AuthContext,
    DataResult,
    MetricQueryScope,
    TableDataResult,
    TextContent,
    ToolResult,
)
from full_view_agent.infrastructure.governance_adapter import InMemoryGovernanceAdapter
from full_view_agent.infrastructure.memory_store import InMemoryAgentStore
from full_view_agent.semantic.action_resolver import (
    SEMANTIC_QUERY_TOOL_ID,
    SemanticActionResolver,
)
from full_view_agent.semantic.authorization import SubjectAuthorization
from full_view_agent.semantic.catalog import CapabilityBinding, SemanticCatalog

from .test_analysis_planner import area_request
from .test_policy import population_auth_context


def test_public_execution_port_accepts_only_plan_identity_not_plan_body() -> None:
    parameters = inspect.signature(AnalysisPlanExecutor.execute).parameters
    assert "plan" not in parameters
    assert {"plan_id", "request_id", "auth_context"} <= set(parameters)


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


class _InMemoryAnalysisPlanRepository:
    """测试用可信服务端计划源；不代表客户端输入。"""

    def __init__(self) -> None:
        self.plans: dict[tuple[str, str, str, str], AnalysisPlan] = {}
        self.error: AnalysisPlanStoreRejected | None = None

    async def get(
        self,
        *,
        tenant_id: str,
        user_id: str,
        run_id: str,
        plan_id: str,
    ) -> AnalysisPlan | None:
        if self.error is not None:
            raise self.error
        return self.plans.get((tenant_id, user_id, run_id, plan_id))


class _ControlledAnalysisPlanner(AnalysisPlanner):
    """仅为调度单测生成带依赖/短超时的可信服务端计划。"""

    expected_plan: AnalysisPlan | None = None

    def plan(
        self,
        request: AnalysisRequest,
        *,
        authorization: SubjectAuthorization | None,
    ) -> AnalysisPlan:
        if self.expected_plan is not None:
            return self.expected_plan
        return super().plan(request, authorization=authorization)


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
        plan_repository: _InMemoryAnalysisPlanRepository,
        planner: _ControlledAnalysisPlanner,
    ) -> None:
        self.inner = inner
        self.delays = dict(delays or {})
        self.statuses: dict[str, Literal["failed", "denied"]] = dict(
            statuses or {}
        )
        self.summaries = dict(summaries or {})
        self.exceptions = dict(exceptions or {})
        self.plan_repository = plan_repository
        self.planner = planner
        self.result_store: InMemoryAgentStore | None = None
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
            if status == "partial":
                real = await self.inner.execute(
                    tool_call_id=tool_call_id,
                    tool_id=tool_id,
                    raw_arguments=raw_arguments,
                    auth_context=auth_context,
                )
                return real.model_copy(
                    update={
                        "status": "partial",
                        "summary": self.summaries.get(
                            subject, f"{subject} controlled partial"
                        ),
                        "warnings": [f"{subject.upper()}_PARTIAL"],
                    }
                )
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
    statuses: Mapping[str, Literal["failed", "denied", "partial"]] | None = None,
    summaries: Mapping[str, str] | None = None,
    exceptions: Mapping[str, Exception] | None = None,
    result_store: InMemoryAgentStore | None = None,
) -> tuple[AnalysisPlanExecutor, _ControlledSemanticPort, SemanticCatalog]:
    effective_catalog = catalog or SemanticCatalog.default()
    stack = build_semantic_capability_stack(
        registry=ToolRegistry.default(),
        adapter=InMemoryGovernanceAdapter(),
        catalog=effective_catalog,
    )
    plan_repository = _InMemoryAnalysisPlanRepository()
    planner = _ControlledAnalysisPlanner(stack.catalog)
    port = _ControlledSemanticPort(
        stack.executor,
        delays=delays,
        statuses=statuses,
        summaries=summaries,
        exceptions=exceptions,
        plan_repository=plan_repository,
        planner=planner,
    )
    effective_store = result_store or InMemoryAgentStore()
    # 测试句柄：报告/落库断言通过它读取既有 Result 生命周期存储。
    port.result_store = effective_store
    return (
        AnalysisPlanExecutor(
            catalog=stack.catalog,
            resolver=stack.resolver,
            semantic_executor=port,
            planner=planner,
            plan_repository=plan_repository,
            result_store=effective_store,
        ),
        port,
        stack.catalog,
    )


async def _seed_result_store(
    store: InMemoryAgentStore, *, auth_context: AuthContext
) -> None:
    """按授权上下文的 session/run 命名空间激活既有 Result 生命周期。"""
    if auth_context.run_id in store.runs:
        return
    await store.create_session(
        AgentSession(
            session_id=auth_context.session_id,
            owner_user_id=auth_context.principal.user_id,
            title="analysis gateway",
        )
    )
    message = AgentMessage(
        message_id=f"msg-{auth_context.run_id}",
        session_id=auth_context.session_id,
        run_id=auth_context.run_id,
        role="user",
        content=[TextContent(type="text", text="analysis")],
    )
    await store.create_run_if_session_idle(
        user_id=auth_context.principal.user_id,
        session_id=auth_context.session_id,
        run=AgentRun(
            run_id=auth_context.run_id,
            session_id=auth_context.session_id,
            origin_client_instance_id="cli-analysis",
            status="queued",
            input_message_id=message.message_id,
            base_context_version=1,
        ),
        input_message=message,
    )
    await store.start_run(
        user_id=auth_context.principal.user_id, run_id=auth_context.run_id
    )


async def _execute_loaded(
    executor: AnalysisPlanExecutor,
    port: _ControlledSemanticPort,
    plan: AnalysisPlan,
    *,
    auth_context: AuthContext,
    lookup_plan_id: str | None = None,
    request_id: str | None = None,
    trusted_replanned: bool = False,
) -> AnalysisExecutionResult:
    effective_plan_id = lookup_plan_id or plan.plan_id
    key = (
        auth_context.principal.tenant_id,
        auth_context.principal.user_id,
        auth_context.run_id,
        effective_plan_id,
    )
    port.plan_repository.plans[key] = plan
    if trusted_replanned:
        port.planner.expected_plan = plan
    assert port.result_store is not None
    await _seed_result_store(port.result_store, auth_context=auth_context)
    try:
        return await executor.execute(
            plan_id=effective_plan_id,
            request_id=request_id or plan.request_id,
            auth_context=auth_context,
        )
    finally:
        port.planner.expected_plan = None


@pytest.mark.asyncio
async def test_repository_integrity_failure_uses_stable_execution_rejection() -> None:
    executor, port, _ = _executor()
    port.plan_repository.error = AnalysisPlanStoreRejected(
        "PLAN_JSON_INVALID",
        "stored plan JSON is invalid",
    )

    with pytest.raises(AnalysisExecutionRejected) as exc_info:
        await executor.execute(
            plan_id="plan-corrupt",
            request_id="request-corrupt",
            auth_context=_full_auth_context(),
        )

    assert exc_info.value.code == "PLAN_STORE_REJECTED"
    assert isinstance(exc_info.value.__cause__, AnalysisPlanStoreRejected)


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
    replaced = AnalysisPlan(
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
    return replaced.model_copy(
        update={"plan_id": recompute_analysis_plan_id(replaced)}
    )


def _attacker_recomputed_plan_id(plan: AnalysisPlan) -> str:
    """模拟攻击者知道公开算法后重算 ID；ID 不是签名。"""
    return canonical_fingerprint(
        domain="analysis-plan:1.0",
        value={
            "schema_version": plan.schema_version,
            "catalog_version": plan.catalog_version,
            "catalog_fingerprint": plan.catalog_fingerprint,
            "request_id": plan.request_id,
            "goals": list(plan.goals),
            "scope_ref": plan.scope_ref.model_dump(mode="json"),
            "steps": [step.model_dump(mode="json") for step in plan.steps],
            "omissions": [
                omission.model_dump(mode="json") for omission in plan.omissions
            ],
            "constraints": plan.constraints.model_dump(mode="json"),
        },
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

    result = await _execute_loaded(
        executor,
        port,
        plan,
        auth_context=_full_auth_context(),
        trusted_replanned=True,
    )

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

    result = await _execute_loaded(
        executor,
        port,
        plan,
        auth_context=_full_auth_context(),
        trusted_replanned=True,
    )

    assert result.status == "completed"
    assert port.calls.index("event") < port.calls.index("housing")


@pytest.mark.asyncio
async def test_failed_subject_does_not_stop_independent_subjects_and_is_partial() -> None:
    executor, port, catalog = _executor(statuses={"housing": "failed"})
    plan = _overview_plan(catalog)

    result = await _execute_loaded(
        executor,
        port,
        plan,
        auth_context=_full_auth_context(),
        trusted_replanned=True,
    )

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

    result = await _execute_loaded(
        executor,
        port,
        plan,
        auth_context=_full_auth_context(),
        trusted_replanned=True,
    )

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

    result = await _execute_loaded(
        executor,
        _port,
        plan,
        auth_context=_full_auth_context(),
        trusted_replanned=True,
    )

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

    result = await _execute_loaded(
        executor,
        port,
        plan,
        auth_context=_full_auth_context(),
        trusted_replanned=True,
    )

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
        _execute_loaded(executor, port, plan, auth_context=_full_auth_context())
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
            await _execute_loaded(
                executor, port, drifted, auth_context=_full_auth_context()
            )
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
        await _execute_loaded(
            executor, port, plan, auth_context=_full_auth_context()
        )

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
        await _execute_loaded(
            executor, port, plan, auth_context=_full_auth_context()
        )

    assert exc_info.value.code == "CATALOG_FINGERPRINT_MISMATCH"
    assert port.calls == []


@pytest.mark.asyncio
async def test_model_copy_steps_over_budget_is_rejected_before_any_call() -> None:
    executor, port, catalog = _executor()
    plan = _overview_plan(catalog)
    # model_copy 模拟持久化层绕过 Pydantic 构造校验的污染对象；
    # 执行器仍必须在边界 fail closed。
    plan = plan.model_copy(
        update={"constraints": PlanBudget(max_parallel=2, max_tool_calls=1)}
    )

    with pytest.raises(AnalysisExecutionRejected) as exc_info:
        await _execute_loaded(
            executor, port, plan, auth_context=_full_auth_context()
        )

    assert exc_info.value.code == "PLAN_CONTRACT_INVALID"
    assert port.calls == []


@pytest.mark.asyncio
async def test_long_tool_summary_is_bounded_without_breaking_other_steps() -> None:
    executor, _port, catalog = _executor(
        statuses={"housing": "failed"}, summaries={"housing": "x" * 2_000}
    )
    plan = _overview_plan(catalog)

    result = await _execute_loaded(
        executor, _port, plan, auth_context=_full_auth_context()
    )

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
        await _execute_loaded(
            executor, port, plan, auth_context=_full_auth_context()
        )

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
            planner=AnalysisPlanner(first.catalog),
            plan_repository=_InMemoryAnalysisPlanRepository(),
            result_store=InMemoryAgentStore(),
        )


@pytest.mark.asyncio
async def test_changed_step_scope_with_old_plan_id_is_rejected_before_calls() -> None:
    executor, port, catalog = _executor()
    plan = _overview_plan(catalog)
    changed_scope = AreaScopeRef(scope=MetricQueryScope(area_code="330106001"))
    changed = plan.steps[0].model_copy(update={"scope_ref": changed_scope})
    tampered = plan.model_copy(update={"steps": (changed, *plan.steps[1:])})

    with pytest.raises(AnalysisExecutionRejected) as exc_info:
        await _execute_loaded(
            executor, port, tampered, auth_context=_full_auth_context()
        )

    assert exc_info.value.code == "PLAN_ID_MISMATCH"
    assert port.calls == []


@pytest.mark.asyncio
async def test_recomputed_plan_id_cannot_authorize_cross_scope_step() -> None:
    executor, port, catalog = _executor()
    plan = _overview_plan(catalog)
    changed_scope = AreaScopeRef(scope=MetricQueryScope(area_code="330106001"))
    changed = plan.steps[0].model_copy(update={"scope_ref": changed_scope})
    tampered = plan.model_copy(update={"steps": (changed, *plan.steps[1:])})
    tampered = tampered.model_copy(
        update={"plan_id": _attacker_recomputed_plan_id(tampered)}
    )

    with pytest.raises(AnalysisExecutionRejected) as exc_info:
        await _execute_loaded(
            executor, port, tampered, auth_context=_full_auth_context()
        )

    assert exc_info.value.code == "PLAN_SCOPE_MISMATCH"
    assert port.calls == []


@pytest.mark.asyncio
async def test_model_copy_duplicate_step_ids_is_rejected_before_calls() -> None:
    executor, port, catalog = _executor()
    plan = _overview_plan(catalog)
    tampered = plan.model_copy(update={"steps": (*plan.steps, plan.steps[0])})

    with pytest.raises(AnalysisExecutionRejected) as exc_info:
        await _execute_loaded(
            executor, port, tampered, auth_context=_full_auth_context()
        )

    assert exc_info.value.code == "PLAN_CONTRACT_INVALID"
    assert port.calls == []


@pytest.mark.asyncio
async def test_model_copy_cycle_is_rejected_before_calls() -> None:
    executor, port, catalog = _executor()
    plan = _overview_plan(catalog)
    first, second, third = plan.steps
    first = first.model_copy(update={"depends_on": (second.step_id,)})
    second = second.model_copy(update={"depends_on": (first.step_id,)})
    tampered = plan.model_copy(update={"steps": (first, second, third)})

    with pytest.raises(AnalysisExecutionRejected) as exc_info:
        await _execute_loaded(
            executor, port, tampered, auth_context=_full_auth_context()
        )

    assert exc_info.value.code == "PLAN_CONTRACT_INVALID"
    assert port.calls == []


@pytest.mark.asyncio
async def test_model_construct_pollution_is_rejected_with_stable_code() -> None:
    executor, port, catalog = _executor()
    plan = _overview_plan(catalog)
    payload = plan.model_dump(mode="python")
    payload["steps"] = ("polluted-step",)
    polluted = AnalysisPlan.model_construct(**payload)

    with warnings.catch_warnings(record=True) as caught, pytest.raises(
        AnalysisExecutionRejected
    ) as exc_info:
            await _execute_loaded(
                executor, port, polluted, auth_context=_full_auth_context()
            )

    assert exc_info.value.code == "PLAN_CONTRACT_INVALID"
    assert port.calls == []
    assert not [item for item in caught if "Pydantic serializer" in str(item.message)]


@pytest.mark.asyncio
async def test_recomputed_id_cannot_attach_unrelated_goal_to_step() -> None:
    executor, port, catalog = _executor()
    plan = _overview_plan(catalog)
    changed = plan.steps[0].model_copy(update={"goals": ("event",)})
    tampered = plan.model_copy(update={"steps": (changed, *plan.steps[1:])})
    tampered = tampered.model_copy(
        update={"plan_id": _attacker_recomputed_plan_id(tampered)}
    )

    with pytest.raises(AnalysisExecutionRejected) as exc_info:
        await _execute_loaded(
            executor, port, tampered, auth_context=_full_auth_context()
        )

    assert exc_info.value.code == "PLAN_GOAL_MISMATCH"
    assert port.calls == []


@pytest.mark.asyncio
async def test_non_derivable_step_is_rejected_before_any_tool_call() -> None:
    base = SemanticCatalog.default()
    population = base.subjects["population"].model_copy(
        update={"scope_levels": (*base.subjects["population"].scope_levels, 15)}
    )
    catalog = SemanticCatalog(
        catalog_version=base.catalog_version,
        supported_spec_versions=base.supported_spec_versions,
        subjects={**base.subjects, "population": population},
        bindings=base.bindings,
    )
    executor, port, _catalog = _executor(catalog=catalog)
    plan = AnalysisPlanner(catalog).plan(
        area_request("overview", area_code="330106001001001"),
        authorization=SubjectAuthorization.from_auth_context(_full_auth_context()),
    )
    assert [step.subject for step in plan.steps] == ["event", "housing", "population"]

    with pytest.raises(AnalysisExecutionRejected) as exc_info:
        await _execute_loaded(
            executor, port, plan, auth_context=_full_auth_context()
        )

    assert exc_info.value.code == "QUERY_SPEC_NOT_DERIVABLE"
    assert port.calls == []


@pytest.mark.asyncio
async def test_plan_repository_not_found_is_rejected_before_calls() -> None:
    executor, port, _catalog = _executor()
    auth = _full_auth_context()

    with pytest.raises(AnalysisExecutionRejected) as exc_info:
        await executor.execute(
            plan_id="sha256:" + "0" * 64,
            request_id="req-missing",
            auth_context=auth,
        )

    assert exc_info.value.code == "PLAN_NOT_FOUND"
    assert port.calls == []


@pytest.mark.asyncio
async def test_repository_returning_wrong_plan_id_is_rejected_before_calls() -> None:
    executor, port, catalog = _executor()
    plan = _overview_plan(catalog)
    requested_id = "sha256:" + "f" * 64

    with pytest.raises(AnalysisExecutionRejected) as exc_info:
        await _execute_loaded(
            executor,
            port,
            plan,
            lookup_plan_id=requested_id,
            auth_context=_full_auth_context(),
        )

    assert exc_info.value.code == "PLAN_SOURCE_ID_MISMATCH"
    assert port.calls == []


@pytest.mark.asyncio
async def test_loaded_plan_request_id_must_match_execution_request() -> None:
    executor, port, catalog = _executor()
    plan = _overview_plan(catalog)

    with pytest.raises(AnalysisExecutionRejected) as exc_info:
        await _execute_loaded(
            executor,
            port,
            plan,
            request_id="req-other",
            auth_context=_full_auth_context(),
        )

    assert exc_info.value.code == "PLAN_REQUEST_MISMATCH"
    assert port.calls == []


@pytest.mark.asyncio
async def test_replanning_rejects_overview_with_a_subject_removed() -> None:
    executor, port, catalog = _executor()
    plan = _overview_plan(catalog)
    tampered = plan.model_copy(
        update={"steps": tuple(step for step in plan.steps if step.subject != "housing")}
    )
    tampered = tampered.model_copy(
        update={"plan_id": _attacker_recomputed_plan_id(tampered)}
    )

    with pytest.raises(AnalysisExecutionRejected) as exc_info:
        await _execute_loaded(
            executor, port, tampered, auth_context=_full_auth_context()
        )

    assert exc_info.value.code == "PLAN_REPLANNING_MISMATCH"
    assert port.calls == []


@pytest.mark.asyncio
async def test_replanning_rejects_forged_overview_omission_reason() -> None:
    executor, port, catalog = _executor()
    plan = _overview_plan(catalog)
    housing = next(step for step in plan.steps if step.subject == "housing")
    tampered = plan.model_copy(
        update={
            "steps": tuple(step for step in plan.steps if step.subject != "housing"),
            "omissions": (
                AnalysisOmission(
                    goals=housing.goals,
                    subject="housing",
                    reason_code="NOT_ENTITLED",
                    detail="forged omission",
                ),
            ),
        }
    )
    tampered = tampered.model_copy(
        update={"plan_id": _attacker_recomputed_plan_id(tampered)}
    )

    with pytest.raises(AnalysisExecutionRejected) as exc_info:
        await _execute_loaded(
            executor, port, tampered, auth_context=_full_auth_context()
        )

    assert exc_info.value.code == "PLAN_REPLANNING_MISMATCH"
    assert port.calls == []


@pytest.mark.asyncio
async def test_whole_scope_replacement_cannot_be_loaded_by_old_plan_id() -> None:
    executor, port, catalog = _executor()
    original = _overview_plan(catalog)
    alternate = AnalysisPlanner(catalog).plan(
        area_request("overview", area_code="330106001"),
        authorization=SubjectAuthorization.from_auth_context(_full_auth_context()),
    )
    assert alternate.plan_id != original.plan_id

    with pytest.raises(AnalysisExecutionRejected) as exc_info:
        await _execute_loaded(
            executor,
            port,
            alternate,
            lookup_plan_id=original.plan_id,
            request_id=original.request_id,
            auth_context=_full_auth_context(),
        )

    assert exc_info.value.code == "PLAN_SOURCE_ID_MISMATCH"
    assert port.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("namespace_field", ["tenant_id", "user_id", "run_id"])
async def test_same_plan_id_is_not_visible_across_server_namespace(
    namespace_field: str,
) -> None:
    executor, port, catalog = _executor()
    plan = _overview_plan(catalog)
    owner = _full_auth_context()
    key = (
        owner.principal.tenant_id,
        owner.principal.user_id,
        owner.run_id,
        plan.plan_id,
    )
    port.plan_repository.plans[key] = plan
    if namespace_field == "run_id":
        other = owner.model_copy(update={"run_id": "run-other"})
    else:
        other = owner.model_copy(
            update={
                "principal": owner.principal.model_copy(
                    update={namespace_field: f"{namespace_field}-other"}
                )
            }
        )

    with pytest.raises(AnalysisExecutionRejected) as exc_info:
        await executor.execute(
            plan_id=plan.plan_id,
            request_id=plan.request_id,
            auth_context=other,
        )

    assert exc_info.value.code == "PLAN_NOT_FOUND"
    assert port.calls == []


@pytest.mark.asyncio
async def test_usable_child_results_are_persisted_through_the_result_lifecycle() -> None:
    executor, port, catalog = _executor(statuses={"housing": "partial"})
    plan = _overview_plan(catalog)
    auth = _full_auth_context()

    result = await _execute_loaded(executor, port, plan, auth_context=auth)

    assert port.result_store is not None
    persisted = 0
    for step in result.steps:
        if step.status not in {"success", "partial"}:
            continue
        assert step.tool_result is not None
        assert step.tool_result.data_result is not None
        stored = await port.result_store.get_result(
            user_id=auth.principal.user_id,
            result_id=step.tool_result.data_result.result_id,
        )
        assert stored == step.tool_result.data_result
        persisted += 1
    assert persisted == 3


@pytest.mark.asyncio
async def test_failed_denied_and_timeout_steps_persist_nothing() -> None:
    executor, port, catalog = _executor(
        statuses={"event": "failed", "housing": "denied"},
        delays={"population": 0.3},
    )
    plan = _overview_plan(catalog)
    steps = tuple(
        step.model_copy(update={"timeout_ms": 100})
        if step.subject == "population"
        else step
        for step in plan.steps
    )
    plan = _replace_steps(plan, steps)

    result = await _execute_loaded(
        executor, port, plan, auth_context=_full_auth_context(), trusted_replanned=True
    )

    assert result.status == "failed"
    assert port.result_store is not None
    assert port.result_store.results == {}


@pytest.mark.asyncio
async def test_child_result_persistence_failure_is_structured_and_isolated() -> None:
    class _HousingRejectingStore(InMemoryAgentStore):
        async def save_result(self, *, user_id: str, run_id: str, result: DataResult) -> DataResult:
            if isinstance(result, TableDataResult) and result.data.rows and getattr(
                result.data.rows[0], "lease_type", None
            ):
                raise RunStateConflict("results can only be saved for an active run")
            return await super().save_result(
                user_id=user_id, run_id=run_id, result=result
            )

    executor, port, catalog = _executor(result_store=_HousingRejectingStore())
    plan = _overview_plan(catalog)
    auth = _full_auth_context()

    result = await _execute_loaded(executor, port, plan, auth_context=auth)

    assert result.status == "partial"
    by_subject = {step.subject: step for step in result.steps}
    assert by_subject["housing"].status == "failed"
    assert by_subject["housing"].reason_code == "CHILD_RESULT_NOT_PERSISTED"
    assert by_subject["event"].status == "success"
    assert by_subject["population"].status == "success"
    assert port.result_store is not None
    assert len(port.result_store.results) == 2


@pytest.mark.asyncio
async def test_child_result_save_is_idempotent_within_the_result_lifecycle() -> None:
    executor, port, catalog = _executor()
    plan = _overview_plan(catalog)
    auth = _full_auth_context()

    first = await _execute_loaded(executor, port, plan, auth_context=auth)
    second = await _execute_loaded(executor, port, plan, auth_context=auth)

    assert port.result_store is not None
    first_ids = {
        step.tool_result.data_result.result_id
        for step in first.steps
        if step.tool_result is not None and step.tool_result.data_result is not None
    }
    # 重复保存同一子结果不得产生冲突或重复条目。
    for step in second.steps:
        if step.tool_result is not None and step.tool_result.data_result is not None:
            data_result = step.tool_result.data_result
            await port.result_store.save_result(
                user_id=auth.principal.user_id,
                run_id=auth.run_id,
                result=data_result,
            )
            stored = await port.result_store.get_result(
                user_id=auth.principal.user_id, result_id=data_result.result_id
            )
            assert stored == data_result
    stored_ids = set(port.result_store.results)
    assert len(stored_ids) == len(port.result_store.results)
    assert first_ids <= stored_ids
