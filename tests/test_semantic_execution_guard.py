"""S0 语义内核候选：执行层按实际主题复核权限的测试。

即使未来存在通用语义入口，执行前也必须按编译计划中的真实主题 Tool
再次复核 Tool 授权、数据集、区域与字段策略 —— 复用生产
MinimalPolicyAdapter，不复制第二套策略逻辑。
"""

import pytest

from full_view_agent.application.policy import MinimalPolicyAdapter
from full_view_agent.application.tool_registry import ToolRegistry
from full_view_agent.domain import models
from full_view_agent.domain.models import AuthorizedAreaScope
from full_view_agent.semantic import (
    EvidenceExpectation,
    ExecutionGuard,
    ExpectedResultShape,
    PlanStep,
    SemanticCatalog,
    SemanticCompiler,
    SemanticPlan,
    SemanticQuerySpec,
    SubjectAuthorization,
)

from .test_policy import population_auth_context


def _full_governance_auth_context(
    *, areas: tuple[str, ...] = ("3301",)
) -> models.AuthContext:
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
                    "areas": [
                        models.AuthorizedAreaScope(
                            area_code=area_code, include_descendants=True
                        )
                        for area_code in areas
                    ],
                    "datasets": [
                        "administrative_area",
                        "event",
                        "housing",
                        "population",
                    ],
                }
            ),
        }
    )


def _auth_view(auth_context: models.AuthContext) -> SubjectAuthorization:
    return SubjectAuthorization.from_auth_context(auth_context)


@pytest.fixture
def guard() -> ExecutionGuard:
    return ExecutionGuard(
        catalog=SemanticCatalog.default(),
        registry=ToolRegistry.default(),
        policy=MinimalPolicyAdapter(),
    )


@pytest.fixture
def compiler() -> SemanticCompiler:
    return SemanticCompiler(SemanticCatalog.default())


def _housing_spec(**overrides: object) -> SemanticQuerySpec:
    defaults: dict[str, object] = {
        "subject": "housing",
        "metrics": ["dwelling_count"],
        "scope": {"area_code": "330106"},
        "filters": [],
        "group_by": [],
    }
    defaults.update(overrides)
    return SemanticQuerySpec.model_validate(defaults)


def _population_spec() -> SemanticQuerySpec:
    return SemanticQuerySpec.model_validate(
        {
            "subject": "population",
            "metrics": ["person_count"],
            "scope": {"area_code": "330106"},
            "filters": [
                {
                    "field": "person_category",
                    "operator": "eq",
                    "value": "solitary_elderly",
                }
            ],
            "group_by": ["street"],
        }
    )


# ---------------------------------------------------------------------------
# 正常路径：全授权用户的计划按实际主题 Tool 复核通过
# ---------------------------------------------------------------------------


def test_guard_allows_fully_authorized_population_plan(
    guard: ExecutionGuard,
    compiler: SemanticCompiler,
) -> None:
    auth_context = _full_governance_auth_context()
    plan = compiler.compile(
        _population_spec(), authorization=_auth_view(auth_context)
    )

    recheck = guard.recheck(plan, auth_context=auth_context)

    assert recheck.allowed is True
    assert recheck.denial_codes == ()
    assert len(recheck.decisions) == 1
    # 复核评估的是真实主题的 manifest，而不是任何通用入口。
    assert recheck.decisions[0].tool_id == "governance.query_population_metrics"
    assert recheck.decisions[0].decision == "allow"


# ---------------------------------------------------------------------------
# 核心反例：通用入口授权不能替代真实主题授权
# ---------------------------------------------------------------------------


def test_guard_rechecks_actual_subject_not_generic_entry(
    guard: ExecutionGuard,
    compiler: SemanticCompiler,
) -> None:
    # 用户只有人口主题权限；即使计划来自语义层，住房主题仍须被拒绝。
    auth_context = population_auth_context()
    plan = compiler.compile(
        _housing_spec(),
        authorization=SubjectAuthorization(
            entitlements=(
                "governance.population.aggregate.read",
                "governance.housing.aggregate.read",
            ),
            datasets=("population", "housing"),
            area_scopes=(AuthorizedAreaScope(area_code="3301"),),
            field_policy_set="governance_analyst_v1",
        ),
    )

    recheck = guard.recheck(plan, auth_context=auth_context)

    assert recheck.allowed is False
    assert "TOOL_NOT_ENTITLED" in recheck.denial_codes
    assert recheck.decisions[0].tool_id == "governance.query_housing_metrics"


def test_guard_rejects_dataset_not_authorized(
    guard: ExecutionGuard,
    compiler: SemanticCompiler,
) -> None:
    auth_context = _full_governance_auth_context()
    narrowed = auth_context.model_copy(
        update={
            "data_scopes": auth_context.data_scopes.model_copy(
                update={"datasets": ["population"]}
            )
        }
    )
    plan = compiler.compile(
        _housing_spec(),
        authorization=SubjectAuthorization(
            entitlements=tuple(narrowed.entitlements),
            datasets=("population", "housing"),
            area_scopes=(AuthorizedAreaScope(area_code="3301"),),
            field_policy_set="governance_analyst_v1",
        ),
    )

    recheck = guard.recheck(plan, auth_context=narrowed)

    assert recheck.allowed is False
    assert "DATASET_NOT_AUTHORIZED" in recheck.denial_codes


def test_guard_rejects_area_out_of_scope(
    guard: ExecutionGuard,
    compiler: SemanticCompiler,
) -> None:
    auth_context = _full_governance_auth_context(areas=("330106",))
    plan = compiler.compile(
        _housing_spec(scope={"area_code": "330105"}),
        authorization=SubjectAuthorization(
            entitlements=tuple(auth_context.entitlements),
            datasets=("housing",),
            area_scopes=(AuthorizedAreaScope(area_code="3301"),),
            field_policy_set="governance_analyst_v1",
        ),
    )

    recheck = guard.recheck(plan, auth_context=auth_context)

    assert recheck.allowed is False
    assert "AREA_OUT_OF_SCOPE" in recheck.denial_codes


def test_guard_rejects_unknown_or_generic_capability(
    guard: ExecutionGuard,
    compiler: SemanticCompiler,
) -> None:
    auth_context = _full_governance_auth_context()
    plan = compiler.compile(
        _population_spec(), authorization=_auth_view(auth_context)
    )
    tampered = plan.model_copy(
        update={
            "steps": (
                PlanStep(
                    capability_id="governance.semantic_query",
                    capability_version="1.0.0",
                    arguments=plan.steps[0].arguments,
                ),
            )
        }
    )

    recheck = guard.recheck(tampered, auth_context=auth_context)

    assert recheck.allowed is False
    # 泛化/未知能力标识与主题绑定不一致，必须在 Policy 复核之前拒绝。
    assert "CAPABILITY_MISMATCH" in recheck.denial_codes
    assert recheck.decisions == ()


def test_guard_rejects_plan_arguments_invalid_for_target_tool(
    guard: ExecutionGuard,
    compiler: SemanticCompiler,
) -> None:
    auth_context = _full_governance_auth_context()
    plan = compiler.compile(
        _population_spec(), authorization=_auth_view(auth_context)
    )
    tampered = plan.model_copy(
        update={
            "steps": (
                PlanStep(
                    capability_id=plan.steps[0].capability_id,
                    capability_version=plan.steps[0].capability_version,
                    arguments={"query": {"scope": {"area_code": ""}}},
                ),
            )
        }
    )

    recheck = guard.recheck(tampered, auth_context=auth_context)

    assert recheck.allowed is False
    assert "PLAN_ARGUMENTS_INVALID" in recheck.denial_codes


# ---------------------------------------------------------------------------
# 计划完整性与版本复核：陈旧/篡改计划必须 fail closed，不得执行 Policy
# ---------------------------------------------------------------------------


def test_guard_rejects_stale_catalog_version(
    guard: ExecutionGuard,
    compiler: SemanticCompiler,
) -> None:
    auth_context = _full_governance_auth_context()
    plan = compiler.compile(_population_spec(), authorization=_auth_view(auth_context))
    tampered = plan.model_copy(update={"catalog_version": "0.0.0-obsolete"})

    recheck = guard.recheck(tampered, auth_context=auth_context)

    assert recheck.allowed is False
    assert "CATALOG_VERSION_MISMATCH" in recheck.denial_codes
    # 完整性校验必须先于 Policy：不得产生任何 Policy 决策。
    assert recheck.decisions == ()


def test_guard_rejects_tampered_capability_version(
    guard: ExecutionGuard,
    compiler: SemanticCompiler,
) -> None:
    auth_context = _full_governance_auth_context()
    plan = compiler.compile(_population_spec(), authorization=_auth_view(auth_context))
    step = plan.steps[0]
    tampered = plan.model_copy(
        update={
            "steps": (
                PlanStep(
                    capability_id=step.capability_id,
                    capability_version="999.0.0",
                    arguments=step.arguments,
                ),
            )
        }
    )

    recheck = guard.recheck(tampered, auth_context=auth_context)

    assert recheck.allowed is False
    assert "CAPABILITY_VERSION_MISMATCH" in recheck.denial_codes
    assert recheck.decisions == ()


def test_guard_rejects_unknown_plan_subject(
    guard: ExecutionGuard,
    compiler: SemanticCompiler,
) -> None:
    auth_context = _full_governance_auth_context()
    plan = compiler.compile(_population_spec(), authorization=_auth_view(auth_context))
    tampered = plan.model_copy(update={"subject": "traffic"})

    recheck = guard.recheck(tampered, auth_context=auth_context)

    assert recheck.allowed is False
    assert "UNKNOWN_SUBJECT" in recheck.denial_codes
    assert recheck.decisions == ()


def test_guard_rejects_tampered_logical_dataset(
    guard: ExecutionGuard,
    compiler: SemanticCompiler,
) -> None:
    auth_context = _full_governance_auth_context()
    plan = compiler.compile(_housing_spec(), authorization=_auth_view(auth_context))
    tampered = plan.model_copy(update={"logical_dataset_id": "population"})

    recheck = guard.recheck(tampered, auth_context=auth_context)

    assert recheck.allowed is False
    assert "LOGICAL_DATASET_MISMATCH" in recheck.denial_codes
    assert recheck.decisions == ()


def test_guard_rejects_cross_subject_capability_swap(
    guard: ExecutionGuard,
    compiler: SemanticCompiler,
) -> None:
    auth_context = _full_governance_auth_context()
    plan = compiler.compile(_population_spec(), authorization=_auth_view(auth_context))
    # 换成另一主题的真实能力与契约参数：全授权用户能通过 Policy 复核，
    # 因此替换必须先被主题绑定一致性校验拦截，不得执行 Policy。
    swap_source = compiler.compile(
        _housing_spec(), authorization=_auth_view(auth_context)
    )
    tampered = plan.model_copy(update={"steps": swap_source.steps})

    recheck = guard.recheck(tampered, auth_context=auth_context)

    assert recheck.allowed is False
    assert "CAPABILITY_MISMATCH" in recheck.denial_codes
    assert recheck.decisions == ()


def test_guard_rejects_extra_plan_steps(
    guard: ExecutionGuard,
    compiler: SemanticCompiler,
) -> None:
    # S0 每个主题只允许编译器定义的单步计划；追加步骤整体拒绝。
    auth_context = _full_governance_auth_context()
    population_plan = compiler.compile(
        _population_spec(), authorization=_auth_view(auth_context)
    )
    event_plan = compiler.compile(
        SemanticQuerySpec.model_validate(
            {
                "subject": "event",
                "metrics": ["finish_rate"],
                "scope": {"area_code": "330106"},
            }
        ),
        authorization=_auth_view(auth_context),
    )
    tampered = population_plan.model_copy(
        update={"steps": population_plan.steps + event_plan.steps}
    )

    recheck = guard.recheck(tampered, auth_context=auth_context)

    assert recheck.allowed is False
    assert "PLAN_STEPS_INVALID" in recheck.denial_codes
    assert recheck.decisions == ()


def _drift_plan(*, capability_id: str, arguments: dict[str, object]) -> SemanticPlan:
    return SemanticPlan(
        catalog_version=SemanticCatalog.default().catalog_version,
        subject="population",
        logical_dataset_id="population",
        steps=(
            PlanStep(
                capability_id=capability_id,
                capability_version="1.0.0",
                arguments=arguments,
            ),
        ),
        expected_result=ExpectedResultShape(
            data_schema_ref="schema://data/population-metric-table/1.0.0",
            row_fields=("area_code", "area_name", "person_count"),
        ),
        evidence=EvidenceExpectation(dataset_id="population", metric_definitions=()),
    )


def _drifted_catalog(capability_id: str) -> SemanticCatalog:
    base = SemanticCatalog.default()
    binding = base.binding("population")
    assert binding is not None
    return SemanticCatalog(
        catalog_version=base.catalog_version,
        supported_spec_versions=base.supported_spec_versions,
        subjects=base.subjects,
        bindings={
            "population": binding.model_copy(update={"capability_id": capability_id}),
            "housing": base.binding("housing"),
            "event": base.binding("event"),
        },
    )


def test_guard_rejects_capability_missing_from_registry() -> None:
    # Catalog/Registry 漂移：绑定指向未注册 Tool 时按 UNKNOWN_CAPABILITY 拒绝。
    guard = ExecutionGuard(
        catalog=_drifted_catalog("governance.not_registered"),
        registry=ToolRegistry.default(),
        policy=MinimalPolicyAdapter(),
    )
    plan = _drift_plan(
        capability_id="governance.not_registered",
        arguments={
            "query": {
                "metrics": ["person_count"],
                "scope": {"area_code": "330106"},
                "filters": [
                    {
                        "field": "person_category",
                        "operator": "eq",
                        "value": "solitary_elderly",
                    }
                ],
                "group_by": ["street"],
            }
        },
    )

    recheck = guard.recheck(plan, auth_context=_full_governance_auth_context())

    assert recheck.allowed is False
    assert "UNKNOWN_CAPABILITY" in recheck.denial_codes
    assert recheck.decisions == ()


def test_guard_rejects_manifest_dataset_drift() -> None:
    # 绑定指向已注册但数据集不同的 Tool：manifest 数据集与主题不一致 → 拒绝。
    guard = ExecutionGuard(
        catalog=_drifted_catalog("governance.resolve_area"),
        registry=ToolRegistry.default(),
        policy=MinimalPolicyAdapter(),
    )
    plan = _drift_plan(
        capability_id="governance.resolve_area",
        arguments={"query": {"keyword": "翠苑"}},
    )

    recheck = guard.recheck(plan, auth_context=_full_governance_auth_context())

    assert recheck.allowed is False
    assert "LOGICAL_DATASET_MISMATCH" in recheck.denial_codes
    assert recheck.decisions == ()


def test_subject_authorization_round_trips_auth_context() -> None:
    auth_context = _full_governance_auth_context()
    view = SubjectAuthorization.from_auth_context(auth_context)
    assert "governance.housing.aggregate.read" in view.entitlements
    assert "housing" in view.datasets
    assert view.field_policy_set == "governance_analyst_v1"
    assert view.area_scopes[0].area_code == "3301"
