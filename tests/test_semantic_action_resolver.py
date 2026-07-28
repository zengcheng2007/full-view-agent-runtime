"""S1-A：governance.semantic_query 虚拟 Tool 的语义动作解析器。

解析链路为 parse → S1-A 主题白名单（fail closed）→ 必填筛选强制 →
Validator → Compiler → ExecutionGuard 生产 Policy 复核，成功时产出
规范 ToolAction、SemanticPlan 与结果血缘。housing/event 仅保留 Catalog
声明，本期禁止绑定；所有反例必须结构化拒绝，不得静默改写或放行。
"""

import pytest

from full_view_agent.application.policy import MinimalPolicyAdapter
from full_view_agent.application.tool_registry import ToolRegistry
from full_view_agent.domain.models import QueryPopulationMetricsInput
from full_view_agent.semantic.action_resolver import (
    S1A_BINDABLE_SUBJECTS,
    SEMANTIC_QUERY_TOOL_ID,
    SEMANTIC_QUERY_TOOL_VERSION,
    DeniedSemanticAction,
    RejectedSemanticAction,
    ResolvedSemanticAction,
    SemanticActionResolver,
)
from full_view_agent.semantic.catalog import SemanticCatalog

from .test_policy import population_auth_context


def _resolver(
    *,
    catalog: SemanticCatalog | None = None,
    registry: ToolRegistry | None = None,
) -> SemanticActionResolver:
    return SemanticActionResolver(
        catalog=catalog or SemanticCatalog.default(),
        registry=registry or ToolRegistry.default(),
        policy=MinimalPolicyAdapter(),
    )


def _population_spec(**spec_overrides: object) -> dict[str, object]:
    """构造 semantic_query 原始参数；override 直接覆盖 spec 字段。"""
    spec: dict[str, object] = {
        "subject": "population",
        "metrics": ["person_count"],
        "scope": {"area_code": "330106"},
        "filters": [
            {"field": "person_category", "operator": "eq", "value": "solitary_elderly"}
        ],
        "group_by": ["street"],
    }
    spec.update(spec_overrides)
    return {"spec": spec}


# ---------------------------------------------------------------------------
# 成功路径：population + 授权区域 → 规范 ToolAction + 血缘
# ---------------------------------------------------------------------------


def test_resolver_compiles_population_spec_to_canonical_tool_action() -> None:
    resolution = _resolver().resolve(
        _population_spec(), auth_context=population_auth_context()
    )

    assert isinstance(resolution, ResolvedSemanticAction)
    action = resolution.canonical_action
    assert action.tool_id == "governance.query_population_metrics"
    # 规范参数必须能被生产 Tool 输入契约直接校验。
    QueryPopulationMetricsInput.model_validate(action.arguments)
    query = action.arguments["query"]
    assert isinstance(query, dict)
    assert query["metrics"] == ["person_count"]
    assert query["scope"]["area_code"] == "330106"
    assert query["group_by"] == ["street"]
    assert query["filters"] == [
        {"field": "person_category", "operator": "eq", "value": "solitary_elderly"}
    ]


def test_resolver_attaches_versioned_semantic_lineage() -> None:
    catalog = SemanticCatalog.default()
    resolution = _resolver(catalog=catalog).resolve(
        _population_spec(), auth_context=population_auth_context()
    )

    assert isinstance(resolution, ResolvedSemanticAction)
    lineage = resolution.lineage
    assert lineage.virtual_tool_id == SEMANTIC_QUERY_TOOL_ID
    assert lineage.virtual_tool_version == SEMANTIC_QUERY_TOOL_VERSION
    assert lineage.spec_version == "s0.1"
    assert lineage.catalog_version == catalog.catalog_version
    assert lineage.subject == "population"
    assert lineage.logical_dataset_id == "population"
    assert lineage.canonical_tool_id == "governance.query_population_metrics"
    assert lineage.canonical_tool_version == "1.0.0"
    assert lineage.area_code == "330106"
    assert lineage.output == "table"
    assert [m.metric_id for m in lineage.metric_definitions] == ["person_count"]
    assert lineage.spec_fingerprint.startswith("sha256:")
    assert lineage.plan_fingerprint.startswith("sha256:")


def test_resolver_lineage_tracks_output_form_for_frontend_commands() -> None:
    resolution = _resolver().resolve(
        _population_spec(output="choropleth"),
        auth_context=population_auth_context(),
    )

    assert isinstance(resolution, ResolvedSemanticAction)
    assert resolution.lineage.output == "choropleth"


def test_resolver_plan_passes_execution_guard_recheck() -> None:
    resolution = _resolver().resolve(
        _population_spec(), auth_context=population_auth_context()
    )

    assert isinstance(resolution, ResolvedSemanticAction)
    assert resolution.recheck is not None
    assert resolution.recheck.allowed is True
    assert resolution.recheck.denial_codes == ()


# ---------------------------------------------------------------------------
# 确定性：同义问法收敛到同一规范指纹（H 要求）
# ---------------------------------------------------------------------------


def test_resolver_fingerprints_are_deterministic_across_key_order() -> None:
    resolver = _resolver()
    first = resolver.resolve(_population_spec(), auth_context=population_auth_context())
    reordered = {
        "spec": {
            "group_by": ["street"],
            "scope": {"include_descendants": True, "area_code": "330106"},
            "metrics": ["person_count"],
            "subject": "population",
            "filters": [
                {"value": "solitary_elderly", "operator": "eq", "field": "person_category"}
            ],
        }
    }
    second = resolver.resolve(reordered, auth_context=population_auth_context())

    assert isinstance(first, ResolvedSemanticAction)
    assert isinstance(second, ResolvedSemanticAction)
    assert first.lineage.spec_fingerprint == second.lineage.spec_fingerprint
    assert first.lineage.plan_fingerprint == second.lineage.plan_fingerprint
    assert first.canonical_action == second.canonical_action


def test_resolver_different_specs_produce_different_fingerprints() -> None:
    resolver = _resolver()
    auth = population_auth_context()
    street = resolver.resolve(_population_spec(), auth_context=auth)
    community = resolver.resolve(
        _population_spec(scope={"area_code": "330106001"}, group_by=["community"]),
        auth_context=auth,
    )

    assert isinstance(street, ResolvedSemanticAction)
    assert isinstance(community, ResolvedSemanticAction)
    assert street.lineage.spec_fingerprint != community.lineage.spec_fingerprint


# ---------------------------------------------------------------------------
# Fail closed：S1-A 只绑定 population；housing/event 拒绝解析
# ---------------------------------------------------------------------------


def test_s1a_allowlist_only_binds_population() -> None:
    assert frozenset({"population"}) == S1A_BINDABLE_SUBJECTS


@pytest.mark.parametrize("subject", ["housing", "event"])
def test_resolver_rejects_catalog_subjects_outside_s1a_allowlist(subject: str) -> None:
    resolution = _resolver().resolve(
        {
            "spec": {
                "subject": subject,
                "metrics": ["dwelling_count"],
                "scope": {"area_code": "330106"},
            }
        },
        auth_context=population_auth_context(),
    )

    assert isinstance(resolution, RejectedSemanticAction)
    assert "SUBJECT_NOT_BINDABLE" in resolution.codes
    assert subject in resolution.user_message
    assert resolution.is_authorization_denial is False


def test_resolver_rejects_unknown_subject_with_catalog_violation() -> None:
    resolution = _resolver().resolve(
        {"spec": {"subject": "traffic", "metrics": ["x"], "scope": {"area_code": "330106"}}},
        auth_context=population_auth_context(),
    )

    assert isinstance(resolution, RejectedSemanticAction)
    assert "UNKNOWN_SUBJECT" in resolution.codes


# ---------------------------------------------------------------------------
# Unsupported 能力：结构化拒绝，不宣称、不放行
# ---------------------------------------------------------------------------


def test_resolver_rejects_unknown_metric() -> None:
    resolution = _resolver().resolve(
        _population_spec(metrics=["household_count"]),
        auth_context=population_auth_context(),
    )

    assert isinstance(resolution, RejectedSemanticAction)
    assert "UNKNOWN_METRIC" in resolution.codes


def test_resolver_rejects_invalid_dimension() -> None:
    resolution = _resolver().resolve(
        _population_spec(group_by=["next_area"]),
        auth_context=population_auth_context(),
    )

    assert isinstance(resolution, RejectedSemanticAction)
    assert "INVALID_GROUP_BY" in resolution.codes


def test_resolver_rejects_non_eq_operator_via_required_filter_precedence() -> None:
    # person_category 仅注册 eq；gte 同时意味着必填筛选缺失，
    # 必填筛选检查优先（真实 Adapter 白名单只接受 eq=solitary_elderly）。
    resolution = _resolver().resolve(
        _population_spec(
            filters=[
                {"field": "person_category", "operator": "gte", "value": "solitary_elderly"}
            ]
        ),
        auth_context=population_auth_context(),
    )

    assert isinstance(resolution, RejectedSemanticAction)
    assert "REQUIRED_FILTER_MISSING" in resolution.codes


def test_resolver_rejects_age_filter_as_capability_gap() -> None:
    # 《16》S1.3：真实接口不支持的年龄附加筛选必须明确拒绝，禁止伪造。
    resolution = _resolver().resolve(
        _population_spec(
            filters=[
                {"field": "person_category", "operator": "eq", "value": "solitary_elderly"},
                {"field": "age", "operator": "gte", "value": 80},
            ]
        ),
        auth_context=population_auth_context(),
    )

    assert isinstance(resolution, RejectedSemanticAction)
    assert "INVALID_FILTER_FIELD" in resolution.codes


def test_resolver_rejects_unsupported_scope_level() -> None:
    # 市级（4 位）不在 population 支持的 6/9/12 层级内。
    resolution = _resolver().resolve(
        _population_spec(scope={"area_code": "3301"}),
        auth_context=population_auth_context(),
    )

    assert isinstance(resolution, RejectedSemanticAction)
    assert "SCOPE_LEVEL_UNSUPPORTED" in resolution.codes


def test_resolver_rejects_order_by_as_unsupported() -> None:
    resolution = _resolver().resolve(
        _population_spec(order_by=[{"field": "person_count", "direction": "desc"}]),
        auth_context=population_auth_context(),
    )

    assert isinstance(resolution, RejectedSemanticAction)
    assert "INVALID_ORDER_BY" in resolution.codes


def test_resolver_rejects_time_range_as_unsupported() -> None:
    resolution = _resolver().resolve(
        _population_spec(time_range={"start": "2026-01-01", "end": "2026-06-30"}),
        auth_context=population_auth_context(),
    )

    assert isinstance(resolution, RejectedSemanticAction)
    assert "TIME_RANGE_UNSUPPORTED" in resolution.codes


def test_resolver_rejects_missing_required_solitary_elderly_filter() -> None:
    # 无筛选的人口查询在真实 Adapter 白名单之外，必须语义层即拒绝。
    resolution = _resolver().resolve(
        _population_spec(filters=[]),
        auth_context=population_auth_context(),
    )

    assert isinstance(resolution, RejectedSemanticAction)
    assert "REQUIRED_FILTER_MISSING" in resolution.codes
    assert "solitary_elderly" in resolution.user_message


def test_resolver_rejects_incompatible_spec_version() -> None:
    resolution = _resolver().resolve(
        _population_spec(schema_version="s9.9"),
        auth_context=population_auth_context(),
    )

    # schema_version 是 Literal["s0.1"]：结构层直接拒绝为输入非法。
    assert isinstance(resolution, RejectedSemanticAction)
    assert "SEMANTIC_INPUT_INVALID" in resolution.codes


# ---------------------------------------------------------------------------
# 授权反例：越权主题/数据集/区域按 denied 归类
# ---------------------------------------------------------------------------


def test_resolver_denies_unentitled_subject_as_authorization_denial() -> None:
    auth = population_auth_context().model_copy(update={"entitlements": []})
    resolution = _resolver().resolve(_population_spec(), auth_context=auth)

    assert isinstance(resolution, RejectedSemanticAction)
    assert "SUBJECT_NOT_ENTITLED" in resolution.codes
    assert resolution.is_authorization_denial is True


def test_resolver_denies_unauthorized_dataset() -> None:
    auth = population_auth_context()
    auth = auth.model_copy(
        update={
            "data_scopes": auth.data_scopes.model_copy(update={"datasets": ["housing"]})
        }
    )
    resolution = _resolver().resolve(_population_spec(), auth_context=auth)

    assert isinstance(resolution, RejectedSemanticAction)
    assert "DATASET_NOT_AUTHORIZED" in resolution.codes
    assert resolution.is_authorization_denial is True


def test_resolver_denies_area_out_of_scope() -> None:
    # 授权 330106（西湖区），请求 330108（滨江区）。
    resolution = _resolver().resolve(
        _population_spec(scope={"area_code": "330108"}),
        auth_context=population_auth_context(),
    )

    assert isinstance(resolution, RejectedSemanticAction)
    assert "AREA_OUT_OF_SCOPE" in resolution.codes
    assert resolution.is_authorization_denial is True


def test_resolver_mixed_semantic_and_auth_violations_are_failed_not_denied() -> None:
    # 语义错误优先：模型可修正 spec，不应记为权限拒绝。
    auth = population_auth_context().model_copy(update={"entitlements": []})
    resolution = _resolver().resolve(
        _population_spec(metrics=["nope"]),
        auth_context=auth,
    )

    assert isinstance(resolution, RejectedSemanticAction)
    assert "UNKNOWN_METRIC" in resolution.codes
    assert resolution.is_authorization_denial is False


def test_resolver_denies_unsupported_area_scope_even_with_descendants() -> None:
    # 授权仅含街道 330106001：其下级社区（12 位）可查，其他街道拒绝。
    auth = population_auth_context().model_copy(
        update={
            "data_scopes": population_auth_context().data_scopes.model_copy(
                update={"areas": [{"area_code": "330106001", "include_descendants": True}]}
            )
        }
    )
    resolution = _resolver().resolve(
        _population_spec(scope={"area_code": "330106002"}, group_by=["community"]),
        auth_context=auth,
    )

    assert isinstance(resolution, RejectedSemanticAction)
    assert "AREA_OUT_OF_SCOPE" in resolution.codes


# ---------------------------------------------------------------------------
# 输入结构与版本绑定反例
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw_arguments",
    [
        {},
        {"spec": {}},
        {"spec": "population"},
        {"spec": {"subject": "population", "metrics": [], "scope": {"area_code": "330106"}}},
        {
            "spec": {
                "subject": "population",
                "metrics": ["person_count"],
                "scope": {"area_code": "xihu"},
            }
        },
        {
            "spec": {
                "subject": "population",
                "metrics": ["person_count"],
                "scope": {"area_code": "33010"},
            }
        },
        {"query": {"metrics": ["person_count"]}},
    ],
)
def test_resolver_rejects_malformed_input(raw_arguments: dict[str, object]) -> None:
    resolution = _resolver().resolve(raw_arguments, auth_context=population_auth_context())

    assert isinstance(resolution, RejectedSemanticAction)
    assert "SEMANTIC_INPUT_INVALID" in resolution.codes
    assert resolution.is_authorization_denial is False


def test_resolver_rejects_spec_version_not_supported_by_catalog() -> None:
    current = SemanticCatalog.default()
    newer = SemanticCatalog(
        catalog_version=current.catalog_version,
        supported_spec_versions=("s0.2",),
        subjects=current.subjects,
        bindings=current.bindings,
    )
    resolution = _resolver(catalog=newer).resolve(
        _population_spec(), auth_context=population_auth_context()
    )

    assert isinstance(resolution, RejectedSemanticAction)
    assert "CATALOG_VERSION_INCOMPATIBLE" in resolution.codes


def test_resolver_rejects_capability_version_drift_against_registry() -> None:
    catalog = SemanticCatalog.default()
    drifted_bindings = dict(catalog.bindings)
    binding = drifted_bindings["population"]
    drifted_bindings["population"] = binding.model_copy(
        update={"capability_version": "9.9.9"}
    )
    drifted = SemanticCatalog(
        catalog_version=catalog.catalog_version,
        supported_spec_versions=catalog.supported_spec_versions,
        subjects=catalog.subjects,
        bindings=drifted_bindings,
    )
    resolution = _resolver(catalog=drifted).resolve(
        _population_spec(), auth_context=population_auth_context()
    )

    assert isinstance(resolution, DeniedSemanticAction)
    assert "CAPABILITY_VERSION_MISMATCH" in resolution.codes


def test_resolver_denies_when_area_not_authorized_at_leaf_level() -> None:
    # 授权仅 330106 本级（不含下级）：查询其下街道被拒绝。
    auth = population_auth_context()
    auth = auth.model_copy(
        update={
            "data_scopes": auth.data_scopes.model_copy(
                update={"areas": [{"area_code": "330106", "include_descendants": False}]}
            )
        }
    )
    resolution = _resolver().resolve(
        _population_spec(scope={"area_code": "330106001"}, group_by=["community"]),
        auth_context=auth,
    )

    assert isinstance(resolution, RejectedSemanticAction)
    assert "AREA_OUT_OF_SCOPE" in resolution.codes
    assert resolution.is_authorization_denial is True
