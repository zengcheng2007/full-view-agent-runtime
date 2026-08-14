import pytest
from pydantic import ValidationError

from full_view_agent.application.answer_claims import (
    FINISH_TOOL_INPUT_SCHEMA,
    AnswerClaim,
    StructuredFinish,
)
from full_view_agent.application.harness import (
    DeterministicCompletionValidator,
    FinishAction,
    HarnessState,
)
from full_view_agent.domain.models import (
    EnterpriseMetricRow,
    EnterpriseMetricTable,
    EventFinishRateRow,
    EventFinishRateTable,
    EvidenceMetricDefinition,
    GovernanceObjectRef,
    GovernanceOverviewRow,
    GovernanceOverviewTable,
    HousingAreaGroupRow,
    HousingAreaGroupTable,
    ObjectProfileData,
    ObjectProfileResult,
    PopulationMetricRow,
    PopulationMetricTable,
    SemanticFilterLineage,
    SemanticResultLineage,
    TableDataResult,
    ToolResult,
)

from .test_harness import auth_context, housing_comparison_result, successful_housing_result


def claim(
    *,
    result_id: str = "res-housing",
    result_fingerprint: str = "sha256:housing",
    locator: dict[str, object] | None = None,
    field: str = "dwelling_count",
    operation: str = "value",
    value: object = 884,
) -> AnswerClaim:
    return AnswerClaim(
        claim_id="claim-1",
        result_id=result_id,
        result_fingerprint=result_fingerprint,
        collection="rows",
        row_locator=(
            {"lease_type": "住宅出租"} if locator is None else locator
        ),
        field=field,
        operation=operation,
        value=value,
    )


def finish(*claims: AnswerClaim) -> FinishAction:
    structured = StructuredFinish(
        kind="claims",
        summary="模型生成的正文不应成为事实来源。",
        claims=list(claims),
    )
    return FinishAction(summary=structured.summary, structured_finish=structured)


@pytest.mark.asyncio
async def test_structured_claim_accepts_exact_row_value_and_renders_server_fact() -> None:
    assessment = await DeterministicCompletionValidator().assess(
        HarnessState(tool_results=(successful_housing_result(),)),
        finish(claim()),
    )

    assert assessment.status == "accept"
    assert assessment.safe_summary == "住宅出租的出租房数量为884套。"


@pytest.mark.asyncio
async def test_structured_claim_preserves_unsupported_constraint_as_server_text() -> None:
    structured = StructuredFinish(
        kind="claims",
        summary="这里可能包含模型自由文本，不能直接返回。",
        limitations=["unsupported_requested_constraint"],
        claims=[claim()],
    )

    assessment = await DeterministicCompletionValidator().assess(
        HarnessState(tool_results=(successful_housing_result(),)),
        FinishAction(summary=structured.summary, structured_finish=structured),
    )

    assert assessment.status == "accept"
    assert assessment.safe_summary == (
        "当前能力不支持用户要求的全部筛选条件；以下结果采用已支持的更宽口径，"
        "不等同于原问题的精确结果。\n住宅出租的出租房数量为884套。"
    )


def test_structured_limitation_rejects_uncontrolled_model_text() -> None:
    with pytest.raises(ValidationError):
        StructuredFinish.model_validate(
            {
                "kind": "claims",
                "summary": "summary",
                "limitations": ["model_supplied_warning"],
                "claims": [claim().model_dump(mode="json")],
            }
        )


@pytest.mark.asyncio
async def test_capability_limitation_after_query_returns_only_server_boundary() -> None:
    structured = StructuredFinish(
        kind="capability",
        summary="模型自由文本包含128人，但不得泄漏为能力答复。",
        limitations=["unsupported_requested_constraint"],
    )

    assessment = await DeterministicCompletionValidator().assess(
        HarnessState(tool_results=(successful_housing_result(),)),
        FinishAction(summary=structured.summary, structured_finish=structured),
    )

    assert assessment.status == "accept"
    assert assessment.safe_summary == (
        "当前能力不支持用户要求的全部筛选条件，无法按原条件精确查询。"
    )
    assert "128" not in assessment.safe_summary


def metric_result(
    *,
    result_id: str,
    data: (
        PopulationMetricTable
        | HousingAreaGroupTable
        | EventFinishRateTable
        | GovernanceOverviewTable
        | EnterpriseMetricTable
    ),
    semantic_lineage: SemanticResultLineage | None = None,
) -> ToolResult:
    rows = data.rows
    return ToolResult(
        tool_call_id=f"tc-{result_id}",
        tool_id="governance.semantic_query",
        tool_version="1.0.0",
        status="success",
        summary="查询成功",
        semantic_lineage=semantic_lineage,
        data_result=TableDataResult(
            result_id=result_id,
            data_schema_ref=f"schema://data/{result_id}/1.0.0",
            result_fingerprint=f"sha256:{result_id}",
            data=data,
            row_count=len(rows),
        ),
    )


@pytest.mark.asyncio
async def test_event_claim_renders_contract_label_unit_and_enum_label() -> None:
    result = metric_result(
        result_id="event",
        data=EventFinishRateTable(
            rows=[EventFinishRateRow(level="grid", finish_rate=85.0)]
        ),
    )
    event_claim = claim(
        result_id="event",
        result_fingerprint="sha256:event",
        locator={"level": "grid"},
        field="finish_rate",
        value=85,
    )

    assessment = await DeterministicCompletionValidator().assess(
        HarnessState(tool_results=(result,)), finish(event_claim)
    )

    assert assessment.status == "accept"
    assert assessment.safe_summary == "网格的事件办结率为85%。"


@pytest.mark.asyncio
async def test_governance_overview_claim_renders_chinese_subject_label() -> None:
    result = metric_result(
        result_id="governance-overview",
        data=GovernanceOverviewTable(
            rows=[
                GovernanceOverviewRow(
                    subject="person",
                    subject_label="人",
                    related_count=80,
                    total_count=100,
                    coverage_rate=80,
                )
            ]
        ),
    )
    overview_claim = claim(
        result_id="governance-overview",
        result_fingerprint="sha256:governance-overview",
        locator={"subject": "person"},
        field="coverage_rate",
        value=80,
    )

    assessment = await DeterministicCompletionValidator().assess(
        HarnessState(tool_results=(result,)), finish(overview_claim)
    )

    assert assessment.status == "accept"
    assert assessment.safe_summary == "人的治理覆盖率为80%。"
    assert "person" not in assessment.safe_summary


@pytest.mark.asyncio
async def test_enterprise_claim_renders_business_area_name_and_unit() -> None:
    result = metric_result(
        result_id="enterprise",
        data=EnterpriseMetricTable(
            rows=[
                EnterpriseMetricRow(
                    area_code="330106001",
                    area_name="翠苑街道",
                    enterprise_count=31,
                )
            ]
        ),
    )
    enterprise_claim = claim(
        result_id="enterprise",
        result_fingerprint="sha256:enterprise",
        locator={"area_code": "330106001"},
        field="enterprise_count",
        value=31,
    )

    assessment = await DeterministicCompletionValidator().assess(
        HarnessState(tool_results=(result,)), finish(enterprise_claim)
    )

    assert assessment.status == "accept"
    assert assessment.safe_summary == "翠苑街道的企业数量为31家。"


@pytest.mark.asyncio
async def test_comprehensive_claims_keep_governance_and_enterprise_business_labels() -> None:
    overview = metric_result(
        result_id="governance-overview",
        data=GovernanceOverviewTable(
            rows=[
                GovernanceOverviewRow(
                    subject="enterprise",
                    subject_label="企",
                    related_count=18,
                    total_count=20,
                    coverage_rate=90,
                )
            ]
        ),
    )
    enterprise = metric_result(
        result_id="enterprise",
        data=EnterpriseMetricTable(
            rows=[
                EnterpriseMetricRow(
                    area_code="330106001",
                    area_name="翠苑街道",
                    enterprise_count=31,
                )
            ]
        ),
    )
    overview_claim = claim(
        result_id="governance-overview",
        result_fingerprint="sha256:governance-overview",
        locator={"subject": "enterprise"},
        field="coverage_rate",
        value=90,
    )
    enterprise_claim = claim(
        result_id="enterprise",
        result_fingerprint="sha256:enterprise",
        locator={"area_code": "330106001"},
        field="enterprise_count",
        value=31,
    ).model_copy(update={"claim_id": "claim-2"})

    assessment = await DeterministicCompletionValidator().assess(
        HarnessState(tool_results=(overview, enterprise)),
        finish(overview_claim, enterprise_claim),
    )

    assert assessment.status == "accept"
    assert assessment.safe_summary == (
        "企的治理覆盖率为90%。\n翠苑街道的企业数量为31家。"
    )
    assert not {"person", "house", "enterprise", "event", "matter"}.intersection(
        assessment.safe_summary.split()
    )


@pytest.mark.asyncio
async def test_population_claim_renders_contract_label_and_unit() -> None:
    result = metric_result(
        result_id="population",
        data=PopulationMetricTable(
            rows=[
                PopulationMetricRow(
                    area_code="330106", area_name="西湖区", person_count=1200
                )
            ]
        ),
    )
    population_claim = claim(
        result_id="population",
        result_fingerprint="sha256:population",
        locator={"area_name": "西湖区"},
        field="person_count",
        value=1200,
    )

    assessment = await DeterministicCompletionValidator().assess(
        HarnessState(tool_results=(result,)), finish(population_claim)
    )

    assert assessment.status == "accept"
    assert assessment.safe_summary == "西湖区的人口数为1200人。"


@pytest.mark.asyncio
async def test_population_claim_preserves_server_resolved_filter_context() -> None:
    lineage = SemanticResultLineage(
        virtual_tool_id="governance.semantic_query",
        virtual_tool_version="1.0.0",
        spec_version="s0.1",
        catalog_version="catalog-v1",
        subject="population",
        logical_dataset_id="population",
        canonical_tool_id="governance.query_population_metrics",
        canonical_tool_version="1.0.0",
        spec_fingerprint="sha256:spec",
        plan_fingerprint="sha256:plan",
        area_code="330106",
        output="table",
        metric_definitions=[
            EvidenceMetricDefinition(
                metric_id="person_count",
                definition_version="1.0.0",
            )
        ],
        filter_contexts=[
            SemanticFilterLineage(
                field="person_category",
                operator="eq",
                value="solitary_elderly",
                display_label="独居老人",
            )
        ],
    )
    result = metric_result(
        result_id="population",
        semantic_lineage=lineage,
        data=PopulationMetricTable(
            rows=[
                PopulationMetricRow(
                    area_code="330106001",
                    area_name="示例街道",
                    person_count=128,
                )
            ]
        ),
    )
    population_claim = claim(
        result_id="population",
        result_fingerprint="sha256:population",
        locator={"area_code": "330106001"},
        field="person_count",
        value=128,
    )

    assessment = await DeterministicCompletionValidator().assess(
        HarnessState(tool_results=(result,)), finish(population_claim)
    )

    assert assessment.status == "accept"
    assert assessment.safe_summary == "独居老人中，示例街道的人口数为128人。"


@pytest.mark.asyncio
async def test_area_code_locator_renders_available_area_name() -> None:
    result = metric_result(
        result_id="population",
        data=PopulationMetricTable(
            rows=[
                PopulationMetricRow(
                    area_code="330106011",
                    area_name="转塘街道",
                    person_count=1840,
                )
            ]
        ),
    )
    population_claim = claim(
        result_id="population",
        result_fingerprint="sha256:population",
        locator={"area_code": "330106011", "area_name": "转塘街道"},
        field="person_count",
        value=1840,
    )

    assessment = await DeterministicCompletionValidator().assess(
        HarnessState(tool_results=(result,)), finish(population_claim)
    )

    assert assessment.status == "accept"
    assert assessment.safe_summary == "转塘街道的人口数为1840人。"


@pytest.mark.asyncio
async def test_housing_area_claim_uses_same_contract_metadata() -> None:
    result = metric_result(
        result_id="housing-area",
        data=HousingAreaGroupTable(
            rows=[
                HousingAreaGroupRow(
                    area_code="330106", area_name="西湖区", dwelling_count=3200
                )
            ]
        ),
    )
    area_claim = claim(
        result_id="housing-area",
        result_fingerprint="sha256:housing-area",
        locator={"area_name": "西湖区"},
        value=3200,
    )

    assessment = await DeterministicCompletionValidator().assess(
        HarnessState(tool_results=(result,)), finish(area_claim)
    )

    assert assessment.status == "accept"
    assert assessment.safe_summary == "西湖区的出租房数量为3200套。"


def test_model_cannot_supply_claim_label_or_unit() -> None:
    payload = claim().model_dump(mode="python") | {
        "label": "伪造的安全字段",
        "unit": "亿套",
    }

    with pytest.raises(ValidationError):
        AnswerClaim.model_validate(payload)


def test_display_metadata_does_not_enter_result_data_fingerprint_input() -> None:
    row = EventFinishRateRow(level="grid", finish_rate=85.0)

    assert row.model_dump(mode="python") == {
        "level": "grid",
        "finish_rate": 85.0,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("bad_claim", "reason_code"),
    (
        (claim(value=850), "claim_value_mismatch"),
        (claim(result_fingerprint="sha256:wrong"), "claim_fingerprint_mismatch"),
        (claim(locator={"lease_type": "不存在"}), "claim_row_not_found"),
        (claim(locator={}), "claim_row_ambiguous"),
        (claim(field="missing_field"), "claim_field_not_found"),
    ),
)
async def test_structured_claim_rejects_invalid_source_binding(
    bad_claim: AnswerClaim, reason_code: str
) -> None:
    assessment = await DeterministicCompletionValidator().assess(
        HarnessState(tool_results=(successful_housing_result(),)),
        finish(bad_claim),
    )

    assert assessment.status == "revise"
    assert assessment.reason_code == reason_code


@pytest.mark.asyncio
async def test_claim_cannot_swap_values_between_results_with_same_labels() -> None:
    result_a = housing_comparison_result(
        result_id="res-source-a", residential=3200, commercial=850
    )
    result_b = housing_comparison_result(
        result_id="res-source-b", residential=1000, commercial=500
    )
    swapped = claim(
        result_id="res-source-a",
        result_fingerprint="sha256:res-source-a",
        value=1000,
    )

    assessment = await DeterministicCompletionValidator().assess(
        HarnessState(tool_results=(result_a, result_b)), finish(swapped)
    )

    assert assessment.status == "revise"
    assert assessment.reason_code == "claim_value_mismatch"


@pytest.mark.asyncio
async def test_structured_claim_rejects_wrong_extreme_operation() -> None:
    result = housing_comparison_result(
        result_id="res-rank", residential=3200, commercial=850
    )
    wrong_max = claim(
        result_id="res-rank",
        result_fingerprint="sha256:res-rank",
        locator={"lease_type": "商铺出租"},
        operation="is_max",
        value=850,
    )

    assessment = await DeterministicCompletionValidator().assess(
        HarnessState(tool_results=(result,)), finish(wrong_max)
    )

    assert assessment.status == "revise"
    assert assessment.reason_code == "claim_operation_mismatch"


@pytest.mark.asyncio
async def test_root_collection_claim_reads_visible_result_field() -> None:
    result = ToolResult(
        tool_call_id="tc-object",
        tool_id="governance.get_object_profile",
        tool_version="1.0",
        status="success",
        summary="查询成功",
        data_result=ObjectProfileResult(
            result_id="res-object",
            data_schema_ref="schema://data/object-profile/1.0.0",
            result_fingerprint="sha256:object",
            data=ObjectProfileData(
                object_ref=GovernanceObjectRef(
                    object_type="building", object_id="building-12"
                ),
                area_code="330106",
                title="翠苑一区12幢",
                fields=[],
            ),
        ),
    )
    root_claim = claim(
        result_id="res-object",
        result_fingerprint="sha256:object",
        locator={"area_code": "330106"},
        field="title",
        value="翠苑一区12幢",
    ).model_copy(update={"collection": "root"})

    assessment = await DeterministicCompletionValidator().assess(
        HarnessState(tool_results=(result,)), finish(root_claim)
    )

    assert assessment.status == "accept"
    assert assessment.safe_summary == "330106的title为翠苑一区12幢。"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "locator", "value"),
    (
        ("sum", {}, 4420),
        ("is_max", {"lease_type": "住宅出租"}, 884),
    ),
)
async def test_truncated_result_rejects_whole_result_aggregate(
    operation: str, locator: dict[str, object], value: object
) -> None:
    result = successful_housing_result().model_copy(
        update={
            "data_result": successful_housing_result().data_result.model_copy(
                update={"truncated": True}
            )
        }
    )
    aggregate = claim(locator=locator, operation=operation, value=value)

    assessment = await DeterministicCompletionValidator().assess(
        HarnessState(tool_results=(result,)), finish(aggregate)
    )

    assert assessment.status == "revise"
    assert assessment.reason_code == "claim_truncated_aggregate"


@pytest.mark.asyncio
async def test_reference_only_uses_fixed_server_summary() -> None:
    structured = StructuredFinish(
        kind="reference_only",
        summary="模型正文即使写错数字 9999 也不能展示。",
        claims=[],
    )

    assessment = await DeterministicCompletionValidator().assess(
        HarnessState(tool_results=(successful_housing_result(),)),
        FinishAction(summary=structured.summary, structured_finish=structured),
    )

    assert assessment.status == "accept"
    assert assessment.safe_summary == "查询已完成，详细结果请查看数据面板。"


@pytest.mark.asyncio
async def test_finish_tool_schema_only_exposes_implemented_kinds() -> None:
    kind_schema = FINISH_TOOL_INPUT_SCHEMA["properties"]["kind"]

    assert kind_schema["enum"] == [
        "claims",
        "reference_only",
        "capability",
        "clarification",
        "denial",
        "failure",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "tool_results", "expected_summary"),
    (
        (
            "capability",
            (),
            "我可以协助使用当前已授权的治理查询能力。",
        ),
        (
            "clarification",
            (),
            "请补充查询所需的区域、对象或统计口径。",
        ),
        (
            "denial",
            (
                ToolResult(
                    tool_call_id="tc-denied",
                    tool_id="governance.query_population_metrics",
                    tool_version="1.0.0",
                    status="denied",
                    summary="无权访问",
                ),
            ),
            "当前查询因权限限制无法完成。",
        ),
        (
            "failure",
            (
                ToolResult(
                    tool_call_id="tc-failed",
                    tool_id="governance.query_population_metrics",
                    tool_version="1.0.0",
                    status="failed",
                    summary="上游失败",
                ),
            ),
            "本次查询执行失败，未生成业务结论。",
        ),
    ),
)
async def test_non_data_structured_finish_uses_status_bound_server_template(
    kind: str,
    tool_results: tuple[ToolResult, ...],
    expected_summary: str,
) -> None:
    structured = StructuredFinish(
        kind=kind,
        summary="我可以确认住宅出租有999999套。",
        claims=[],
    )

    assessment = await DeterministicCompletionValidator().assess(
        HarnessState(tool_results=tool_results),
        FinishAction(
            summary=structured.summary,
            structured_finish=structured,
            legacy=False,
        ),
    )

    assert assessment.status == "accept"
    assert assessment.safe_summary == expected_summary


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ("denial", "failure"))
async def test_status_bound_finish_rejects_without_matching_tool_result(
    kind: str,
) -> None:
    structured = StructuredFinish(
        kind=kind,
        summary="模型试图无依据结束。",
        claims=[],
    )

    assessment = await DeterministicCompletionValidator().assess(
        HarnessState(),
        FinishAction(
            summary=structured.summary,
            structured_finish=structured,
            legacy=False,
        ),
    )

    assert assessment.status == "revise"
    assert assessment.reason_code == f"structured_{kind}_state_mismatch"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "value"),
    (("sum", 4420), ("count", 5), ("min", 884), ("max", 884), ("all_equal", True)),
)
async def test_structured_claim_recomputes_supported_aggregates(
    operation: str, value: object
) -> None:
    assessment = await DeterministicCompletionValidator().assess(
        HarnessState(tool_results=(successful_housing_result(),)),
        finish(claim(locator={}, operation=operation, value=value)),
    )

    assert assessment.status == "accept"


@pytest.mark.asyncio
async def test_all_equal_requires_more_than_one_selected_row() -> None:
    assessment = await DeterministicCompletionValidator().assess(
        HarnessState(tool_results=(successful_housing_result(),)),
        finish(
            claim(
                locator={"lease_type": "住宅出租"},
                operation="all_equal",
                value=True,
            )
        ),
    )

    assert assessment.status == "revise"
    assert assessment.reason_code == "claim_operation_mismatch"


@pytest.mark.asyncio
async def test_all_equal_false_cannot_render_the_opposite_meaning() -> None:
    result = housing_comparison_result(
        result_id="res-not-equal", residential=3200, commercial=850
    )

    assessment = await DeterministicCompletionValidator().assess(
        HarnessState(tool_results=(result,)),
        finish(
            claim(
                result_id="res-not-equal",
                result_fingerprint="sha256:res-not-equal",
                locator={},
                operation="all_equal",
                value=False,
            )
        ),
    )

    assert assessment.status == "revise"
    assert assessment.reason_code == "claim_operation_mismatch"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ("reference_only",))
async def test_structured_finish_without_any_result_cannot_borrow_text_allowlist(
    kind: str,
) -> None:
    structured = StructuredFinish(
        kind=kind,
        summary="我可以确认住宅出租有9999套。",
        claims=[],
    )

    assessment = await DeterministicCompletionValidator().assess(
        HarnessState(),
        FinishAction(
            summary=structured.summary,
            structured_finish=structured,
            legacy=False,
        ),
    )

    assert assessment.status == "revise"
    assert assessment.safe_summary is None


@pytest.mark.asyncio
async def test_inherited_unhydrated_result_only_allows_structured_reference() -> None:
    state = HarnessState(
        inherited_result_ids=("res-old",),
        inherited_evidence_ids=("ev-old",),
    )
    plain = await DeterministicCompletionValidator().assess(
        state,
        FinishAction(summary="历史结果显示住宅出租有9999套。", legacy=False),
    )
    claims = StructuredFinish(
        kind="claims",
        summary="历史结果显示住宅出租有9999套。",
        claims=[claim(result_id="res-old", result_fingerprint="sha256:old", value=9999)],
    )
    unverified_claim = await DeterministicCompletionValidator().assess(
        state,
        FinishAction(
            summary=claims.summary,
            structured_finish=claims,
            legacy=False,
        ),
    )
    reference = StructuredFinish(
        kind="reference_only",
        summary="模型正文中的9999不能展示。",
        claims=[],
    )
    safe_reference = await DeterministicCompletionValidator().assess(
        state,
        FinishAction(
            summary=reference.summary,
            structured_finish=reference,
            legacy=False,
        ),
    )

    assert plain.status == "revise"
    assert plain.reason_code == "structured_finish_required"
    assert unverified_claim.status == "revise"
    assert unverified_claim.reason_code == "claim_result_not_found"
    assert safe_reference.status == "accept"
    assert safe_reference.safe_summary == "查询已完成，详细结果请查看数据面板。"


class ProductionTextPlanner:
    async def decide(self, state: HarnessState):
        from full_view_agent.application.harness import ToolAction

        if not state.tool_results:
            return ToolAction(tool_id="governance.query_housing_metrics", arguments={})
        return FinishAction(summary="住宅出租为884套。", legacy=False)


class SuccessfulExecutor:
    async def execute(self, **kwargs):
        return successful_housing_result(tool_call_id=kwargs["tool_call_id"])


@pytest.mark.asyncio
async def test_production_text_finish_revises_once_then_stops_safely() -> None:
    from full_view_agent.application.harness import AgentHarness

    result = await AgentHarness(
        tool_executor=SuccessfulExecutor(),
        validator=DeterministicCompletionValidator(),
    ).run(
        planner=ProductionTextPlanner(),
        auth_context=auth_context(),
    )

    assert result.state.completion_feedback_code == "structured_claims_required"
    assert result.state.completion_revision_count == 1
    assert result.summary == (
        "抱歉，当前回答仍包含无法由查询结果核验的内容，已停止生成结论。"
    )
