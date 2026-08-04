"""P2 意图编译服务测试：受控意图 → 服务端可信 AnalysisRequest/Plan。

覆盖：合法 named/current 编译、区划歧义/未知/越权 fail closed、
未知/禁用/越权 goal 拒绝、重复请求幂等、相同文本跨用户/跨 run 不串
plan、overview 不自动扩大。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from full_view_agent.application.analysis_intent_service import (
    AnalysisAreaResolver,
    AnalysisIntentClarificationRequired,
    AnalysisIntentCompilationService,
    AnalysisIntentRejected,
    ResolvedArea,
)
from full_view_agent.application.analysis_planner import AnalysisPlanner
from full_view_agent.application.analysis_service import AnalysisPlanningService
from full_view_agent.domain.analysis_intent import AnalysisIntentV1
from full_view_agent.domain.analysis_plan import AnalysisRequest, PlanBudget
from full_view_agent.domain.models import AuthContext, MetricQueryScope
from full_view_agent.infrastructure.analysis_plan_repository import (
    InMemoryAnalysisPlanRepository,
)
from full_view_agent.semantic.authorization import SubjectAuthorization
from full_view_agent.semantic.catalog import SemanticCatalog

ALL_ENTITLEMENTS = (
    "governance.event.aggregate.read",
    "governance.housing.aggregate.read",
    "governance.population.aggregate.read",
)
ALL_DATASETS = ("event", "housing", "population")

KNOWN_AREAS: dict[str, tuple[ResolvedArea, ...]] = {
    "西湖区": (ResolvedArea(area_code="330106", area_name="西湖区"),),
    "滨江区": (ResolvedArea(area_code="330108", area_name="滨江区"),),
    "杭州市": (ResolvedArea(area_code="3301", area_name="杭州市"),),
    "新区": (
        ResolvedArea(area_code="330109", area_name="钱塘新区"),
        ResolvedArea(area_code="330110", area_name="临江新区"),
    ),
    "跨权限县": (ResolvedArea(area_code="330226", area_name="外市某县"),),
    "混合权限": (
        ResolvedArea(area_code="330226", area_name="外市某县"),
        ResolvedArea(area_code="330106", area_name="西湖区"),
    ),
    "坏编码区": (ResolvedArea(area_code="33010600X", area_name="坏编码区"),),
}


class FakeAreaResolver:
    """测试用区划解析器：按查询词返回预置候选。"""

    def __init__(
        self,
        areas: dict[str, tuple[ResolvedArea, ...]] | None = None,
        *,
        error: Exception | None = None,
    ) -> None:
        self._areas = KNOWN_AREAS if areas is None else areas
        self._error = error

    async def resolve_area_query(self, *, query: str) -> tuple[ResolvedArea, ...]:
        if self._error is not None:
            raise self._error
        return self._areas.get(query, ())


def make_auth_context(
    *,
    tenant_id: str = "tenant-hz",
    user_id: str = "user-01",
    run_id: str = "run-01",
    entitlements: tuple[str, ...] = ALL_ENTITLEMENTS,
    datasets: tuple[str, ...] = ALL_DATASETS,
    area_codes: tuple[str, ...] = ("3301",),
    areas: tuple[tuple[str, bool], ...] | None = None,
    field_policy_set: str = "governance_analyst_v1",
) -> AuthContext:
    effective_areas = (
        areas
        if areas is not None
        else tuple((code, True) for code in area_codes)
    )
    return AuthContext.model_validate(
        {
            "auth_context_id": f"authctx-{user_id}-{run_id}",
            "auth_context_fingerprint": f"sha256:auth-{user_id}-{run_id}",
            "principal": {
                "tenant_id": tenant_id,
                "user_id": user_id,
                "org_id": "org-01",
                "roles": ["governance_analyst"],
            },
            "application": {
                "app_id": "full_information_view",
                "agent_id": "governance_general_agent",
            },
            "entitlements": list(entitlements),
            "data_scopes": {
                "areas": [
                    {"area_code": code, "include_descendants": include}
                    for code, include in effective_areas
                ],
                "datasets": list(datasets),
                "field_policy_set": field_policy_set,
            },
            "purpose": "interactive_analysis",
            "session_id": "session-01",
            "run_id": run_id,
            "credential_ref": "cred-01",
            "issued_at": datetime(2099, 1, 1, tzinfo=UTC),
            "expires_at": datetime(2099, 1, 1, 0, 5, tzinfo=UTC),
            "policy_version": "test-v1",
        }
    )


def named_intent(*goals: str, area_query: str = "西湖区") -> AnalysisIntentV1:
    return AnalysisIntentV1.model_validate(
        {
            "goals": list(goals),
            "scope": {"kind": "named_area", "area_query": area_query},
        }
    )


def current_intent(*goals: str) -> AnalysisIntentV1:
    return AnalysisIntentV1.model_validate(
        {"goals": list(goals), "scope": {"kind": "current_area"}}
    )


def build_service(
    *,
    catalog: SemanticCatalog | None = None,
    resolver: FakeAreaResolver | None = None,
    repository: InMemoryAnalysisPlanRepository | None = None,
    default_budget: PlanBudget | None = None,
) -> tuple[
    AnalysisIntentCompilationService,
    InMemoryAnalysisPlanRepository,
]:
    catalog = catalog or SemanticCatalog.default()
    planner = AnalysisPlanner(catalog, default_budget=default_budget)
    repository = repository or InMemoryAnalysisPlanRepository()
    planning = AnalysisPlanningService(planner=planner, repository=repository)
    service = AnalysisIntentCompilationService(
        planning=planning,
        area_resolver=resolver or FakeAreaResolver(),
    )
    return service, repository


# ---------------------------------------------------------------------------
# 合法编译路径
# ---------------------------------------------------------------------------


async def test_named_area_intent_compiles_to_trusted_plan() -> None:
    service, repository = build_service()
    auth_context = make_auth_context()

    plan = await service.compile_intent(
        named_intent("population", "housing"), auth_context=auth_context
    )

    assert [step.subject for step in plan.steps] == ["housing", "population"]
    assert plan.goals == ("population", "housing")
    assert plan.scope_ref.scope == MetricQueryScope(area_code="330106")
    assert plan.omissions == ()
    loaded = await repository.get(
        tenant_id="tenant-hz",
        user_id="user-01",
        run_id="run-01",
        plan_id=plan.plan_id,
    )
    assert loaded == plan


async def test_current_area_intent_uses_server_side_hint() -> None:
    service, _ = build_service()
    auth_context = make_auth_context()

    plan = await service.compile_intent(
        current_intent("overview"),
        auth_context=auth_context,
        current_area_code="330106",
    )

    assert plan.scope_ref.scope.area_code == "330106"
    assert [step.subject for step in plan.steps] == ["event", "housing", "population"]


async def test_request_id_and_budget_are_server_generated() -> None:
    service, _ = build_service()

    plan = await service.compile_intent(
        named_intent("population"), auth_context=make_auth_context()
    )

    assert plan.request_id.startswith("areq_")
    assert 1 <= len(plan.request_id) <= 128
    # intent 不携带 budget：编译结果使用服务端默认预算
    assert plan.constraints == PlanBudget()


async def test_ambiguous_area_query_raises_clarification_with_candidates() -> None:
    service, repository = build_service()

    with pytest.raises(AnalysisIntentClarificationRequired) as exc_info:
        await service.compile_intent(
            named_intent("population", area_query="新区"),
            auth_context=make_auth_context(),
        )

    assert exc_info.value.code == "AREA_QUERY_AMBIGUOUS"
    assert [candidate.area_code for candidate in exc_info.value.candidates] == [
        "330109",
        "330110",
    ]
    assert repository._records == {}


async def test_authorization_filter_picks_single_authorized_candidate() -> None:
    service, _ = build_service()

    plan = await service.compile_intent(
        named_intent("population", area_query="混合权限"),
        auth_context=make_auth_context(),
    )

    assert plan.scope_ref.scope.area_code == "330106"


# ---------------------------------------------------------------------------
# 区划解析 fail closed
# ---------------------------------------------------------------------------


async def test_unknown_area_query_fails_closed() -> None:
    service, repository = build_service()

    with pytest.raises(AnalysisIntentRejected) as exc_info:
        await service.compile_intent(
            named_intent("population", area_query="不存在区"),
            auth_context=make_auth_context(),
        )

    assert exc_info.value.code == "AREA_QUERY_UNKNOWN"
    assert repository._records == {}


async def test_unauthorized_area_candidates_fail_closed() -> None:
    service, repository = build_service()

    with pytest.raises(AnalysisIntentRejected) as exc_info:
        await service.compile_intent(
            named_intent("population", area_query="跨权限县"),
            auth_context=make_auth_context(),
        )

    assert exc_info.value.code == "AREA_NOT_AUTHORIZED"
    assert repository._records == {}


async def test_current_area_hint_outside_authorization_rejected() -> None:
    service, repository = build_service()

    with pytest.raises(AnalysisIntentRejected) as exc_info:
        await service.compile_intent(
            current_intent("population"),
            auth_context=make_auth_context(),
            current_area_code="330206",
        )

    assert exc_info.value.code == "AREA_NOT_AUTHORIZED"
    assert repository._records == {}


async def test_current_area_hint_missing_rejected() -> None:
    service, _ = build_service()

    with pytest.raises(AnalysisIntentRejected) as exc_info:
        await service.compile_intent(
            current_intent("population"), auth_context=make_auth_context()
        )

    assert exc_info.value.code == "CURRENT_AREA_UNAVAILABLE"


@pytest.mark.parametrize("hint", ["abc123", "330", "33010600123456789", ""])
async def test_invalid_current_area_hint_rejected(hint: str) -> None:
    service, _ = build_service()

    with pytest.raises(AnalysisIntentRejected) as exc_info:
        await service.compile_intent(
            current_intent("population"),
            auth_context=make_auth_context(),
            current_area_code=hint,
        )

    assert exc_info.value.code == "CURRENT_AREA_INVALID"


async def test_resolver_returned_invalid_code_fails_closed() -> None:
    service, repository = build_service()

    with pytest.raises(AnalysisIntentRejected) as exc_info:
        await service.compile_intent(
            named_intent("population", area_query="坏编码区"),
            auth_context=make_auth_context(),
        )

    assert exc_info.value.code == "AREA_CODE_INVALID"
    assert repository._records == {}


async def test_unexpected_resolver_error_propagates_without_plan() -> None:
    service, repository = build_service(
        resolver=FakeAreaResolver(error=RuntimeError("area source down"))
    )

    with pytest.raises(RuntimeError, match="area source down"):
        await service.compile_intent(
            named_intent("population"), auth_context=make_auth_context()
        )

    assert repository._records == {}


# ---------------------------------------------------------------------------
# goal 动态准入：未知/禁用/越权拒绝，overview 不自动扩大
# ---------------------------------------------------------------------------


async def test_goal_unknown_to_catalog_rejected() -> None:
    catalog = SemanticCatalog.default()
    partial = SemanticCatalog(
        catalog_version=catalog.catalog_version,
        supported_spec_versions=catalog.supported_spec_versions,
        subjects={"population": catalog.subjects["population"]},
        bindings={"population": catalog.bindings["population"]},
    )
    service, repository = build_service(catalog=partial)

    with pytest.raises(AnalysisIntentRejected) as exc_info:
        await service.compile_intent(
            named_intent("housing"), auth_context=make_auth_context()
        )

    assert exc_info.value.code == "GOAL_NOT_DECLARED"
    assert repository._records == {}


async def test_disabled_goal_rejected() -> None:
    catalog = SemanticCatalog.default()
    unbound = SemanticCatalog(
        catalog_version=catalog.catalog_version,
        supported_spec_versions=catalog.supported_spec_versions,
        subjects=catalog.subjects,
        bindings={
            subject_id: binding
            for subject_id, binding in catalog.bindings.items()
            if subject_id != "housing"
        },
    )
    service, repository = build_service(catalog=unbound)

    with pytest.raises(AnalysisIntentRejected) as exc_info:
        await service.compile_intent(
            named_intent("housing"), auth_context=make_auth_context()
        )

    assert exc_info.value.code == "GOAL_DISABLED"
    assert repository._records == {}


async def test_unentitled_goal_rejected() -> None:
    service, repository = build_service()
    population_only = make_auth_context(
        entitlements=("governance.population.aggregate.read",),
        datasets=("population",),
    )

    with pytest.raises(AnalysisIntentRejected) as exc_info:
        await service.compile_intent(
            named_intent("housing"), auth_context=population_only
        )

    assert exc_info.value.code == "GOAL_NOT_ENTITLED"
    assert repository._records == {}


async def test_compilation_never_expands_goals_to_overview() -> None:
    service, repository = build_service()
    population_only = make_auth_context(
        entitlements=("governance.population.aggregate.read",),
        datasets=("population",),
    )

    plan = await service.compile_intent(
        named_intent("population"), auth_context=population_only
    )

    # 只请求 population：不得扩大成 overview 或其他主题
    assert plan.goals == ("population",)
    assert "overview" not in plan.goals
    assert [step.subject for step in plan.steps] == ["population"]
    assert len(repository._records) == 1

    # 被拒绝的意图也不得落库成任何 overview 计划
    with pytest.raises(AnalysisIntentRejected):
        await service.compile_intent(
            named_intent("housing"), auth_context=population_only
        )
    assert len(repository._records) == 1


async def test_intent_compiling_to_no_steps_fails_closed() -> None:
    service, repository = build_service()

    # 3301 为市级（4 位），population 仅支持 6/9/12 级：
    # 意图只能编译出全 omission 计划，必须 fail closed 而非落库空计划。
    with pytest.raises(AnalysisIntentRejected) as exc_info:
        await service.compile_intent(
            named_intent("population", area_query="杭州市"),
            auth_context=make_auth_context(),
        )

    assert exc_info.value.code == "NO_EXECUTABLE_GOALS"
    assert repository._records == {}


# ---------------------------------------------------------------------------
# 幂等与隔离
# ---------------------------------------------------------------------------


async def test_repeat_intent_is_idempotent_and_forms_one_plan() -> None:
    service, repository = build_service()
    auth_context = make_auth_context()

    first = await service.compile_intent(
        named_intent("population", "housing"), auth_context=auth_context
    )
    second = await service.compile_intent(
        named_intent("population", "housing"), auth_context=auth_context
    )

    assert second.request_id == first.request_id
    assert second.plan_id == first.plan_id
    assert len(repository._records) == 1


async def test_different_intents_form_distinct_plans() -> None:
    service, repository = build_service()
    auth_context = make_auth_context()

    population = await service.compile_intent(
        named_intent("population"), auth_context=auth_context
    )
    housing = await service.compile_intent(
        named_intent("housing"), auth_context=auth_context
    )

    assert population.request_id != housing.request_id
    assert population.plan_id != housing.plan_id
    assert len(repository._records) == 2


async def test_same_intent_text_does_not_bleed_across_users() -> None:
    service, repository = build_service()

    user_a = await service.compile_intent(
        named_intent("population"), auth_context=make_auth_context(user_id="user-a")
    )
    user_b = await service.compile_intent(
        named_intent("population"), auth_context=make_auth_context(user_id="user-b")
    )

    assert user_a.request_id != user_b.request_id
    assert user_a.plan_id != user_b.plan_id
    assert (
        await repository.get(
            tenant_id="tenant-hz",
            user_id="user-a",
            run_id="run-01",
            plan_id=user_b.plan_id,
        )
        is None
    )
    assert (
        await repository.get(
            tenant_id="tenant-hz",
            user_id="user-b",
            run_id="run-01",
            plan_id=user_a.plan_id,
        )
        is None
    )


async def test_same_intent_text_does_not_bleed_across_runs() -> None:
    service, repository = build_service()

    run_one = await service.compile_intent(
        named_intent("population"), auth_context=make_auth_context(run_id="run-01")
    )
    run_two = await service.compile_intent(
        named_intent("population"), auth_context=make_auth_context(run_id="run-02")
    )

    assert run_one.request_id != run_two.request_id
    assert (
        await repository.get(
            tenant_id="tenant-hz",
            user_id="user-01",
            run_id="run-01",
            plan_id=run_two.plan_id,
        )
        is None
    )


async def test_replay_under_reduced_authorization_does_not_return_stale_plan() -> None:
    service, _ = build_service()
    intent = named_intent("population", "housing")

    full_plan = await service.compile_intent(
        intent, auth_context=make_auth_context()
    )
    assert [step.subject for step in full_plan.steps] == ["housing", "population"]

    # 权限收缩后同一意图不得回放旧计划，必须按当前授权 fail closed
    reduced = make_auth_context(
        entitlements=("governance.population.aggregate.read",),
        datasets=("population",),
    )
    with pytest.raises(AnalysisIntentRejected) as exc_info:
        await service.compile_intent(intent, auth_context=reduced)
    assert exc_info.value.code == "GOAL_NOT_ENTITLED"


async def test_intent_boundary_rejects_sensitive_payloads_before_service() -> None:
    # 服务只接受已通过契约校验的意图；注入字段在边界即被拒绝
    with pytest.raises(ValidationError):
        AnalysisIntentV1.model_validate(
            {
                "goals": ["population"],
                "scope": {"kind": "named_area", "area_query": "西湖区"},
                "steps": [{"subject": "population", "capability_id": "x"}],
            }
        )


async def test_planning_service_exposes_its_planner() -> None:
    catalog = SemanticCatalog.default()
    planner = AnalysisPlanner(catalog)
    planning = AnalysisPlanningService(
        planner=planner, repository=InMemoryAnalysisPlanRepository()
    )
    assert planning.planner is planner


async def test_compiled_plan_matches_trusted_replanning() -> None:
    catalog = SemanticCatalog.default()
    planner = AnalysisPlanner(catalog)
    planning = AnalysisPlanningService(
        planner=planner, repository=InMemoryAnalysisPlanRepository()
    )
    service = AnalysisIntentCompilationService(
        planning=planning, area_resolver=FakeAreaResolver()
    )
    auth_context = make_auth_context()

    plan = await service.compile_intent(
        named_intent("overview"), auth_context=auth_context
    )

    # 编译产物必须能通过可信加载器的重规划一致性校验前提：
    # 用同一 planner 重新规划得到完全相同的计划。
    replanned = planner.plan(
        AnalysisRequest(
            request_id=plan.request_id,
            goals=plan.goals,
            scope_ref=plan.scope_ref,
            budget=plan.constraints,
        ),
        authorization=SubjectAuthorization.from_auth_context(auth_context),
    )
    assert replanned == plan


def test_resolver_protocol_is_structural() -> None:
    assert isinstance(FakeAreaResolver(), AnalysisAreaResolver)


async def test_area_authorization_follows_scope_direction() -> None:
    # 区域判定复用授权上下文中的 scope：仅授权区县本身时，
    # 区县可解析，其上级市不在授权范围内必须 fail closed。
    service, _ = build_service()
    district_only = make_auth_context(area_codes=("330106",))

    plan = await service.compile_intent(
        named_intent("housing", area_query="西湖区"), auth_context=district_only
    )
    assert plan.scope_ref.scope.area_code == "330106"

    with pytest.raises(AnalysisIntentRejected) as exc_info:
        await service.compile_intent(
            named_intent("housing", area_query="杭州市"), auth_context=district_only
        )
    assert exc_info.value.code == "AREA_NOT_AUTHORIZED"


# ---------------------------------------------------------------------------
# request_id 必须绑定解析后的可信编译上下文（scope/授权/Catalog/预算）
# ---------------------------------------------------------------------------


async def test_current_area_change_produces_distinct_request_ids() -> None:
    # current_area 意图载荷不含任何区划字段：上下文区划从 330106 变到
    # 330108 时，可信 scope 已不同，request_id 必须不同，否则同一
    # request_id 会对应两个不同 plan，破坏唯一语义与恢复。
    service, repository = build_service()
    auth_context = make_auth_context()

    first = await service.compile_intent(
        current_intent("population"),
        auth_context=auth_context,
        current_area_code="330106",
    )
    second = await service.compile_intent(
        current_intent("population"),
        auth_context=auth_context,
        current_area_code="330108",
    )

    assert first.scope_ref.scope.area_code == "330106"
    assert second.scope_ref.scope.area_code == "330108"
    assert first.request_id != second.request_id
    assert first.plan_id != second.plan_id
    assert len(repository._records) == 2


async def test_resolver_drift_produces_distinct_request_ids() -> None:
    # 同一 area_query 在解析器映射漂移后解析到不同区划：request_id 必须
    # 绑定解析结果而非原始查询文本。
    auth_context = make_auth_context()
    service_before, repository = build_service(
        resolver=FakeAreaResolver(
            {"西湖区": (ResolvedArea(area_code="330106", area_name="西湖区"),)}
        )
    )
    before = await service_before.compile_intent(
        named_intent("population"), auth_context=auth_context
    )

    service_after, _ = build_service(
        resolver=FakeAreaResolver(
            {"西湖区": (ResolvedArea(area_code="330108", area_name="西湖区"),)}
        ),
        repository=repository,
    )
    after = await service_after.compile_intent(
        named_intent("population"), auth_context=auth_context
    )

    assert before.scope_ref.scope.area_code == "330106"
    assert after.scope_ref.scope.area_code == "330108"
    assert before.request_id != after.request_id
    assert before.plan_id != after.plan_id
    assert len(repository._records) == 2


async def test_catalog_change_produces_distinct_request_ids() -> None:
    # Catalog 版本/执行指纹变化会改变编译产物：request_id 必须随之变化，
    # 避免同一 (tenant,user,run,request_id) 唯一键下出现不同 plan 内容。
    catalog = SemanticCatalog.default()
    revised = SemanticCatalog(
        catalog_version=f"{catalog.catalog_version}-rev2",
        supported_spec_versions=catalog.supported_spec_versions,
        subjects=catalog.subjects,
        bindings=catalog.bindings,
    )
    assert revised.execution_fingerprint != catalog.execution_fingerprint

    service_before, _ = build_service(catalog=catalog)
    service_after, _ = build_service(catalog=revised)
    auth_context = make_auth_context()

    before = await service_before.compile_intent(
        named_intent("population"), auth_context=auth_context
    )
    after = await service_after.compile_intent(
        named_intent("population"), auth_context=auth_context
    )

    assert before.request_id != after.request_id


async def test_default_budget_change_produces_distinct_request_ids() -> None:
    # 服务端默认预算是编译上下文的一部分：默认预算策略变化必须产生新的
    # request_id（编译产物 constraints 随之改变）。
    service_default, _ = build_service()
    service_tight, _ = build_service(
        default_budget=PlanBudget(max_tool_calls=8, total_timeout_ms=30_000)
    )
    auth_context = make_auth_context()

    baseline = await service_default.compile_intent(
        named_intent("population"), auth_context=auth_context
    )
    tight = await service_tight.compile_intent(
        named_intent("population"), auth_context=auth_context
    )

    assert baseline.constraints != tight.constraints
    assert baseline.request_id != tight.request_id


async def test_planner_exposes_read_only_default_budget() -> None:
    budget = PlanBudget(max_tool_calls=5)
    planner = AnalysisPlanner(SemanticCatalog.default(), default_budget=budget)
    assert planner.default_budget == budget
    assert AnalysisPlanner(SemanticCatalog.default()).default_budget == PlanBudget()


# ---------------------------------------------------------------------------
# 授权视图规范化：顺序/重复不影响 request_id 与 plan
# ---------------------------------------------------------------------------


async def test_authorization_order_and_duplicates_do_not_change_request_id() -> None:
    service, repository = build_service()
    baseline = make_auth_context()
    shuffled = make_auth_context(
        entitlements=ALL_ENTITLEMENTS[::-1] + (ALL_ENTITLEMENTS[0],),
        datasets=ALL_DATASETS[::-1] + (ALL_DATASETS[0],),
        areas=(("3301", True), ("3301", True)),
    )

    first = await service.compile_intent(
        named_intent("population"), auth_context=baseline
    )
    second = await service.compile_intent(
        named_intent("population"), auth_context=shuffled
    )

    assert second.request_id == first.request_id
    assert second.plan_id == first.plan_id
    assert len(repository._records) == 1


# ---------------------------------------------------------------------------
# resolver 候选硬化：去重/冲突 fail closed/确定性顺序/上界
# ---------------------------------------------------------------------------


async def test_duplicate_candidate_entries_are_deduped_not_ambiguous() -> None:
    service, _ = build_service(
        resolver=FakeAreaResolver(
            {
                "西湖区": (
                    ResolvedArea(area_code="330106", area_name="西湖区"),
                    ResolvedArea(area_code="330106", area_name="西湖区"),
                )
            }
        )
    )

    plan = await service.compile_intent(
        named_intent("population"), auth_context=make_auth_context()
    )

    assert plan.scope_ref.scope.area_code == "330106"


async def test_same_area_code_with_conflicting_names_fails_closed() -> None:
    service, repository = build_service(
        resolver=FakeAreaResolver(
            {
                "西湖区": (
                    ResolvedArea(area_code="330106", area_name="西湖区"),
                    ResolvedArea(area_code="330106", area_name="灵隐区"),
                )
            }
        )
    )

    with pytest.raises(AnalysisIntentRejected) as exc_info:
        await service.compile_intent(
            named_intent("population"), auth_context=make_auth_context()
        )

    assert exc_info.value.code == "AREA_CANDIDATE_CONFLICT"
    assert repository._records == {}


async def test_clarification_candidates_are_deterministically_ordered() -> None:
    # resolver 返回顺序任意：歧义候选必须按编码确定性排序。
    service, _ = build_service(
        resolver=FakeAreaResolver(
            {
                "新区": (
                    ResolvedArea(area_code="330110", area_name="临江新区"),
                    ResolvedArea(area_code="330109", area_name="钱塘新区"),
                )
            }
        )
    )

    with pytest.raises(AnalysisIntentClarificationRequired) as exc_info:
        await service.compile_intent(
            named_intent("population", area_query="新区"),
            auth_context=make_auth_context(),
        )

    assert [candidate.area_code for candidate in exc_info.value.candidates] == [
        "330109",
        "330110",
    ]


async def test_candidate_count_above_bound_fails_closed() -> None:
    overflow = tuple(
        ResolvedArea(area_code=f"3301{i:02d}", area_name=f"候选区{i}")
        for i in range(33)
    )
    service, repository = build_service(
        resolver=FakeAreaResolver({"大区": overflow})
    )

    with pytest.raises(AnalysisIntentRejected) as exc_info:
        await service.compile_intent(
            named_intent("population", area_query="大区"),
            auth_context=make_auth_context(),
        )

    assert exc_info.value.code == "AREA_CANDIDATES_OVERFLOW"
    assert repository._records == {}


async def test_any_structurally_invalid_candidate_fails_closed() -> None:
    # 候选必须先完整结构校验：任一候选结构非法即整体 fail closed，
    # 不得静默丢弃后继续编译。
    service, repository = build_service(
        resolver=FakeAreaResolver(
            {
                "混合坏区": (
                    ResolvedArea(area_code="330106", area_name="西湖区"),
                    ResolvedArea(area_code="33010600X", area_name="坏编码区"),
                )
            }
        )
    )

    with pytest.raises(AnalysisIntentRejected) as exc_info:
        await service.compile_intent(
            named_intent("population", area_query="混合坏区"),
            auth_context=make_auth_context(),
        )

    assert exc_info.value.code == "AREA_CODE_INVALID"
    assert repository._records == {}


async def test_validation_bypassed_candidate_is_revalidated_and_rejected() -> None:
    # 绕过校验构造（model_construct）的候选必须被重新校验拦截。
    smuggled = ResolvedArea.model_construct(
        area_code="330106", area_name="区" * 101
    )
    service, repository = build_service(
        resolver=FakeAreaResolver({"西湖区": (smuggled,)})
    )

    with pytest.raises(AnalysisIntentRejected) as exc_info:
        await service.compile_intent(
            named_intent("population"), auth_context=make_auth_context()
        )

    assert exc_info.value.code == "AREA_CODE_INVALID"
    assert repository._records == {}


def test_resolved_area_contract_is_frozen_bounded_and_strict() -> None:
    area = ResolvedArea(area_code="330106", area_name="西湖区")

    with pytest.raises(ValidationError):
        area.area_name = "滨江区"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        ResolvedArea(area_code="", area_name="西湖区")
    with pytest.raises(ValidationError):
        ResolvedArea(area_code="3" * 33, area_name="西湖区")
    with pytest.raises(ValidationError):
        ResolvedArea(area_code="330106", area_name="")
    with pytest.raises(ValidationError):
        ResolvedArea(area_code="330106", area_name="区" * 101)
    with pytest.raises(ValidationError):
        ResolvedArea(area_code="330106", area_name="西湖区", evil="inject")  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# 类型化 fail closed：非字符串 current_area_code 不允许 TypeError 逃逸
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("hint", [330106, ("330106",), ["330106"], b"330106"])
async def test_non_string_current_area_hint_fails_closed_typed(hint: object) -> None:
    service, repository = build_service()

    with pytest.raises(AnalysisIntentRejected) as exc_info:
        await service.compile_intent(
            current_intent("population"),
            auth_context=make_auth_context(),
            current_area_code=hint,  # type: ignore[arg-type]
        )

    assert exc_info.value.code == "CURRENT_AREA_INVALID"
    assert repository._records == {}
