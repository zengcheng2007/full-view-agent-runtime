import pytest

from full_view_agent.application.answer_claims import AnswerClaim, StructuredFinish
from full_view_agent.application.harness import (
    DeterministicCompletionValidator,
    FinishAction,
    HarnessState,
)
from full_view_agent.domain.models import (
    GovernanceObjectRef,
    ObjectProfileData,
    ObjectProfileResult,
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
    assert assessment.safe_summary == "住宅出租的dwelling_count为884。"


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
async def test_success_result_cannot_hide_factual_text_in_capability_finish() -> None:
    structured = StructuredFinish(
        kind="capability",
        summary="住宅出租有9999套。",
        claims=[],
    )

    assessment = await DeterministicCompletionValidator().assess(
        HarnessState(tool_results=(successful_housing_result(),)),
        FinishAction(summary=structured.summary, structured_finish=structured),
    )

    assert assessment.status == "revise"
    assert assessment.reason_code == "structured_finish_kind_not_allowed"


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
