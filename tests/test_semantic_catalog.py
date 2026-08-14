"""S0 语义内核候选：Catalog 真实性、模型视图物理隐蔽与生产边界测试。

Catalog 只声明当前生产 HTTP Adapter 已逐项验证的能力（以 Adapter 白名单
校验函数和生产 Registry manifest 为证）；模型可见视图由权限过滤后的
Catalog 派生，不含物理表、端点、列名或 adapter 映射；本候选不接入生产
Tool Registry。
"""

import inspect
import json

import pytest
from pydantic import ValidationError

from full_view_agent.application.capability_service import TOOL_INPUT_MODELS
from full_view_agent.application.errors import ResourceNotFound
from full_view_agent.application.tool_registry import ToolRegistry
from full_view_agent.domain.models import (
    AuthorizedAreaScope,
    QueryHousingMetricsInput,
    QueryPopulationMetricsInput,
)
from full_view_agent.infrastructure.governance_adapter import (
    _validate_housing_next_area_query,
    _validate_population_query,
)
from full_view_agent.semantic import (
    CapabilityBinding,
    SemanticCatalog,
    SemanticFilter,
    SemanticQuerySpec,
    SubjectAuthorization,
)
from full_view_agent.semantic.catalog import ResultShape

# 模型可见面（能力视图、QuerySpec）中不得出现的物理实现词元。
PHYSICAL_TOKENS = (
    "adapter://",
    "geo-qxst",
    "http://",
    "https://",
    "getNextSiteData",
    "getRoomLeaseType",
    "getEventPropertiesAndConflictsByTotal",
    "getAreaInfoByAreaName",
    "getHouseDetails",
    "base_room_lease",
    "dm_empty_nest_old",
    "tableName",
    "areaCode",
    "gridFinishRate",
    "house_type",
)

FULL_AUTH = SubjectAuthorization(
    entitlements=(
        "governance.area.read",
        "governance.event.aggregate.read",
        "governance.housing.aggregate.read",
        "governance.population.aggregate.read",
    ),
    datasets=("administrative_area", "event", "housing", "population"),
    area_scopes=(AuthorizedAreaScope(area_code="3301", include_descendants=True),),
    field_policy_set="governance_analyst_v1",
)


@pytest.fixture
def catalog() -> SemanticCatalog:
    # 本 fixture 验证完整已部署能力；next_area 必须显式开启，避免测试
    # 无意中改变生产默认门禁。
    return SemanticCatalog.default(
        housing_next_area_enabled=True,
        event_category_enabled=True,
    )


# ---------------------------------------------------------------------------
# 主题与能力声明：只覆盖当前真实 HTTP 已验证的范围
# ---------------------------------------------------------------------------


def test_catalog_declares_exactly_the_verified_subjects(
    catalog: SemanticCatalog,
) -> None:
    assert sorted(catalog.subject_ids()) == [
        "enterprise",
        "event",
        "governance_overview",
        "governance_power",
        "housing",
        "population",
    ]
    assert catalog.catalog_version.endswith("-s0-candidate")
    assert catalog.supported_spec_versions == ("s0.1",)


def test_population_only_declares_verified_capabilities(
    catalog: SemanticCatalog,
) -> None:
    subject = catalog.require_subject("population")

    assert [metric.metric_id for metric in subject.metrics] == ["person_count"]
    assert [f.field for f in subject.filters] == ["person_category"]
    person_category = subject.filters[0]
    assert person_category.operators == ("eq",)
    assert person_category.allowed_values == ("solitary_elderly",)
    assert {rule.value for rule in subject.group_by_rules} == {
        "district",
        "descendant_street",
        "descendant_community",
        "street",
        "community",
        "grid",
    }
    assert subject.scope_levels == (4, 6, 9, 12)
    assert subject.min_group_by == 1
    assert subject.max_group_by == 1
    assert subject.supports_order_by is True
    assert subject.supports_time_range is False
    assert subject.output_forms == ("table", "choropleth")
    # 旧草稿提前宣称的 gender/age_band/age 能力不得出现。
    serialized = subject.model_dump_json()
    assert "gender" not in serialized
    assert "age_band" not in serialized
    assert '"age"' not in serialized


def test_housing_declares_lease_room_use_and_next_area(
    catalog: SemanticCatalog,
) -> None:
    subject = catalog.require_subject("housing")

    assert subject.filters == ()
    assert [rule.value for rule in subject.group_by_rules] == [
        "room_use",
        "next_area",
        "descendant_street",
    ]
    room_use, next_area, descendant_street = subject.group_by_rules
    assert room_use.allowed_scope_levels == (4, 6, 9, 12, 15)
    assert next_area.allowed_scope_levels == (4, 6, 9, 12)
    assert descendant_street.allowed_scope_levels == (4,)
    assert subject.min_group_by == 0
    assert subject.max_group_by == 1
    assert subject.supports_time_range is False
    assert subject.output_forms == ("table",)
    refs = {shape.data_schema_ref for shape in subject.result_shapes}
    assert refs == {
        "schema://data/housing-lease-type-table/1.0.0",
        "schema://data/housing-area-group-table/1.0.0",
        "schema://data/housing-room-use-table/1.0.0",
        "schema://data/housing-stock-overview/1.0.0",
    }


def test_default_catalog_keeps_housing_next_area_gate_closed() -> None:
    catalog = SemanticCatalog.default()
    housing = catalog.require_subject("housing")

    assert [rule.value for rule in housing.group_by_rules] == ["room_use"]
    assert housing.max_group_by == 1
    assert [shape.shape_id for shape in housing.result_shapes] == [
        "housing_lease_type_table",
        "housing_room_use_table",
        "housing_stock_overview",
    ]


def test_event_declares_verified_snapshot_trend_and_category_shapes(
    catalog: SemanticCatalog,
) -> None:
    subject = catalog.require_subject("event")

    assert [metric.metric_id for metric in subject.metrics] == [
        "finish_rate",
        "event_count",
    ]
    assert [rule.value for rule in subject.group_by_rules] == [
        "month",
        "event_category",
    ]
    assert subject.filters == ()
    assert subject.max_group_by == 1
    assert subject.supports_time_range is True
    assert subject.output_forms == ("table",)
    assert [shape.data_schema_ref for shape in subject.result_shapes] == [
        "schema://data/event-finish-rate-table/1.0.0",
        "schema://data/event-trend-table/1.0.0",
        "schema://data/event-category-table/1.0.0",
    ]


def test_enterprise_declares_verified_area_and_type_aggregations() -> None:
    subject = SemanticCatalog.default().require_subject("enterprise")

    assert [metric.metric_id for metric in subject.metrics] == ["enterprise_count"]
    assert [rule.value for rule in subject.group_by_rules] == [
        "next_area",
        "enterprise_type",
        "enterprise_scale",
        "industry_name",
    ]
    assert subject.group_by_rules[0].allowed_scope_levels == (4, 6, 9, 12)
    assert subject.group_by_rules[1].allowed_scope_levels == (4, 6, 9, 12, 15)
    assert subject.group_by_rules[2].allowed_scope_levels == (4, 6, 9, 12, 15)
    assert subject.min_group_by == 1
    assert subject.max_group_by == 1
    assert subject.filters == ()
    assert subject.output_forms == ("table", "choropleth")
    assert subject.include_in_analysis_overview is False


def test_governance_overview_declares_only_verified_snapshot() -> None:
    subject = SemanticCatalog.default().require_subject("governance_overview")

    assert [metric.metric_id for metric in subject.metrics] == [
        "governance_coverage_overview"
    ]
    assert subject.group_by_rules == ()
    assert subject.filters == ()
    assert subject.max_group_by == 0
    assert subject.supports_time_range is False
    assert subject.output_forms == ("table", "metric_card")
    assert subject.include_in_analysis_overview is False
    assert [shape.data_schema_ref for shape in subject.result_shapes] == [
        "schema://data/governance-overview-table/1.0.0"
    ]


def test_governance_power_declares_aggregate_only_contract() -> None:
    subject = SemanticCatalog.default().require_subject("governance_power")

    assert [metric.metric_id for metric in subject.metrics] == [
        "governance_power_count"
    ]
    assert subject.group_by_rules == ()
    assert subject.filters == ()
    assert subject.max_group_by == 0
    assert subject.required_user_terms == ("治理力量", "网格力量")
    assert subject.output_forms == ("table",)
    assert subject.include_in_analysis_overview is False
    assert [shape.data_schema_ref for shape in subject.result_shapes] == [
        "schema://data/governance-power-metric-table/1.0.0"
    ]


def test_analysis_overview_excludes_cross_domain_snapshot_subject() -> None:
    assert SemanticCatalog.default().analysis_overview_subject_ids() == [
        "event",
        "housing",
        "population",
    ]


def test_result_schemas_are_distinct_per_subject(catalog: SemanticCatalog) -> None:
    refs = {
        shape.data_schema_ref
        for subject_id in catalog.subject_ids()
        for shape in catalog.require_subject(subject_id).result_shapes
    }
    assert len(refs) == 15


# ---------------------------------------------------------------------------
# 可执行主题：由内部能力绑定派生，而非硬编码白名单
# ---------------------------------------------------------------------------


def test_bindable_subject_ids_derive_from_capability_bindings(
    catalog: SemanticCatalog,
) -> None:
    # 所有主题均存在已验证能力绑定 → 均可进入语义入口解析链路。
    assert catalog.bindable_subject_ids() == frozenset(
        {
            "enterprise",
            "event",
            "governance_overview",
            "governance_power",
            "housing",
            "population",
        }
    )


def test_bindable_subject_ids_follow_binding_subset() -> None:
    base = SemanticCatalog.default()
    partial = SemanticCatalog(
        catalog_version=base.catalog_version,
        supported_spec_versions=base.supported_spec_versions,
        subjects=base.subjects,
        bindings={"population": base.bindings["population"]},
    )

    # 声明存在但没有绑定的主题（housing/event）不在可执行集合内。
    assert partial.bindable_subject_ids() == frozenset({"population"})


def test_gate_closed_housing_stays_bindable_without_next_area() -> None:
    # 部署门禁关闭 next_area 只移除分组声明，不等于撤销主题能力绑定：
    # 住房区域自身租赁汇总仍是已验证能力，主题保持在可执行集合内。
    gated = SemanticCatalog.default(housing_next_area_enabled=False)
    assert "housing" in gated.bindable_subject_ids()
    assert [
        rule.value for rule in gated.require_subject("housing").group_by_rules
    ] == ["room_use"]
    assert gated.require_subject("housing").max_group_by == 1


# ---------------------------------------------------------------------------
# 模型面入口唯一性：绑定即由统一语义入口承载
# ---------------------------------------------------------------------------


def test_capability_binding_has_no_model_takeover_fork() -> None:
    with pytest.raises(ValidationError):
        CapabilityBinding.model_validate(
            {
                "capability_id": "governance.query_population_metrics",
                "capability_version": "1.0.0",
                "adapter_ref": "adapter://test/population",
                "model_takeover": True,
            }
        )


# ---------------------------------------------------------------------------
# 模型可见视图：物理隐蔽 + 权限过滤
# ---------------------------------------------------------------------------


def test_model_view_excludes_physical_implementation(
    catalog: SemanticCatalog,
) -> None:
    view = catalog.model_capability_view(FULL_AUTH)

    serialized = view.model_dump_json()
    for token in PHYSICAL_TOKENS:
        assert token not in serialized
    assert "adapter" not in serialized
    assert view.catalog_version == catalog.catalog_version
    assert [subject.subject_id for subject in view.subjects] == [
        "event",
        "housing",
        "population",
    ]


def test_model_view_filters_subjects_by_entitlement_and_dataset(
    catalog: SemanticCatalog,
) -> None:
    population_only = SubjectAuthorization(
        entitlements=("governance.population.aggregate.read",),
        datasets=("population",),
        area_scopes=(AuthorizedAreaScope(area_code="3301"),),
        field_policy_set="governance_analyst_v1",
    )
    view = catalog.model_capability_view(population_only)
    assert [subject.subject_id for subject in view.subjects] == ["population"]

    # 有主题授权但数据集未授权 → 该主题不可见。
    missing_dataset = SubjectAuthorization(
        entitlements=(
            "governance.population.aggregate.read",
            "governance.housing.aggregate.read",
        ),
        datasets=("population",),
        area_scopes=(AuthorizedAreaScope(area_code="3301"),),
        field_policy_set="governance_analyst_v1",
    )
    view = catalog.model_capability_view(missing_dataset)
    assert [subject.subject_id for subject in view.subjects] == ["population"]


# ---------------------------------------------------------------------------
# 模型可见视图：fail closed（未授权/无区域授权不得泄露主题）
# ---------------------------------------------------------------------------


def test_model_view_requires_explicit_authorization(catalog: SemanticCatalog) -> None:
    # 授权参数不得有默认值：模型能力视图必须由显式授权派生。
    signature = inspect.signature(SemanticCatalog.model_capability_view)
    assert signature.parameters["authorization"].default is inspect.Parameter.empty
    # 显式 None 也不得展示全部主题（fail closed）。
    assert catalog.model_capability_view(None).subjects == ()


def test_model_view_without_area_scopes_hides_all_subjects(
    catalog: SemanticCatalog,
) -> None:
    # 授权面齐全但没有任何区域授权 → 所有主题不可见。
    no_areas = SubjectAuthorization(
        entitlements=FULL_AUTH.entitlements,
        datasets=FULL_AUTH.datasets,
        area_scopes=(),
        field_policy_set="governance_analyst_v1",
    )
    assert catalog.model_capability_view(no_areas).subjects == ()


def test_model_view_grid_only_authorization_hides_population(
    catalog: SemanticCatalog,
) -> None:
    grid_only = SubjectAuthorization(
        entitlements=FULL_AUTH.entitlements,
        datasets=FULL_AUTH.datasets,
        area_scopes=(AuthorizedAreaScope(area_code="330106001001001"),),
        field_policy_set="governance_analyst_v1",
    )
    view = catalog.model_capability_view(grid_only)
    # 网格（15 位）本级仅 housing/event 有真实 scope 能力，其下再无可支持
    # 层级 → population 不可见。
    assert [subject.subject_id for subject in view.subjects] == ["event", "housing"]
    # 部分可见的过滤视图同样不含物理实现。
    serialized = view.model_dump_json()
    for token in PHYSICAL_TOKENS:
        assert token not in serialized


def test_model_view_city_without_descendants_hides_population(
    catalog: SemanticCatalog,
) -> None:
    city_self_only = SubjectAuthorization(
        entitlements=FULL_AUTH.entitlements,
        datasets=FULL_AUTH.datasets,
        area_scopes=(AuthorizedAreaScope(area_code="3301", include_descendants=False),),
        field_policy_set="governance_analyst_v1",
    )
    view = catalog.model_capability_view(city_self_only)
    # 市级（4 位）不在 population 的 scope_levels；不含下级时无可支持层级。
    assert [subject.subject_id for subject in view.subjects] == ["event", "housing"]


def test_model_view_district_self_level_keeps_population(
    catalog: SemanticCatalog,
) -> None:
    district_self_only = SubjectAuthorization(
        entitlements=FULL_AUTH.entitlements,
        datasets=FULL_AUTH.datasets,
        area_scopes=(
            AuthorizedAreaScope(area_code="330106", include_descendants=False),
        ),
        field_policy_set="governance_analyst_v1",
    )
    view = catalog.model_capability_view(district_self_only)
    # 区县（6 位）本级就在 population 的 scope_levels 内 → 无需下级也可见。
    assert [subject.subject_id for subject in view.subjects] == [
        "event",
        "housing",
        "population",
    ]


def test_model_view_exposes_scope_levels_and_group_by_levels(
    catalog: SemanticCatalog,
) -> None:
    view = catalog.model_capability_view(FULL_AUTH)
    by_id = {subject.subject_id: subject for subject in view.subjects}

    population = by_id["population"]
    assert population.scope_levels == (4, 6, 9, 12)
    assert {rule.value: rule.allowed_scope_levels for rule in population.group_by} == {
        "district": (4,),
        "descendant_street": (4,),
        "descendant_community": (4,),
        "street": (6,),
        "community": (9,),
        "grid": (12,),
    }

    housing = by_id["housing"]
    assert housing.scope_levels == (4, 6, 9, 12, 15)
    assert {rule.value: rule.allowed_scope_levels for rule in housing.group_by} == {
        "room_use": (4, 6, 9, 12, 15),
        "next_area": (4, 6, 9, 12),
        "descendant_street": (4,),
    }

    assert by_id["event"].scope_levels == (4, 6, 9, 12, 15)
    assert {
        rule.value: rule.allowed_scope_levels for rule in by_id["event"].group_by
    } == {
        "month": (4, 6, 9, 12, 15),
        "event_category": (4, 6, 9, 12, 15),
    }


def test_model_view_exposes_safe_catalog_derived_result_grains(
    catalog: SemanticCatalog,
) -> None:
    view = catalog.model_capability_view(FULL_AUTH)
    housing = next(
        subject for subject in view.subjects if subject.subject_id == "housing"
    )

    assert [rule.model_dump(mode="json") for rule in housing.group_by] == [
        {
            "value": "room_use",
            "label": "户室用途",
            "allowed_scope_levels": [4, 6, 9, 12, 15],
        },
        {
            "value": "next_area",
            "label": "直接下级区划",
            "allowed_scope_levels": [4, 6, 9, 12],
        },
        {
            "value": "descendant_street",
            "label": "全市所有街道",
            "allowed_scope_levels": [4],
        },
    ]
    assert [shape.model_dump(mode="json") for shape in housing.result_shapes] == [
        {
            "group_by_selection": [],
            "metric_selection": ["dwelling_count"],
            "grain_label": "按租赁类型汇总",
        },
        {
            "group_by_selection": ["next_area"],
            "metric_selection": ["dwelling_count"],
            "grain_label": "按直接下级区划汇总（结果按出租房数量从高到低返回）",
        },
        {
            "group_by_selection": ["descendant_street"],
            "metric_selection": ["dwelling_count"],
            "grain_label": "全市所有街道汇总（按出租房数量从高到低返回）",
        },
        {
            "group_by_selection": ["room_use"],
            "metric_selection": ["dwelling_count"],
            "grain_label": "按户室用途分类汇总",
        },
        {
            "group_by_selection": [],
            "metric_selection": ["building_count", "room_count"],
            "grain_label": "区域房屋存量总览（楼幢总数与户室总数）",
        },
    ]

    serialized = housing.model_dump_json()
    assert "row_fields" not in serialized
    assert "data_schema_ref" not in serialized
    assert "schema://" not in serialized
    for token in PHYSICAL_TOKENS:
        assert token not in serialized


def test_result_shape_display_label_is_backward_compatible_and_safe() -> None:
    legacy = ResultShape(
        shape_id="legacy_table",
        data_schema_ref="schema://internal/legacy-table/1.0",
        row_fields=("internal_column",),
    )
    assert legacy.grain_label is None
    assert ResultShape(
        shape_id="safe_business_table",
        data_schema_ref="schema://internal/safe-business-table/1.0",
        grain_label="按租赁类型汇总",
        row_fields=("internal_column",),
    ).grain_label == "按租赁类型汇总"

    for unsafe_label in (
        "adapter://private/source",
        "https://internal.example/data",
        "schema://data/private",
        "SELECT secret_column FROM dm_private_table",
        r"C:\private\table.csv",
        "adapter_ref",
        "schema_ref",
        "SELECT_secret_FROM_dm_table",
        "dm_private_table",
        "AdApTeR-ReF",
        "ScHeMa-ReF",
        "SeLeCt-secret-FrOm-dm-table",
        "DM-PRIVATE-TABLE",
    ):
        with pytest.raises(ValidationError, match="business display text"):
            ResultShape(
                shape_id="unsafe_table",
                data_schema_ref="schema://internal/unsafe-table/1.0",
                grain_label=unsafe_label,
                row_fields=("internal_column",),
            )


def test_display_only_grain_label_does_not_change_execution_fingerprint() -> None:
    base = SemanticCatalog.default()
    housing = base.require_subject("housing")
    relabeled = housing.model_copy(
        update={
            "result_shapes": tuple(
                shape.model_copy(
                    update={"grain_label": "按另一种安全业务标签展示"}
                )
                for shape in housing.result_shapes
            )
        }
    )
    changed = SemanticCatalog(
        catalog_version=base.catalog_version,
        supported_spec_versions=base.supported_spec_versions,
        subjects={**base.subjects, "housing": relabeled},
        bindings=base.bindings,
    )

    assert changed.execution_fingerprint == base.execution_fingerprint


# ---------------------------------------------------------------------------
# 内部绑定：与生产 Registry manifest 逐项一致（binding 不出现在模型视图）
# ---------------------------------------------------------------------------


def test_bindings_match_production_registry_manifests(
    catalog: SemanticCatalog,
) -> None:
    registry = ToolRegistry.default()
    for subject_id in catalog.subject_ids():
        subject = catalog.require_subject(subject_id)
        binding = catalog.binding(subject_id)
        assert binding is not None
        manifest = registry.get_manifest(binding.capability_id)
        assert binding.capability_version == manifest.tool_version
        assert binding.adapter_ref == manifest.adapter_ref
        assert subject.logical_dataset_id == manifest.dataset_id
        assert subject.required_entitlement == manifest.required_permissions[0]


def test_model_view_serialization_matches_json_round_trip(
    catalog: SemanticCatalog,
) -> None:
    view = catalog.model_capability_view(FULL_AUTH)
    round_tripped = json.loads(view.model_dump_json())
    assert round_tripped["catalog_version"] == catalog.catalog_version
    assert "bindings" not in json.dumps(round_tripped)


# ---------------------------------------------------------------------------
# Catalog 声明与真实 Adapter 白名单逐项互证
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("area_code", "group_by"),
    [
        ("330106", "street"),
        ("330106001", "community"),
        ("330106001001", "grid"),
    ],
)
def test_declared_population_grouping_is_accepted_by_legacy_adapter(
    catalog: SemanticCatalog,
    area_code: str,
    group_by: str,
) -> None:
    subject = catalog.require_subject("population")
    rule_values = {rule.value for rule in subject.group_by_rules}
    assert group_by in rule_values
    for filters, expected_kind in (
        ([], "general"),
        (
            [
                {
                    "field": "person_category",
                    "operator": "eq",
                    "value": "solitary_elderly",
                }
            ],
            "solitary_elderly",
        ),
    ):
        arguments = QueryPopulationMetricsInput.model_validate(
            {
                "query": {
                    "metrics": ["person_count"],
                    "scope": {"area_code": area_code},
                    "filters": filters,
                    "group_by": [group_by],
                }
            }
        )
        # 不得抛出：Catalog 声明的组合必须被真实 Adapter 白名单接受。
        assert _validate_population_query(arguments) == expected_kind


def test_population_adapter_rejects_unmatched_city_and_undeclared_dimensions(
    catalog: SemanticCatalog,
) -> None:
    subject = catalog.require_subject("population")
    assert 4 in subject.scope_levels

    city_scope = QueryPopulationMetricsInput.model_validate(
        {
            "query": {
                "metrics": ["person_count"],
                "scope": {"area_code": "3301"},
                "filters": [
                    {
                        "field": "person_category",
                        "operator": "eq",
                        "value": "solitary_elderly",
                    }
                ],
                "group_by": ["street"],
            }
        }
    )
    with pytest.raises(Exception, match="declared population area groupings"):
        _validate_population_query(city_scope)

    gender_group = QueryPopulationMetricsInput.model_validate(
        {
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
                "group_by": ["gender"],
            }
        }
    )
    with pytest.raises(Exception, match="declared population area groupings"):
        _validate_population_query(gender_group)


@pytest.mark.parametrize(
    ("area_code", "accepted"),
    [
        ("3301", True),
        ("330106", True),
        ("330106001", True),
        ("330106001001", True),
        ("330106001001001", False),
    ],
)
def test_declared_housing_next_area_levels_match_legacy_adapter(
    catalog: SemanticCatalog,
    area_code: str,
    accepted: bool,
) -> None:
    arguments = QueryHousingMetricsInput.model_validate(
        {"query": {"scope": {"area_code": area_code}, "group_by": ["next_area"]}}
    )
    if accepted:
        _validate_housing_next_area_query(arguments)
    else:
        with pytest.raises(Exception, match="next_area grouping only for"):
            _validate_housing_next_area_query(arguments)


# ---------------------------------------------------------------------------
# 生产边界：S0 候选不得接线为生产 Tool
# ---------------------------------------------------------------------------


def test_semantic_query_is_not_registered_in_production_registry() -> None:
    registry = ToolRegistry.default()
    assert "governance.semantic_query" not in registry.list_tool_ids()
    assert "governance.semantic_query" not in TOOL_INPUT_MODELS
    with pytest.raises(ResourceNotFound):
        registry.get_manifest("governance.semantic_query")


def test_production_model_descriptors_do_not_mention_semantic_query() -> None:
    registry = ToolRegistry.default()
    for tool_id in registry.list_tool_ids():
        descriptor = registry.get_model_descriptor(tool_id)
        assert "semantic_query" not in descriptor.model_dump_json()


def test_subject_schema_rejects_physical_field_shapes() -> None:
    # 驼峰/点号等物理命名在 Schema 层即被结构性拒绝。
    with pytest.raises(ValidationError):
        SemanticFilter(field="areaCode", operator="eq", value="330106")
    with pytest.raises(ValidationError):
        SemanticFilter(field="area.code", operator="eq", value="330106")
    # 结构合法但未注册的物理表名由 Validator 语义白名单拒绝（见
    # test_semantic_validator.py::test_physical_table_name_rejected_by_whitelist）。
    assert SemanticFilter(
        field="dm_empty_nest_old", operator="eq", value="x"
    ).field == "dm_empty_nest_old"

    spec = SemanticQuerySpec(
        subject="population",
        metrics=["person_count"],
        scope={"area_code": "330106"},
    )
    assert spec.schema_version == "s0.1"
    assert spec.output == "table"
