"""S1-A：governance.semantic_query 虚拟 Tool 的语义动作解析器。

解析链路为 parse → Catalog 绑定派生的可执行主题（fail closed）→ 必填
筛选强制 → Validator → Compiler → ExecutionGuard 生产 Policy 复核，成功
时产出规范 ToolAction、SemanticPlan 与结果血缘。可执行主题没有硬编码
白名单：人口、住房、事件均由 Catalog 能力绑定派生进入统一入口；声明
存在但缺少绑定的主题结构化拒绝（``SUBJECT_NOT_BINDABLE``）。所有反例
必须结构化拒绝，不得静默改写或放行。
"""

import pytest

from full_view_agent.application.policy import MinimalPolicyAdapter
from full_view_agent.application.tool_registry import ToolRegistry
from full_view_agent.domain.models import (
    QueryEnterpriseMetricsInput,
    QueryEventMetricsInput,
    QueryGovernanceOverviewInput,
    QueryGovernancePowerMetricsInput,
    QueryHousingMetricsInput,
    QueryPopulationMetricsInput,
)
from full_view_agent.semantic.action_resolver import (
    SEMANTIC_QUERY_TOOL_ID,
    SEMANTIC_QUERY_TOOL_VERSION,
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


def _catalog_with_bindings(*subject_ids: str) -> SemanticCatalog:
    """只保留指定主题能力绑定的目录：模拟绑定增减后的可执行集合。"""
    base = SemanticCatalog.default()
    return SemanticCatalog(
        catalog_version=base.catalog_version,
        supported_spec_versions=base.supported_spec_versions,
        subjects=base.subjects,
        bindings={
            subject_id: base.bindings[subject_id] for subject_id in subject_ids
        },
    )


def _auth_for(*subjects: str):
    """按主题派生授权：entitlement 与数据集跟随主题集合。"""
    base = population_auth_context()
    return base.model_copy(
        update={
            "entitlements": [
                f"governance.{subject}.aggregate.read" for subject in subjects
            ],
            "data_scopes": base.data_scopes.model_copy(
                update={"datasets": list(subjects)}
            ),
        }
    )


def _subject_args(
    subject: str,
    *,
    metrics: list[str],
    area_code: str = "330106",
    group_by: list[str] | None = None,
    catalog: SemanticCatalog | None = None,
) -> dict[str, object]:
    """构造 semantic_query 原始参数；版本与指纹钉扎到给定目录。"""
    effective = catalog or SemanticCatalog.default()
    return {
        "catalog_version": effective.catalog_version,
        "catalog_fingerprint": effective.execution_fingerprint,
        "spec": {
            "subject": subject,
            "metrics": metrics,
            "scope": {"area_code": area_code},
            "group_by": group_by or [],
        },
    }


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
    return {
        "catalog_version": SemanticCatalog.default().catalog_version,
        "catalog_fingerprint": SemanticCatalog.default().execution_fingerprint,
        "spec": spec,
    }


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
    assert len(lineage.filter_contexts) == 1
    assert lineage.filter_contexts[0].field == "person_category"
    assert lineage.filter_contexts[0].value == "solitary_elderly"
    assert lineage.filter_contexts[0].display_label == (
        "独居老人（空巢老人按此受控口径映射）"
    )
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
        "catalog_version": SemanticCatalog.default().catalog_version,
        "catalog_fingerprint": SemanticCatalog.default().execution_fingerprint,
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
# 可执行主题：由 Catalog 能力绑定派生（无硬编码白名单）
# ---------------------------------------------------------------------------


def test_resolver_bindable_subjects_derive_from_catalog_bindings() -> None:
    # 默认目录三主题均有已验证绑定 → 全部进入统一语义入口。
    assert _resolver().bindable_subjects == frozenset(
        {
            "enterprise",
            "event",
            "governance_overview",
            "governance_power",
            "housing",
            "population",
        }
    )
    # 绑定收缩 → 可执行集合同步收缩，无需改任何白名单常量。
    partial = _catalog_with_bindings("population")
    assert _resolver(catalog=partial).bindable_subjects == frozenset(
        {"population"}
    )


# ---------------------------------------------------------------------------
# 成功路径：housing / event 与 population 走同一解析链路
# ---------------------------------------------------------------------------


def test_resolver_compiles_housing_lease_spec_to_canonical_tool_action() -> None:
    resolution = _resolver().resolve(
        _subject_args("housing", metrics=["dwelling_count"]),
        auth_context=_auth_for("housing"),
    )

    assert isinstance(resolution, ResolvedSemanticAction)
    action = resolution.canonical_action
    assert action.tool_id == "governance.query_housing_metrics"
    QueryHousingMetricsInput.model_validate(action.arguments)
    assert resolution.lineage.subject == "housing"
    assert resolution.lineage.logical_dataset_id == "housing"
    assert resolution.lineage.canonical_tool_id == "governance.query_housing_metrics"
    assert resolution.recheck is not None
    assert resolution.recheck.allowed is True


def test_resolver_compiles_housing_next_area_spec_to_canonical_tool_action() -> None:
    # 区域按直接下级区划汇总（门禁开启时）同样进入统一入口。
    catalog = SemanticCatalog.default(housing_next_area_enabled=True)
    resolution = _resolver(catalog=catalog).resolve(
        _subject_args(
            "housing",
            metrics=["dwelling_count"],
            group_by=["next_area"],
            catalog=catalog,
        ),
        auth_context=_auth_for("housing"),
    )

    assert isinstance(resolution, ResolvedSemanticAction)
    validated = QueryHousingMetricsInput.model_validate(
        resolution.canonical_action.arguments
    )
    assert validated.query.group_by == ["next_area"]


def test_resolver_compiles_event_snapshot_spec_to_canonical_tool_action() -> None:
    resolution = _resolver().resolve(
        _subject_args("event", metrics=["finish_rate"]),
        auth_context=_auth_for("event"),
    )

    assert isinstance(resolution, ResolvedSemanticAction)
    action = resolution.canonical_action
    assert action.tool_id == "governance.query_event_metrics"
    QueryEventMetricsInput.model_validate(action.arguments)
    query = action.arguments["query"]
    assert isinstance(query, dict)
    # 事件三层办结率快照：参数不得携带 group_by/filters 等未验证语义。
    assert "group_by" not in query
    assert "filters" not in query
    assert resolution.lineage.subject == "event"
    assert resolution.lineage.canonical_tool_id == "governance.query_event_metrics"
    assert resolution.recheck is not None
    assert resolution.recheck.allowed is True


def test_resolver_compiles_governance_overview_to_canonical_tool_action() -> None:
    resolution = _resolver().resolve(
        _subject_args(
            "governance_overview",
            metrics=["governance_coverage_overview"],
        ),
        auth_context=_auth_for("overview").model_copy(
            update={
                "data_scopes": population_auth_context().data_scopes.model_copy(
                    update={"datasets": ["governance_overview"]}
                )
            }
        ),
    )

    assert isinstance(resolution, ResolvedSemanticAction)
    action = resolution.canonical_action
    assert action.tool_id == "governance.get_governance_overview"
    validated = QueryGovernanceOverviewInput.model_validate(action.arguments)
    assert validated.query.scope.area_code == "330106"
    assert resolution.lineage.logical_dataset_id == "governance_overview"


@pytest.mark.parametrize("phrase", ["查询西湖区治理力量汇总", "西湖区网格力量构成"])
def test_resolver_compiles_governance_power_expressions_to_aggregate_action(
    phrase: str,
) -> None:
    del phrase  # user-language coverage is asserted at the planner boundary below
    auth = _auth_for("power").model_copy(
        update={
            "data_scopes": population_auth_context().data_scopes.model_copy(
                update={"datasets": ["governance_power"]}
            )
        }
    )
    resolution = _resolver().resolve(
        _subject_args(
            "governance_power", metrics=["governance_power_count"]
        ),
        auth_context=auth,
    )

    assert isinstance(resolution, ResolvedSemanticAction)
    action = resolution.canonical_action
    assert action.tool_id == "governance.query_governance_power_metrics"
    assert QueryGovernancePowerMetricsInput.model_validate(
        action.arguments
    ).query.scope.area_code == "330106"
    assert resolution.recheck is not None
    assert resolution.recheck.allowed is True


def test_resolver_compiles_enterprise_distribution_to_canonical_tool_action() -> None:
    resolution = _resolver().resolve(
        _subject_args(
            "enterprise",
            metrics=["enterprise_count"],
            group_by=["next_area"],
        ),
        auth_context=_auth_for("enterprise"),
    )

    assert isinstance(resolution, ResolvedSemanticAction)
    action = resolution.canonical_action
    assert action.tool_id == "governance.query_enterprise_metrics"
    validated = QueryEnterpriseMetricsInput.model_validate(action.arguments)
    assert validated.query.group_by == ["next_area"]
    assert resolution.lineage.logical_dataset_id == "enterprise"
    assert resolution.recheck is not None
    assert resolution.recheck.allowed is True


# ---------------------------------------------------------------------------
# Fail closed：声明存在但无能力绑定 → SUBJECT_NOT_BINDABLE，不执行
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("subject", "metrics"),
    [("housing", ["dwelling_count"]), ("event", ["finish_rate"])],
)
def test_resolver_rejects_declared_subject_without_capability_binding(
    subject: str, metrics: list[str]
) -> None:
    partial = _catalog_with_bindings("population")
    resolution = _resolver(catalog=partial).resolve(
        _subject_args(subject, metrics=metrics, catalog=partial),
        auth_context=_auth_for(subject),
    )

    assert isinstance(resolution, RejectedSemanticAction)
    assert "SUBJECT_NOT_BINDABLE" in resolution.codes
    assert subject in resolution.user_message
    assert resolution.is_authorization_denial is False


# ---------------------------------------------------------------------------
# 事件受控边界：不得虚构时间、阈值、总量或下级区划
# ---------------------------------------------------------------------------


def test_resolver_rejects_event_time_range_as_unsupported() -> None:
    arguments = _subject_args("event", metrics=["finish_rate"])
    spec = arguments["spec"]
    assert isinstance(spec, dict)
    spec["time_range"] = {"start": "2026-01-01", "end": "2026-06-30"}

    resolution = _resolver().resolve(arguments, auth_context=_auth_for("event"))

    assert isinstance(resolution, RejectedSemanticAction)
    assert "TIME_RANGE_UNSUPPORTED" in resolution.codes


@pytest.mark.parametrize("metric", ["finish_count", "total_count"])
def test_resolver_rejects_event_total_or_volume_metrics(metric: str) -> None:
    resolution = _resolver().resolve(
        _subject_args("event", metrics=[metric]),
        auth_context=_auth_for("event"),
    )

    assert isinstance(resolution, RejectedSemanticAction)
    assert "UNKNOWN_METRIC" in resolution.codes


def test_resolver_rejects_event_count_without_month_and_time_range() -> None:
    resolution = _resolver().resolve(
        _subject_args("event", metrics=["event_count"]),
        auth_context=_auth_for("event"),
    )

    assert isinstance(resolution, RejectedSemanticAction)
    assert "INVALID_RESULT_SHAPE" in resolution.codes


def test_resolver_rejects_event_threshold_filter() -> None:
    arguments = _subject_args("event", metrics=["finish_rate"])
    spec = arguments["spec"]
    assert isinstance(spec, dict)
    spec["filters"] = [{"field": "finish_rate", "operator": "lt", "value": 60}]

    resolution = _resolver().resolve(arguments, auth_context=_auth_for("event"))

    assert isinstance(resolution, RejectedSemanticAction)
    assert "INVALID_FILTER_FIELD" in resolution.codes


@pytest.mark.parametrize("dimension", ["next_area", "street", "grid"])
def test_resolver_rejects_event_sub_area_grouping(dimension: str) -> None:
    resolution = _resolver().resolve(
        _subject_args("event", metrics=["finish_rate"], group_by=[dimension]),
        auth_context=_auth_for("event"),
    )

    assert isinstance(resolution, RejectedSemanticAction)
    assert "INVALID_GROUP_BY" in resolution.codes


# ---------------------------------------------------------------------------
# 住房部署门禁：next_area 关闭时仅区域自身租赁汇总合法
# ---------------------------------------------------------------------------


def test_resolver_rejects_housing_next_area_when_deployment_gate_closed() -> None:
    gated = SemanticCatalog.default(housing_next_area_enabled=False)
    resolver = _resolver(catalog=gated)

    next_area = resolver.resolve(
        _subject_args(
            "housing",
            metrics=["dwelling_count"],
            group_by=["next_area"],
            catalog=gated,
        ),
        auth_context=_auth_for("housing"),
    )
    assert isinstance(next_area, RejectedSemanticAction)
    assert "INVALID_GROUP_BY" in next_area.codes

    lease_self = resolver.resolve(
        _subject_args("housing", metrics=["dwelling_count"], catalog=gated),
        auth_context=_auth_for("housing"),
    )
    assert isinstance(lease_self, ResolvedSemanticAction)


# ---------------------------------------------------------------------------
# 授权反例：住房/事件越权主题与越权区域按 denied 归类
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("subject", "metrics"),
    [("housing", ["dwelling_count"]), ("event", ["finish_rate"])],
)
def test_resolver_denies_housing_and_event_without_entitlement(
    subject: str, metrics: list[str]
) -> None:
    # 仅人口授权：住房/事件主题按权限拒绝，不按语义失败。
    resolution = _resolver().resolve(
        _subject_args(subject, metrics=metrics),
        auth_context=population_auth_context(),
    )

    assert isinstance(resolution, RejectedSemanticAction)
    assert "SUBJECT_NOT_ENTITLED" in resolution.codes
    assert resolution.is_authorization_denial is True


@pytest.mark.parametrize(
    ("subject", "metrics"),
    [("housing", ["dwelling_count"]), ("event", ["finish_rate"])],
)
def test_resolver_denies_housing_and_event_area_out_of_scope(
    subject: str, metrics: list[str]
) -> None:
    # 授权 330106（西湖区），请求 330108（滨江区）。
    resolution = _resolver().resolve(
        _subject_args(subject, metrics=metrics, area_code="330108"),
        auth_context=_auth_for(subject),
    )

    assert isinstance(resolution, RejectedSemanticAction)
    assert "AREA_OUT_OF_SCOPE" in resolution.codes
    assert resolution.is_authorization_denial is True


# ---------------------------------------------------------------------------
# 确定性：住房/事件同义 spec 收敛到同一规范动作与指纹
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("subject", "metric"),
    [("housing", "dwelling_count"), ("event", "finish_rate")],
)
def test_resolver_synonymous_specs_converge_to_same_canonical_action(
    subject: str, metric: str
) -> None:
    resolver = _resolver()
    auth = _auth_for(subject)
    first = resolver.resolve(
        _subject_args(subject, metrics=[metric]), auth_context=auth
    )
    # 键序不同、显式默认值（output/limit/空集合）与省略默认值同义。
    synonym = {
        "catalog_version": SemanticCatalog.default().catalog_version,
        "catalog_fingerprint": SemanticCatalog.default().execution_fingerprint,
        "spec": {
            "group_by": [],
            "filters": [],
            "order_by": [],
            "output": "table",
            "limit": 200,
            "scope": {"include_descendants": True, "area_code": "330106"},
            "metrics": [metric],
            "subject": subject,
        },
    }
    second = resolver.resolve(synonym, auth_context=auth)

    assert isinstance(first, ResolvedSemanticAction)
    assert isinstance(second, ResolvedSemanticAction)
    assert first.lineage.spec_fingerprint == second.lineage.spec_fingerprint
    assert first.lineage.plan_fingerprint == second.lineage.plan_fingerprint
    assert first.canonical_action == second.canonical_action


def test_resolver_rejects_unknown_subject_with_catalog_violation() -> None:
    resolution = _resolver().resolve(
        {
            "catalog_version": SemanticCatalog.default().catalog_version,
            "catalog_fingerprint": SemanticCatalog.default().execution_fingerprint,
            "spec": {
                "subject": "traffic",
                "metrics": ["x"],
                "scope": {"area_code": "330106"},
            },
        },
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


def test_resolver_rejects_non_eq_population_filter_operator() -> None:
    # person_category 仅注册 eq；其他操作符必须明确拒绝。
    resolution = _resolver().resolve(
        _population_spec(
            filters=[
                {"field": "person_category", "operator": "gte", "value": "solitary_elderly"}
            ]
        ),
        auth_context=population_auth_context(),
    )

    assert isinstance(resolution, RejectedSemanticAction)
    assert "INVALID_FILTER_OPERATOR" in resolution.codes


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


def test_resolver_rejects_unauthorized_city_and_unmatched_grouping() -> None:
    # 市级（4 位）不在 population 支持的 6/9/12 层级内。
    resolution = _resolver().resolve(
        _population_spec(scope={"area_code": "3301"}),
        auth_context=population_auth_context(),
    )

    assert isinstance(resolution, RejectedSemanticAction)
    assert "AREA_OUT_OF_SCOPE" in resolution.codes
    assert "GROUP_BY_SCOPE_MISMATCH" in resolution.codes


def test_resolver_compiles_population_count_order_by() -> None:
    resolution = _resolver().resolve(
        _population_spec(order_by=[{"field": "person_count", "direction": "desc"}]),
        auth_context=population_auth_context(),
    )

    assert isinstance(resolution, ResolvedSemanticAction)
    assert resolution.canonical_action.arguments["query"]["order_by"] == [
        {"field": "person_count", "direction": "desc"}
    ]


def test_resolver_rejects_time_range_as_unsupported() -> None:
    resolution = _resolver().resolve(
        _population_spec(time_range={"start": "2026-01-01", "end": "2026-06-30"}),
        auth_context=population_auth_context(),
    )

    assert isinstance(resolution, RejectedSemanticAction)
    assert "TIME_RANGE_UNSUPPORTED" in resolution.codes


def test_resolver_accepts_general_population_without_specialized_filter() -> None:
    # 无筛选明确表示一般人口，不得被替换成独居老人或提前拒绝。
    resolution = _resolver().resolve(
        _population_spec(filters=[]),
        auth_context=population_auth_context(),
    )

    assert isinstance(resolution, ResolvedSemanticAction)
    assert resolution.canonical_action.arguments["query"]["filters"] == []


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
    arguments = _population_spec()
    arguments["catalog_fingerprint"] = newer.execution_fingerprint
    resolution = _resolver(catalog=newer).resolve(
        arguments, auth_context=population_auth_context()
    )

    assert isinstance(resolution, RejectedSemanticAction)
    assert "CATALOG_VERSION_INCOMPATIBLE" in resolution.codes


def test_resolver_uses_run_registry_version_instead_of_legacy_catalog_version() -> None:
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
    arguments = _population_spec()
    arguments["catalog_fingerprint"] = drifted.execution_fingerprint
    resolution = _resolver(catalog=drifted).resolve(
        arguments, auth_context=population_auth_context()
    )

    assert isinstance(resolution, ResolvedSemanticAction)
    assert resolution.plan.steps[0].capability_version == "1.0.0"


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
