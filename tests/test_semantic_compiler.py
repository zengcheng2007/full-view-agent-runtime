"""S0 语义内核候选：Compiler 与结果 Schema 校验测试。

Compiler 只把受控 QuerySpec 映射到已验证的能力标识（生产 Tool 契约），
每个主题携带各自准确的结果 Schema；物理 adapter 映射不出现在计划中；
verify_result 拒绝跨主题/跨形态的结果 Schema 不符。
"""

import pytest

from full_view_agent.application.fingerprints import canonical_fingerprint
from full_view_agent.domain.models import (
    AuthorizedAreaScope,
    EventFinishRateRow,
    EventFinishRateTable,
    HousingAreaGroupRow,
    HousingAreaGroupTable,
    HousingLeaseTypeRow,
    HousingLeaseTypeTable,
    PopulationMetricRow,
    PopulationMetricTable,
    QueryEventMetricsInput,
    QueryHousingMetricsInput,
    QueryPopulationMetricsInput,
    TableDataResult,
)
from full_view_agent.semantic import (
    ResultSchemaMismatch,
    SemanticCatalog,
    SemanticCompiler,
    SemanticQueryRejected,
    SemanticQuerySpec,
    SubjectAuthorization,
    ViolationCode,
)

PHYSICAL_TOKENS = (
    "adapter://",
    "http://",
    "https://",
    "getNextSiteData",
    "getRoomLeaseType",
    "getEventPropertiesAndConflictsByTotal",
    "base_room_lease",
    "dm_empty_nest_old",
    "tableName",
    "areaCode",
    "gridFinishRate",
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
def compiler() -> SemanticCompiler:
    return SemanticCompiler(SemanticCatalog.default())


def _population_spec(**overrides: object) -> SemanticQuerySpec:
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


def _table_result(data_schema_ref: str, data) -> TableDataResult:
    return TableDataResult(
        result_id="res-01",
        data_schema_ref=data_schema_ref,
        result_fingerprint=canonical_fingerprint(domain="data-result:test", value=data),
        data=data,
        row_count=len(data.rows),
    )


# ---------------------------------------------------------------------------
# 编译：映射到已验证能力标识与受控参数
# ---------------------------------------------------------------------------


def test_population_plan_maps_to_verified_capability(compiler: SemanticCompiler) -> None:
    plan = compiler.compile(_population_spec(), authorization=FULL_AUTH)

    assert plan.subject == "population"
    assert plan.logical_dataset_id == "population"
    assert len(plan.steps) == 1
    step = plan.steps[0]
    assert step.capability_id == "governance.query_population_metrics"
    assert step.capability_version == "1.0.0"
    # 编译产物必须能通过生产 Tool 输入契约校验。
    validated = QueryPopulationMetricsInput.model_validate(step.arguments)
    assert validated.query.metrics == ["person_count"]
    assert validated.query.group_by == ["street"]
    assert validated.query.filters[0].value == "solitary_elderly"
    assert validated.query.presentation_hint == "table"

    assert plan.expected_result.data_schema_ref == (
        "schema://data/population-metric-table/1.0.0"
    )
    assert plan.expected_result.row_fields == ("area_code", "area_name", "person_count")
    assert plan.evidence.dataset_id == "population"
    assert [m.metric_id for m in plan.evidence.metric_definitions] == ["person_count"]
    assert plan.catalog_version == SemanticCatalog.default().catalog_version


def test_population_choropleth_maps_to_presentation_hint(
    compiler: SemanticCompiler,
) -> None:
    plan = compiler.compile(
        _population_spec(output="choropleth"), authorization=FULL_AUTH
    )
    validated = QueryPopulationMetricsInput.model_validate(plan.steps[0].arguments)
    assert validated.query.presentation_hint == "choropleth"


def test_housing_lease_and_next_area_select_distinct_result_schemas(
    compiler: SemanticCompiler,
) -> None:
    lease = compiler.compile(
        _population_spec(
            subject="housing",
            metrics=["dwelling_count"],
            scope={"area_code": "330106001"},
            filters=[],
            group_by=[],
        ),
        authorization=FULL_AUTH,
    )
    lease_input = QueryHousingMetricsInput.model_validate(lease.steps[0].arguments)
    assert lease_input.query.group_by == []
    assert lease.steps[0].capability_id == "governance.query_housing_metrics"
    assert lease.expected_result.data_schema_ref == (
        "schema://data/housing-lease-type-table/1.0.0"
    )
    assert lease.expected_result.row_fields == ("lease_type", "dwelling_count")

    next_area_compiler = SemanticCompiler(
        SemanticCatalog.default(housing_next_area_enabled=True)
    )
    next_area = next_area_compiler.compile(
        _population_spec(
            subject="housing",
            metrics=["dwelling_count"],
            scope={"area_code": "330106"},
            filters=[],
            group_by=["next_area"],
        ),
        authorization=FULL_AUTH,
    )
    next_area_input = QueryHousingMetricsInput.model_validate(
        next_area.steps[0].arguments
    )
    assert next_area_input.query.group_by == ["next_area"]
    assert next_area.expected_result.data_schema_ref == (
        "schema://data/housing-area-group-table/1.0.0"
    )
    assert next_area.expected_result.row_fields == (
        "area_code",
        "area_name",
        "dwelling_count",
    )


def test_event_plan_maps_to_finish_rate_snapshot(compiler: SemanticCompiler) -> None:
    plan = compiler.compile(
        _population_spec(
            subject="event",
            metrics=["finish_rate"],
            filters=[],
            group_by=[],
        ),
        authorization=FULL_AUTH,
    )
    validated = QueryEventMetricsInput.model_validate(plan.steps[0].arguments)
    assert validated.query.scope.area_code == "330106"
    assert plan.expected_result.data_schema_ref == (
        "schema://data/event-finish-rate-table/1.0.0"
    )
    assert plan.expected_result.row_fields == ("level", "finish_rate")
    # 事件参数不得携带 group_by/filters 等未验证语义。
    assert "group_by" not in plan.steps[0].arguments["query"]
    assert "filters" not in plan.steps[0].arguments["query"]


def test_three_subjects_never_share_result_schema(compiler: SemanticCompiler) -> None:
    population = compiler.compile(_population_spec(), authorization=FULL_AUTH)
    housing = compiler.compile(
        _population_spec(
            subject="housing",
            metrics=["dwelling_count"],
            filters=[],
            group_by=[],
        ),
        authorization=FULL_AUTH,
    )
    event = compiler.compile(
        _population_spec(
            subject="event", metrics=["finish_rate"], filters=[], group_by=[]
        ),
        authorization=FULL_AUTH,
    )
    refs = {
        population.expected_result.data_schema_ref,
        housing.expected_result.data_schema_ref,
        event.expected_result.data_schema_ref,
    }
    assert len(refs) == 3


def test_plan_does_not_expose_physical_implementation(
    compiler: SemanticCompiler,
) -> None:
    plan = compiler.compile(_population_spec(), authorization=FULL_AUTH)
    serialized = plan.model_dump_json()
    for token in PHYSICAL_TOKENS:
        assert token not in serialized


# ---------------------------------------------------------------------------
# 编译前置门：必须先过 Validator
# ---------------------------------------------------------------------------


def test_compile_rejects_invalid_spec_with_violations(
    compiler: SemanticCompiler,
) -> None:
    with pytest.raises(SemanticQueryRejected) as excinfo:
        compiler.compile(_population_spec(metrics=["elderly_count"]))
    codes = {violation.code for violation in excinfo.value.violations}
    assert ViolationCode.UNKNOWN_METRIC in codes


def test_compile_rejects_unauthorized_subject(compiler: SemanticCompiler) -> None:
    population_only = SubjectAuthorization(
        entitlements=("governance.population.aggregate.read",),
        datasets=("population", "housing"),
        area_scopes=(AuthorizedAreaScope(area_code="3301"),),
        field_policy_set="governance_analyst_v1",
    )
    with pytest.raises(SemanticQueryRejected) as excinfo:
        compiler.compile(
            _population_spec(
                subject="housing",
                metrics=["dwelling_count"],
                filters=[],
                group_by=[],
            ),
            authorization=population_only,
        )
    codes = {violation.code for violation in excinfo.value.violations}
    assert ViolationCode.SUBJECT_NOT_ENTITLED in codes


# ---------------------------------------------------------------------------
# verify_result：结果 Schema 不符反例
# ---------------------------------------------------------------------------


def test_verify_result_accepts_matching_population_table(
    compiler: SemanticCompiler,
) -> None:
    plan = compiler.compile(_population_spec(), authorization=FULL_AUTH)
    data = PopulationMetricTable(
        rows=[
            PopulationMetricRow(
                area_code="330106001", area_name="翠苑街道", person_count=128
            )
        ]
    )
    result = _table_result("schema://data/population-metric-table/1.0.0", data)
    compiler.verify_result(plan, result)


def test_verify_result_rejects_population_payload_for_event_plan(
    compiler: SemanticCompiler,
) -> None:
    plan = compiler.compile(
        _population_spec(
            subject="event", metrics=["finish_rate"], filters=[], group_by=[]
        ),
        authorization=FULL_AUTH,
    )
    population_data = PopulationMetricTable(
        rows=[
            PopulationMetricRow(
                area_code="330106001", area_name="翠苑街道", person_count=128
            )
        ]
    )
    wrong = _table_result("schema://data/population-metric-table/1.0.0", population_data)
    with pytest.raises(ResultSchemaMismatch):
        compiler.verify_result(plan, wrong)


def test_verify_result_rejects_area_group_payload_for_lease_plan(
    compiler: SemanticCompiler,
) -> None:
    plan = compiler.compile(
        _population_spec(
            subject="housing",
            metrics=["dwelling_count"],
            scope={"area_code": "330106"},
            filters=[],
            group_by=[],
        ),
        authorization=FULL_AUTH,
    )
    area_group = HousingAreaGroupTable(
        rows=[
            HousingAreaGroupRow(
                area_code="330106001", area_name="翠苑街道", dwelling_count=21
            )
        ]
    )
    wrong = _table_result(
        "schema://data/housing-area-group-table/1.0.0", area_group
    )
    with pytest.raises(ResultSchemaMismatch):
        compiler.verify_result(plan, wrong)


def test_verify_result_rejects_row_shape_drift(compiler: SemanticCompiler) -> None:
    plan = compiler.compile(
        _population_spec(
            subject="event", metrics=["finish_rate"], filters=[], group_by=[]
        ),
        authorization=FULL_AUTH,
    )
    # schema_ref 正确但行字段被替换成人口行 → 仍须拒绝。
    drifted = EventFinishRateTable(
        rows=[EventFinishRateRow(level="grid", finish_rate=85)]
    )
    wrong = _table_result("schema://data/event-finish-rate-table/1.0.0", drifted)
    tampered = wrong.model_copy(
        update={
            "data": PopulationMetricTable(
                rows=[
                    PopulationMetricRow(
                        area_code="330106001", area_name="翠苑街道", person_count=1
                    )
                ]
            )
        }
    )
    with pytest.raises(ResultSchemaMismatch):
        compiler.verify_result(plan, tampered)


def test_verify_result_accepts_each_subject_shape(compiler: SemanticCompiler) -> None:
    lease_plan = compiler.compile(
        _population_spec(
            subject="housing",
            metrics=["dwelling_count"],
            filters=[],
            group_by=[],
        ),
        authorization=FULL_AUTH,
    )
    lease = HousingLeaseTypeTable(
        rows=[HousingLeaseTypeRow(lease_type="住宅出租", dwelling_count=32)]
    )
    compiler.verify_result(
        lease_plan,
        _table_result("schema://data/housing-lease-type-table/1.0.0", lease),
    )

    event_plan = compiler.compile(
        _population_spec(
            subject="event", metrics=["finish_rate"], filters=[], group_by=[]
        ),
        authorization=FULL_AUTH,
    )
    event = EventFinishRateTable(
        rows=[EventFinishRateRow(level="grid", finish_rate=85.5)]
    )
    compiler.verify_result(
        event_plan,
        _table_result("schema://data/event-finish-rate-table/1.0.0", event),
    )
