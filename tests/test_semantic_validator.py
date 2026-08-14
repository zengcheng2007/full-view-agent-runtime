"""S0 语义内核候选：Validator 反例测试。

覆盖未知主题/指标、非法维度/筛选/排序/输出、物理字段注入、版本不兼容、
越权主题/数据集/字段/区域，以及三主题受控语义边界。
"""

import pytest
from pydantic import ValidationError

from full_view_agent.domain.models import AuthorizedAreaScope
from full_view_agent.semantic import (
    SemanticCatalog,
    SemanticFilter,
    SemanticOrder,
    SemanticQuerySpec,
    SemanticTimeRange,
    SemanticValidator,
    SubjectAuthorization,
    ViolationCode,
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
def validator() -> SemanticValidator:
    # 正向 next_area 用例只在显式开启部署门禁的目录上运行。
    return SemanticValidator(
        SemanticCatalog.default(housing_next_area_enabled=True)
    )


def _spec(**overrides: object) -> SemanticQuerySpec:
    defaults: dict[str, object] = {
        "subject": "population",
        "metrics": ["person_count"],
        "scope": {"area_code": "330106"},
        "filters": [
            {"field": "person_category", "operator": "eq", "value": "solitary_elderly"}
        ],
        "group_by": ["street"],
    }
    defaults.update(overrides)
    return SemanticQuerySpec.model_validate(defaults)


def _codes(report) -> set[ViolationCode]:
    return {violation.code for violation in report.violations}


# ---------------------------------------------------------------------------
# 合法路径：三主题各自的受控语义
# ---------------------------------------------------------------------------


def test_valid_population_spec_passes(validator: SemanticValidator) -> None:
    report = validator.validate(_spec(), authorization=FULL_AUTH)
    assert report.is_valid, report.violations


@pytest.mark.parametrize(
    ("scope", "group_by"),
    [
        ("330106001", []),
        ("330106", ["next_area"]),
        ("3301", ["next_area"]),
        ("330106001001", ["next_area"]),
    ],
)
def test_valid_housing_specs_pass(
    validator: SemanticValidator,
    scope: str,
    group_by: list[str],
) -> None:
    report = validator.validate(
        _spec(
            subject="housing",
            metrics=["dwelling_count"],
            scope={"area_code": scope},
            filters=[],
            group_by=group_by,
        ),
        authorization=FULL_AUTH,
    )
    assert report.is_valid, report.violations


def test_valid_event_spec_passes(validator: SemanticValidator) -> None:
    report = validator.validate(
        _spec(subject="event", metrics=["finish_rate"], filters=[], group_by=[]),
        authorization=FULL_AUTH,
    )
    assert report.is_valid, report.violations


# ---------------------------------------------------------------------------
# 未知主题 / 未知指标
# ---------------------------------------------------------------------------


def test_unknown_subject_rejected(validator: SemanticValidator) -> None:
    report = validator.validate(_spec(subject="traffic"))
    assert ViolationCode.UNKNOWN_SUBJECT in _codes(report)
    assert not report.is_valid


def test_unknown_metric_rejected(validator: SemanticValidator) -> None:
    report = validator.validate(_spec(metrics=["elderly_count"]))
    assert ViolationCode.UNKNOWN_METRIC in _codes(report)


def test_cross_subject_metric_rejected(validator: SemanticValidator) -> None:
    # 把事件指标塞进人口主题：指标按主题注册，不得跨主题借用。
    report = validator.validate(_spec(metrics=["finish_rate"]))
    assert ViolationCode.UNKNOWN_METRIC in _codes(report)


# ---------------------------------------------------------------------------
# 非法维度 / 范围层级
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("dimension", ["gender", "age_band", "house_type"])
def test_unregistered_group_by_dimension_rejected(
    validator: SemanticValidator,
    dimension: str,
) -> None:
    report = validator.validate(_spec(group_by=[dimension]))
    assert ViolationCode.INVALID_GROUP_BY in _codes(report)


def test_population_group_by_must_match_immediate_child_level(
    validator: SemanticValidator,
) -> None:
    # 区县 scope 只能按街道汇总，传 community 违反真实 Adapter 白名单。
    report = validator.validate(_spec(group_by=["community"]))
    assert ViolationCode.GROUP_BY_SCOPE_MISMATCH in _codes(report)


def test_population_requires_exactly_one_group_by(
    validator: SemanticValidator,
) -> None:
    empty = validator.validate(_spec(group_by=[]))
    assert ViolationCode.INVALID_GROUP_BY in _codes(empty)
    double = validator.validate(_spec(group_by=["street", "community"]))
    assert ViolationCode.INVALID_GROUP_BY in _codes(double)


def test_scope_level_unsupported(validator: SemanticValidator) -> None:
    # 人口主题没有市级聚合的真实接口。
    city = validator.validate(
        _spec(scope={"area_code": "3301"}, group_by=["street"])
    )
    assert ViolationCode.GROUP_BY_SCOPE_MISMATCH in _codes(city)


def test_housing_next_area_rejects_grid_scope(validator: SemanticValidator) -> None:
    report = validator.validate(
        _spec(
            subject="housing",
            metrics=["dwelling_count"],
            scope={"area_code": "330106001001001"},
            filters=[],
            group_by=["next_area"],
        )
    )
    codes = _codes(report)
    assert ViolationCode.GROUP_BY_SCOPE_MISMATCH in codes or (
        ViolationCode.SCOPE_LEVEL_UNSUPPORTED in codes
    )


def test_housing_next_area_rejected_when_deployment_gate_closed() -> None:
    # 部署门禁关闭 next_area 时，Catalog 不声明该分组，Validator 必须拒绝；
    # 区域自身按租赁类型汇总（无 group_by）仍为唯一合法住房查询。
    gated = SemanticValidator(
        SemanticCatalog.default(housing_next_area_enabled=False)
    )
    next_area = gated.validate(
        _spec(
            subject="housing",
            metrics=["dwelling_count"],
            filters=[],
            group_by=["next_area"],
        ),
        authorization=FULL_AUTH,
    )
    assert ViolationCode.INVALID_GROUP_BY in _codes(next_area)

    lease_self = gated.validate(
        _spec(subject="housing", metrics=["dwelling_count"], filters=[], group_by=[]),
        authorization=FULL_AUTH,
    )
    assert lease_self.is_valid, lease_self.violations


def test_housing_next_area_accepts_only_its_intrinsic_dwelling_count_desc_order(
    validator: SemanticValidator,
) -> None:
    report = validator.validate(
        _spec(
            subject="housing",
            metrics=["dwelling_count"],
            filters=[],
            group_by=["next_area"],
            order_by=[{"field": "dwelling_count", "direction": "desc"}],
            output="table",
        ),
        authorization=FULL_AUTH,
    )

    assert report.is_valid, report.violations


@pytest.mark.parametrize(
    "overrides",
    [
        {"order_by": [{"field": "dwelling_count", "direction": "asc"}]},
        {"group_by": [], "order_by": [{"field": "dwelling_count", "direction": "desc"}]},
        {
            "filters": [{"field": "lease_type", "operator": "eq", "value": "住宅"}],
            "order_by": [{"field": "dwelling_count", "direction": "desc"}],
        },
        {
            "output": "choropleth",
            "order_by": [{"field": "dwelling_count", "direction": "desc"}],
        },
    ],
)
def test_housing_intrinsic_order_does_not_widen_other_shapes_or_constraints(
    validator: SemanticValidator,
    overrides: dict[str, object],
) -> None:
    values: dict[str, object] = {
        "subject": "housing",
        "metrics": ["dwelling_count"],
        "filters": [],
        "group_by": ["next_area"],
    }
    values.update(overrides)
    report = validator.validate(
        _spec(**values),
        authorization=FULL_AUTH,
    )

    assert ViolationCode.INVALID_ORDER_BY in _codes(report)


# ---------------------------------------------------------------------------
# 区划编码结构校验（QuerySpec 边界，先于主题语义）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "area_code",
    [
        "3301xx",  # 非数字填充
        "330106;DROP",  # 注入式后缀
        "33010",  # 5 位：非系统支持的区划长度
        "3301060",  # 7 位
        "33010600100",  # 11 位
        "33010600100100",  # 14 位
        "33010600100100100",  # 17 位
        "33 0106",  # 内嵌空格
        "330106.0",  # 小数点变体
        "３３０１０６",  # 全角数字：非 ASCII 数字同样拒绝
    ],
)
def test_query_spec_rejects_malformed_area_codes(area_code: str) -> None:
    # 语义层边界拒绝非纯数字与非 4/6/9/12/15 长度的区划编码。
    with pytest.raises(ValidationError):
        SemanticQuerySpec.model_validate(
            {
                "subject": "housing",
                "metrics": ["dwelling_count"],
                "scope": {"area_code": area_code},
            }
        )


@pytest.mark.parametrize(
    "area_code",
    ["3301", "330106", "330106001", "330106001001", "330106001001001"],
)
def test_query_spec_accepts_all_five_structural_area_levels(area_code: str) -> None:
    # 市(4)/区县(6)/街道(9)/社区(12)/网格(15) 五种合法长度通过结构层。
    spec = SemanticQuerySpec.model_validate(
        {
            "subject": "housing",
            "metrics": ["dwelling_count"],
            "scope": {"area_code": area_code},
        }
    )
    assert spec.scope.area_code == area_code


def test_structurally_valid_levels_still_constrained_by_subject(
    validator: SemanticValidator,
) -> None:
    # 结构合法的层级，主题没有对应真实能力时仍由 Validator 拒绝。
    city = validator.validate(_spec(scope={"area_code": "3301"}, group_by=["street"]))
    assert ViolationCode.GROUP_BY_SCOPE_MISMATCH in _codes(city)
    grid = validator.validate(
        _spec(scope={"area_code": "330106001001001"}, group_by=["grid"])
    )
    assert ViolationCode.SCOPE_LEVEL_UNSUPPORTED in _codes(grid)

    # housing 在网格本级有租赁类型汇总能力（无 group_by）→ 仍然有效。
    housing_grid = validator.validate(
        _spec(
            subject="housing",
            metrics=["dwelling_count"],
            scope={"area_code": "330106001001001"},
            filters=[],
            group_by=[],
        ),
        authorization=FULL_AUTH,
    )
    assert housing_grid.is_valid, housing_grid.violations


def test_event_rejects_any_group_by(validator: SemanticValidator) -> None:
    report = validator.validate(
        _spec(
            subject="event",
            metrics=["finish_rate"],
            filters=[],
            group_by=["street"],
        )
    )
    assert ViolationCode.INVALID_GROUP_BY in _codes(report)


# ---------------------------------------------------------------------------
# 事件受控边界：不得虚构时间、阈值、总量或下级区划
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("metric", ["finish_count", "total_count"])
def test_event_rejects_total_or_volume_metrics(
    validator: SemanticValidator, metric: str
) -> None:
    # 真实接口只返回三层办结率：任何总量/办结数指标都是虚构，必须拒绝。
    report = validator.validate(
        _spec(subject="event", metrics=[metric], filters=[], group_by=[])
    )
    assert ViolationCode.UNKNOWN_METRIC in _codes(report)


def test_event_count_requires_month_grouping_and_time_range(
    validator: SemanticValidator,
) -> None:
    report = validator.validate(
        _spec(subject="event", metrics=["event_count"], filters=[], group_by=[])
    )

    assert ViolationCode.INVALID_RESULT_SHAPE in _codes(report)


@pytest.mark.parametrize("dimension", ["next_area", "street", "grid"])
def test_event_rejects_sub_area_group_dimensions(
    validator: SemanticValidator, dimension: str
) -> None:
    # 事件只有区域自身快照：任何下级区划分组都是虚构，必须拒绝。
    report = validator.validate(
        _spec(
            subject="event",
            metrics=["finish_rate"],
            filters=[],
            group_by=[dimension],
        )
    )
    assert ViolationCode.INVALID_GROUP_BY in _codes(report)


@pytest.mark.parametrize(
    ("subject", "metrics"),
    [("housing", ["dwelling_count"]), ("event", ["finish_rate"])],
)
def test_housing_and_event_reject_time_range(
    validator: SemanticValidator, subject: str, metrics: list[str]
) -> None:
    report = validator.validate(
        _spec(
            subject=subject,
            metrics=metrics,
            filters=[],
            group_by=[],
            time_range={"start": "2026-01-01", "end": "2026-06-30"},
        )
    )
    assert ViolationCode.TIME_RANGE_UNSUPPORTED in _codes(report)


# ---------------------------------------------------------------------------
# 开放表达：同义结构在受控 Schema 层收敛为同一语义
# ---------------------------------------------------------------------------


def test_synonymous_spec_forms_validate_to_identical_model() -> None:
    # 键序不同、显式默认值（output/limit/空集合）与省略默认值同义：
    # 不同开放表达进入统一入口后不得产生语义差异。
    explicit = SemanticQuerySpec.model_validate(
        {
            "schema_version": "s0.1",
            "subject": "housing",
            "metrics": ["dwelling_count"],
            "scope": {"area_code": "330106", "include_descendants": True},
            "group_by": [],
            "filters": [],
            "order_by": [],
            "limit": 200,
            "output": "table",
        }
    )
    reordered = SemanticQuerySpec.model_validate(
        {
            "output": "table",
            "scope": {"include_descendants": True, "area_code": "330106"},
            "metrics": ["dwelling_count"],
            "subject": "housing",
        }
    )
    assert explicit.model_dump() == reordered.model_dump()


# ---------------------------------------------------------------------------
# 非法筛选 / 物理字段注入
# ---------------------------------------------------------------------------


def test_unregistered_filter_field_rejected(validator: SemanticValidator) -> None:
    report = validator.validate(
        _spec(filters=[{"field": "table_name", "operator": "eq", "value": "x"}])
    )
    assert ViolationCode.INVALID_FILTER_FIELD in _codes(report)


@pytest.mark.parametrize("injected", ["areaCode", "tableName", "area.code"])
def test_physical_field_injection_rejected_at_schema_level(injected: str) -> None:
    with pytest.raises(ValidationError):
        SemanticFilter(field=injected, operator="eq", value="x")


def test_physical_table_name_rejected_by_whitelist(
    validator: SemanticValidator,
) -> None:
    # 结构合法（全小写下划线）的物理表名无法在 Schema 层识别，
    # 必须由 Validator 的字段注册白名单拒绝。
    report = validator.validate(
        _spec(
            filters=[
                {"field": "dm_empty_nest_old", "operator": "eq", "value": "x"}
            ]
        )
    )
    assert ViolationCode.INVALID_FILTER_FIELD in _codes(report)


def test_filter_value_injection_rejected_by_whitelist(
    validator: SemanticValidator,
) -> None:
    report = validator.validate(
        _spec(
            filters=[
                {
                    "field": "person_category",
                    "operator": "eq",
                    "value": "x'; DROP TABLE dm_empty_nest_old;--",
                }
            ]
        )
    )
    assert ViolationCode.INVALID_FILTER_VALUE in _codes(report)


def test_invalid_filter_operator_rejected(validator: SemanticValidator) -> None:
    report = validator.validate(
        _spec(
            filters=[
                {"field": "person_category", "operator": "gte", "value": "solitary_elderly"}
            ]
        )
    )
    assert ViolationCode.INVALID_FILTER_OPERATOR in _codes(report)


def test_invalid_filter_value_rejected(validator: SemanticValidator) -> None:
    report = validator.validate(
        _spec(
            filters=[
                {"field": "person_category", "operator": "eq", "value": "not_a_category"}
            ]
        )
    )
    assert ViolationCode.INVALID_FILTER_VALUE in _codes(report)


def test_housing_and_event_reject_all_filters(
    validator: SemanticValidator,
) -> None:
    housing = validator.validate(
        _spec(
            subject="housing",
            metrics=["dwelling_count"],
            scope={"area_code": "330106"},
            filters=[{"field": "lease_type", "operator": "eq", "value": "群租房"}],
            group_by=[],
        )
    )
    assert ViolationCode.INVALID_FILTER_FIELD in _codes(housing)
    event = validator.validate(
        _spec(
            subject="event",
            metrics=["finish_rate"],
            filters=[{"field": "finish_rate", "operator": "lt", "value": 60}],
            group_by=[],
        )
    )
    assert ViolationCode.INVALID_FILTER_FIELD in _codes(event)


# ---------------------------------------------------------------------------
# 非法排序 / 时间范围 / 输出形态
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("subject", "metrics", "scope", "group_by"),
    [
        ("housing", ["dwelling_count"], "330106", []),
        ("event", ["finish_rate"], "330106", []),
    ],
)
def test_order_by_rejected_for_subjects_without_verified_sorting(
    validator: SemanticValidator,
    subject: str,
    metrics: list[str],
    scope: str,
    group_by: list[str],
) -> None:
    report = validator.validate(
        _spec(
            subject=subject,
            metrics=metrics,
            scope={"area_code": scope},
            filters=[],
            group_by=group_by,
            order_by=[{"field": metrics[0], "direction": "desc"}],
        )
    )
    assert ViolationCode.INVALID_ORDER_BY in _codes(report)


def test_population_person_count_order_by_is_supported(
    validator: SemanticValidator,
) -> None:
    report = validator.validate(
        _spec(
            subject="population",
            metrics=["person_count"],
            scope={"area_code": "330106"},
            filters=[],
            group_by=["street"],
            order_by=[{"field": "person_count", "direction": "desc"}],
        )
    )
    assert report.is_valid


def test_time_range_rejected_until_source_supports_it(
    validator: SemanticValidator,
) -> None:
    report = validator.validate(
        _spec(time_range={"start": "2026-01-01", "end": "2026-06-30"})
    )
    assert ViolationCode.TIME_RANGE_UNSUPPORTED in _codes(report)


@pytest.mark.parametrize(
    ("subject", "metrics", "group_by", "output"),
    [
        ("event", ["finish_rate"], [], "choropleth"),
        ("housing", ["dwelling_count"], [], "choropleth"),
        ("population", ["person_count"], ["street"], "metric_card"),
    ],
)
def test_output_form_constrained_per_subject(
    validator: SemanticValidator,
    subject: str,
    metrics: list[str],
    group_by: list[str],
    output: str,
) -> None:
    report = validator.validate(
        _spec(subject=subject, metrics=metrics, filters=[], group_by=group_by, output=output)
    )
    assert ViolationCode.INVALID_OUTPUT in _codes(report)


def test_population_choropleth_output_allowed(validator: SemanticValidator) -> None:
    report = validator.validate(_spec(output="choropleth"), authorization=FULL_AUTH)
    assert report.is_valid, report.violations


# ---------------------------------------------------------------------------
# 版本兼容
# ---------------------------------------------------------------------------


def test_catalog_version_incompatible_rejected() -> None:
    base = SemanticCatalog.default()
    future = SemanticCatalog(
        catalog_version="0.2.0-s0-candidate",
        supported_spec_versions=("s0.2",),
        subjects=dict(base.subjects),
        bindings=dict(base.bindings),
    )
    report = SemanticValidator(future).validate(_spec())
    assert ViolationCode.CATALOG_VERSION_INCOMPATIBLE in _codes(report)


def test_spec_schema_version_is_closed_vocabulary() -> None:
    with pytest.raises(ValidationError):
        SemanticQuerySpec.model_validate(
            {
                "schema_version": "1.0",
                "subject": "population",
                "metrics": ["person_count"],
                "scope": {"area_code": "330106"},
            }
        )


# ---------------------------------------------------------------------------
# 越权：主题 / 数据集 / 字段策略 / 区域
# ---------------------------------------------------------------------------


def test_subject_not_entitled_rejected(validator: SemanticValidator) -> None:
    population_only = SubjectAuthorization(
        entitlements=("governance.population.aggregate.read",),
        datasets=("population", "housing"),
        area_scopes=(AuthorizedAreaScope(area_code="3301"),),
        field_policy_set="governance_analyst_v1",
    )
    report = validator.validate(
        _spec(
            subject="housing",
            metrics=["dwelling_count"],
            scope={"area_code": "330106"},
            filters=[],
            group_by=[],
        ),
        authorization=population_only,
    )
    assert ViolationCode.SUBJECT_NOT_ENTITLED in _codes(report)


def test_dataset_not_authorized_rejected(validator: SemanticValidator) -> None:
    missing_dataset = SubjectAuthorization(
        entitlements=(
            "governance.population.aggregate.read",
            "governance.housing.aggregate.read",
        ),
        datasets=("population",),
        area_scopes=(AuthorizedAreaScope(area_code="3301"),),
        field_policy_set="governance_analyst_v1",
    )
    report = validator.validate(
        _spec(
            subject="housing",
            metrics=["dwelling_count"],
            scope={"area_code": "330106"},
            filters=[],
            group_by=[],
        ),
        authorization=missing_dataset,
    )
    assert ViolationCode.DATASET_NOT_AUTHORIZED in _codes(report)


def test_area_out_of_scope_rejected(validator: SemanticValidator) -> None:
    xihu_only = SubjectAuthorization(
        entitlements=("governance.population.aggregate.read",),
        datasets=("population",),
        area_scopes=(AuthorizedAreaScope(area_code="330106", include_descendants=True),),
        field_policy_set="governance_analyst_v1",
    )
    report = validator.validate(
        _spec(scope={"area_code": "330105"}, group_by=["street"]),
        authorization=xihu_only,
    )
    assert ViolationCode.AREA_OUT_OF_SCOPE in _codes(report)


def test_field_policy_not_authorized_rejected(validator: SemanticValidator) -> None:
    public_policy = SubjectAuthorization(
        entitlements=("governance.population.aggregate.read",),
        datasets=("population",),
        area_scopes=(AuthorizedAreaScope(area_code="3301"),),
        field_policy_set="public_v1",
    )
    report = validator.validate(_spec(), authorization=public_policy)
    assert ViolationCode.FIELD_POLICY_NOT_AUTHORIZED in _codes(report)


def test_no_authorization_skips_permission_codes(
    validator: SemanticValidator,
) -> None:
    report = validator.validate(_spec())
    permission_codes = {
        ViolationCode.SUBJECT_NOT_ENTITLED,
        ViolationCode.DATASET_NOT_AUTHORIZED,
        ViolationCode.AREA_OUT_OF_SCOPE,
        ViolationCode.FIELD_POLICY_NOT_AUTHORIZED,
    }
    assert not (_codes(report) & permission_codes)
    assert report.is_valid


def test_semantic_filter_and_order_models_reject_physical_names() -> None:
    with pytest.raises(ValidationError):
        SemanticFilter(field="person_category", operator="EQ", value="x")
    with pytest.raises(ValidationError):
        SemanticOrder(field="PersonCount", direction="desc")
    time_range = SemanticTimeRange(start="2026-01-01", end="2026-06-30")
    assert time_range.start == "2026-01-01"
