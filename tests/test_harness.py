from datetime import UTC, datetime

import pytest

from full_view_agent.application.answer_grounding import build_fact_ledger
from full_view_agent.application.errors import BudgetExceeded, LoopDetected
from full_view_agent.application.harness import (
    AgentHarness,
    DeterministicCompletionValidator,
    FinishAction,
    HarnessLimits,
    HarnessState,
    ToolAction,
)
from full_view_agent.domain.models import (
    AreaCandidate,
    AreaCandidatesData,
    AreaCandidatesResult,
    AuthContext,
    GovernanceObjectRef,
    HousingLeaseTypeRow,
    HousingLeaseTypeTable,
    ObjectProfileData,
    ObjectProfileField,
    ObjectProfileResult,
    TableDataResult,
    ToolResult,
)


def auth_context() -> AuthContext:
    return AuthContext.model_validate(
        {
            "auth_context_id": "authctx-01",
            "auth_context_fingerprint": "sha256:auth-context-01",
            "principal": {
                "tenant_id": "tenant-hz",
                "user_id": "user-01",
                "org_id": "org-01",
                "roles": ["governance_analyst"],
            },
            "application": {
                "app_id": "full_information_view",
                "agent_id": "governance_general_agent",
            },
            "entitlements": ["governance.population.aggregate.read"],
            "data_scopes": {
                "areas": [{"area_code": "330106", "include_descendants": True}],
                "datasets": ["population"],
                "field_policy_set": "governance_analyst_v1",
            },
            "purpose": "interactive_analysis",
            "session_id": "session-01",
            "run_id": "run-01",
            "credential_ref": "cred-01",
            "issued_at": datetime(2099, 1, 1, tzinfo=UTC),
            "expires_at": datetime(2099, 1, 1, 0, 5, tzinfo=UTC),
            "policy_version": "test-v1",
        }
    )


class DeniedToolExecutor:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def execute(self, *, tool_call_id, tool_id, raw_arguments, auth_context):
        self.calls.append(raw_arguments)
        return ToolResult(
            tool_call_id=tool_call_id,
            tool_id=tool_id,
            tool_version="1.0.0",
            status="denied",
            summary="拒绝",
        )


def successful_area_result(*, tool_call_id: str = "tc-housing") -> ToolResult:
    return ToolResult(
        tool_call_id=tool_call_id,
        tool_id="governance.resolve_area",
        tool_version="1.0",
        status="success",
        summary="查询成功",
        data_result=AreaCandidatesResult(
            result_id="res-xihu",
            data_schema_ref="schema://data/area-candidates/1.0.0",
            result_fingerprint="sha256:xihu",
            candidate_count=1,
            data=AreaCandidatesData(
                candidates=[
                    AreaCandidate(
                        area_code="330106",
                        area_name="西湖区",
                        level="district",
                    )
                ],
                ambiguous=False,
                resolved_area_code="330106",
            ),
        ),
    )


def successful_housing_result(
    *, tool_call_id: str = "tc-housing"
) -> ToolResult:
    rows = [
        HousingLeaseTypeRow(lease_type=name, dwelling_count=884)
        for name in ("工业出租", "商铺出租", "公寓出租", "群租房", "住宅出租")
    ]
    return ToolResult(
        tool_call_id=tool_call_id,
        tool_id="governance.query_housing_metrics",
        tool_version="1.0",
        status="success",
        summary="查询成功",
        data_result=TableDataResult(
            result_id="res-housing",
            data_schema_ref="schema://data/housing-lease-type-table/1.0.0",
            result_fingerprint="sha256:housing",
            data=HousingLeaseTypeTable(rows=rows),
            row_count=len(rows),
        ),
    )


def housing_comparison_result(
    *, result_id: str, residential: int, commercial: int
) -> ToolResult:
    return successful_housing_result().model_copy(
        update={
            "data_result": TableDataResult(
                result_id=result_id,
                data_schema_ref="schema://data/housing-lease-type-table/1.0.0",
                result_fingerprint=f"sha256:{result_id}",
                data=HousingLeaseTypeTable(
                    rows=[
                        HousingLeaseTypeRow(
                            lease_type="住宅出租", dwelling_count=residential
                        ),
                        HousingLeaseTypeRow(
                            lease_type="商铺出租", dwelling_count=commercial
                        ),
                    ]
                ),
                row_count=2,
            )
        }
    )


class SuccessfulToolExecutor:
    async def execute(
        self, *, tool_call_id, tool_id, raw_arguments, auth_context
    ) -> ToolResult:
        del tool_id, raw_arguments, auth_context
        return successful_housing_result(tool_call_id=tool_call_id)


class SuccessfulAreaHousingExecutor:
    async def execute(
        self, *, tool_call_id, tool_id, raw_arguments, auth_context
    ) -> ToolResult:
        del raw_arguments, auth_context
        if tool_id == "governance.resolve_area":
            return successful_area_result(tool_call_id=tool_call_id)
        return successful_housing_result(tool_call_id=tool_call_id)


class RepeatingPlanner:
    async def decide(self, state: HarnessState):
        return ToolAction(tool_id="governance.resolve_area", arguments={"query": "西湖区"})


class VaryingPlanner:
    async def decide(self, state: HarnessState):
        return ToolAction(
            tool_id="governance.resolve_area",
            arguments={"query": f"西湖区-{state.model_turns}"},
        )


class OneToolPlanner:
    async def decide(self, state: HarnessState):
        if not state.tool_results:
            return ToolAction(
                tool_id="governance.resolve_area",
                arguments={"query": "西湖区"},
            )
        return FinishAction(summary="已完成")


class RepeatingUnsupportedInferencePlanner:
    def __init__(self) -> None:
        self.seen_feedback: list[str | None] = []

    async def decide(self, state: HarnessState):
        self.seen_feedback.append(state.completion_feedback)
        if not state.tool_results:
            return ToolAction(
                tool_id="governance.resolve_area",
                arguments={"query": "西湖区"},
            )
        if len(state.tool_results) == 1:
            return ToolAction(
                tool_id="governance.query_housing_metrics",
                arguments={"query": {"scope": {"area_code": "330106"}}},
            )
        return FinishAction(
            summary=(
                "西湖区出租房共 4420 套。\n"
                "> 注：各类型相同，可能反映当前数据源采用固定统计口径。"
            )
        )


class RepeatingUnsupportedAreaPlanner:
    async def decide(self, state: HarnessState):
        if not state.tool_results:
            return ToolAction(
                tool_id="governance.resolve_area",
                arguments={"query": "西湖区"},
            )
        return FinishAction(summary="拱墅区查询完成。")


class RejectAfterRevisionPlanner:
    async def decide(self, state: HarnessState):
        if not state.tool_results:
            return ToolAction(
                tool_id="governance.resolve_area",
                arguments={"query": "西湖区"},
            )
        if state.completion_revision_count == 0:
            return FinishAction(summary="拱墅区查询完成。")
        return FinishAction(summary="")


@pytest.mark.asyncio
async def test_harness_blocks_repeated_tool_call_loop_before_third_execution() -> None:
    executor = DeniedToolExecutor()
    harness = AgentHarness(
        tool_executor=executor,
        limits=HarnessLimits(
            max_model_turns=10,
            max_tool_calls=10,
            max_consecutive_failures=10,
            max_no_progress=10,
            repeated_call_limit=2,
        ),
    )

    with pytest.raises(LoopDetected):
        await harness.run(planner=RepeatingPlanner(), auth_context=auth_context())

    assert len(executor.calls) == 2


@pytest.mark.asyncio
async def test_harness_enforces_shared_tool_call_budget() -> None:
    executor = DeniedToolExecutor()
    harness = AgentHarness(
        tool_executor=executor,
        limits=HarnessLimits(
            max_model_turns=10,
            max_tool_calls=2,
            max_consecutive_failures=10,
            max_no_progress=10,
            repeated_call_limit=2,
        ),
    )

    with pytest.raises(BudgetExceeded):
        await harness.run(planner=VaryingPlanner(), auth_context=auth_context())

    assert len(executor.calls) == 2


@pytest.mark.asyncio
async def test_harness_finishes_after_tool_planner_returns_a_valid_summary() -> None:
    harness = AgentHarness(tool_executor=DeniedToolExecutor())

    result = await harness.run(planner=OneToolPlanner(), auth_context=auth_context())

    assert result.summary == "已完成"
    assert result.state.model_turns == 2
    assert result.state.tool_calls == 1
    assert result.state.tool_results[-1].status == "denied"


@pytest.mark.asyncio
async def test_harness_exposes_one_turn_plan_control_without_executing_tool() -> None:
    harness = AgentHarness(tool_executor=DeniedToolExecutor())
    control, action, summary = await harness.plan_once(
        planner=OneToolPlanner(), control=harness.begin()
    )

    assert isinstance(action, ToolAction)
    assert summary is None
    assert control.state.model_turns == 1
    assert control.state.tool_calls == 0


@pytest.mark.asyncio
async def test_restored_budget_fails_closed_if_wall_clock_moves_backwards() -> None:
    original = AgentHarness(
        tool_executor=DeniedToolExecutor(),
        clock=lambda: 1_000.0,
    )
    control = original.begin()
    restarted = AgentHarness(
        tool_executor=DeniedToolExecutor(),
        clock=lambda: 999.0,
    )

    with pytest.raises(BudgetExceeded, match="clock moved backwards"):
        await restarted.plan_action_once(
            planner=OneToolPlanner(),
            control=control,
        )


@pytest.mark.asyncio
async def test_deterministic_validator_accepts_successful_tool_result() -> None:
    validator = DeterministicCompletionValidator()
    state = HarnessState(
        tool_results=(
            ToolResult(
                tool_call_id="tc-1",
                tool_id="governance.resolve_area",
                tool_version="1.0",
                status="denied",
                summary="无权访问该区划",
            ),
        )
    )
    # denied only → no fabricated numbers → accept
    assert await validator.validate(state, FinishAction(summary="抱歉，无权访问该区域")) is True


@pytest.mark.asyncio
async def test_deterministic_validator_requests_revision_for_unsupported_inference() -> None:
    validator = DeterministicCompletionValidator()
    state = HarnessState(
        tool_results=(
            successful_area_result(),
            successful_housing_result(),
        )
    )
    action = FinishAction(
        summary=(
            "西湖区出租房共 4420 套。\n"
            "> 注：各类型相同，可能反映当前数据源采用固定统计口径。"
        )
    )

    assessment = await validator.assess(state, action)

    assert assessment.status == "revise"
    assert assessment.reason_code == "unsupported_inference"
    assert assessment.feedback is not None
    assert "仅保留已验证事实" in assessment.feedback
    assert assessment.safe_summary == "西湖区出租房共 4420 套。"
    assert await validator.validate(state, action) is False


@pytest.mark.asyncio
async def test_deterministic_validator_checks_numbers_against_fact_ledger() -> None:
    validator = DeterministicCompletionValidator()
    state = HarnessState(tool_results=(successful_housing_result(),))

    grounded = await validator.assess(
        state,
        FinishAction(
            summary=(
                "共 5 类，每类 884 套，合计 4,420 套，"
                "各类型占比均为 20%。"
            )
        ),
    )
    fabricated = await validator.assess(
        state,
        FinishAction(summary="共 5 类，每类 884 套，合计 4,999 套。"),
    )

    assert grounded.status == "accept"
    assert fabricated.status == "revise"
    assert fabricated.reason_code == "unsupported_number"
    assert "4,999" not in (fabricated.safe_summary or "")
    ledger = build_fact_ledger(state.tool_results)
    assert ledger.sources["number:4420"] == {"res-housing"}


@pytest.mark.asyncio
async def test_deterministic_validator_checks_area_and_object_facts() -> None:
    validator = DeterministicCompletionValidator()
    object_result = ToolResult(
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
                    object_type="building",
                    object_id="building-12",
                ),
                area_code="330106",
                title="翠苑一区12幢",
                fields=[
                    ObjectProfileField(
                        field_id="address",
                        label="地址",
                        value="翠苑一区12幢",
                        classification="public",
                    )
                ],
            ),
        ),
    )
    state = HarnessState(tool_results=(successful_area_result(), object_result))

    grounded = await validator.assess(
        state, FinishAction(summary="西湖区的翠苑一区12幢查询完成。")
    )
    wrong_area = await validator.assess(
        state, FinishAction(summary="拱墅区的翠苑一区12幢查询完成。")
    )
    wrong_object = await validator.assess(
        state, FinishAction(summary="西湖区的翠苑一区13幢查询完成。")
    )

    assert grounded.status == "accept"
    assert wrong_area.reason_code == "unsupported_area"
    assert wrong_object.reason_code == "unsupported_object"


@pytest.mark.asyncio
async def test_deterministic_validator_does_not_treat_area_dimensions_as_names() -> None:
    validator = DeterministicCompletionValidator()
    state = HarnessState(tool_results=(successful_housing_result(),))

    assessment = await validator.assess(
        state,
        FinishAction(
            summary="杭州全市已按区县汇总，可查看各区县和排名靠前的几个街道。"
        ),
    )

    assert assessment.status == "accept"


@pytest.mark.asyncio
async def test_deterministic_validator_checks_comparative_judgements() -> None:
    validator = DeterministicCompletionValidator()
    result = successful_housing_result().model_copy(
        update={
            "data_result": TableDataResult(
                result_id="res-ranked",
                data_schema_ref="schema://data/housing-lease-type-table/1.0.0",
                result_fingerprint="sha256:ranked",
                data=HousingLeaseTypeTable(
                    rows=[
                        HousingLeaseTypeRow(
                            lease_type="住宅出租", dwelling_count=100
                        ),
                        HousingLeaseTypeRow(
                            lease_type="商铺出租", dwelling_count=80
                        ),
                        HousingLeaseTypeRow(
                            lease_type="公寓出租", dwelling_count=80
                        ),
                    ]
                ),
                row_count=3,
            )
        }
    )
    state = HarnessState(tool_results=(result,))

    maximum = await validator.assess(
        state, FinishAction(summary="住宅出租最多，为 100 套。")
    )
    tied = await validator.assess(
        state, FinishAction(summary="商铺出租和公寓出租并列最少，均为 80 套。")
    )
    false_maximum = await validator.assess(
        state, FinishAction(summary="商铺出租最多，为 80 套。")
    )
    false_same = await validator.assess(
        state, FinishAction(summary="三类出租房数量全部相同。")
    )

    assert maximum.status == "accept"
    assert tied.status == "accept"
    assert false_maximum.reason_code == "unsupported_judgement"
    assert false_same.reason_code == "unsupported_judgement"


@pytest.mark.asyncio
async def test_deterministic_validator_rejects_label_number_mismatch() -> None:
    validator = DeterministicCompletionValidator()
    result = successful_housing_result().model_copy(
        update={
            "data_result": TableDataResult(
                result_id="res-label-values",
                data_schema_ref="schema://data/housing-lease-type-table/1.0.0",
                result_fingerprint="sha256:label-values",
                data=HousingLeaseTypeTable(
                    rows=[
                        HousingLeaseTypeRow(
                            lease_type="住宅出租", dwelling_count=100
                        ),
                        HousingLeaseTypeRow(
                            lease_type="商铺出租", dwelling_count=80
                        ),
                    ]
                ),
                row_count=2,
            )
        }
    )

    assessment = await validator.assess(
        HarnessState(tool_results=(result,)),
        FinishAction(summary="住宅出租为 80 套，商铺出租为 100 套。"),
    )

    assert assessment.status == "revise"
    assert assessment.reason_code == "unsupported_judgement"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "summary",
    (
        "住宅出租为850套，商铺出租为3200套。",
        "住宅850套，商铺3200套。",
        "住宅出租：850，商铺出租：3200。",
    ),
)
async def test_deterministic_validator_rejects_adjacent_label_number_mismatch(
    summary: str,
) -> None:
    result = housing_comparison_result(
        result_id="res-adjacent-values", residential=3200, commercial=850
    )

    assessment = await DeterministicCompletionValidator().assess(
        HarnessState(tool_results=(result,)), FinishAction(summary=summary)
    )

    assert assessment.status == "revise"
    assert assessment.reason_code == "unsupported_judgement"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "summary",
    (
        "住宅出租3200套，商铺出租850套。",
        (
            "结果A：住宅出租3200套，商铺出租850套；"
            "结果B：住宅出租1000套，商铺出租500套。"
        ),
    ),
)
async def test_deterministic_validator_keeps_same_labels_scoped_across_results(
    summary: str,
) -> None:
    result_a = housing_comparison_result(
        result_id="res-source-a", residential=3200, commercial=850
    )
    result_b = housing_comparison_result(
        result_id="res-source-b", residential=1000, commercial=500
    )

    assessment = await DeterministicCompletionValidator().assess(
        HarnessState(tool_results=(result_a, result_b)),
        FinishAction(summary=summary),
    )

    assert assessment.status == "accept"


@pytest.mark.asyncio
async def test_deterministic_validator_accepts_respective_label_values() -> None:
    validator = DeterministicCompletionValidator()
    result = successful_housing_result().model_copy(
        update={
            "data_result": TableDataResult(
                result_id="res-respective-values",
                data_schema_ref="schema://data/housing-lease-type-table/1.0.0",
                result_fingerprint="sha256:respective-values",
                data=HousingLeaseTypeTable(
                    rows=[
                        HousingLeaseTypeRow(
                            lease_type="住宅出租", dwelling_count=100
                        ),
                        HousingLeaseTypeRow(
                            lease_type="商铺出租", dwelling_count=80
                        ),
                    ]
                ),
                row_count=2,
            )
        }
    )

    assessment = await validator.assess(
        HarnessState(tool_results=(result,)),
        FinishAction(summary="住宅出租和商铺出租分别为 100 套和 80 套。"),
    )

    assert assessment.status == "accept"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "summary",
    (
        "拱墅区有 884 套出租房。",
        "拱墅区为本次查询范围。",
        "拱墅区最多。",
    ),
)
async def test_deterministic_validator_checks_area_before_claim_verbs(
    summary: str,
) -> None:
    assessment = await DeterministicCompletionValidator().assess(
        HarnessState(
            tool_results=(successful_area_result(), successful_housing_result())
        ),
        FinishAction(summary=summary),
    )

    assert assessment.status == "revise"
    assert assessment.reason_code == "unsupported_area"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "summary",
    (
        "最高的是商铺出租，为 80 套。",
        "最低的是住宅出租，为 100 套。",
    ),
)
async def test_deterministic_validator_checks_inverted_extrema(summary: str) -> None:
    validator = DeterministicCompletionValidator()
    result = successful_housing_result().model_copy(
        update={
            "data_result": TableDataResult(
                result_id="res-inverted-rank",
                data_schema_ref="schema://data/housing-lease-type-table/1.0.0",
                result_fingerprint="sha256:inverted-rank",
                data=HousingLeaseTypeTable(
                    rows=[
                        HousingLeaseTypeRow(
                            lease_type="住宅出租", dwelling_count=100
                        ),
                        HousingLeaseTypeRow(
                            lease_type="商铺出租", dwelling_count=80
                        ),
                    ]
                ),
                row_count=2,
            )
        }
    )

    assessment = await validator.assess(
        HarnessState(tool_results=(result,)), FinishAction(summary=summary)
    )

    assert assessment.status == "revise"
    assert assessment.reason_code == "unsupported_judgement"


@pytest.mark.asyncio
async def test_deterministic_validator_accepts_all_same_and_derived_percentages() -> None:
    validator = DeterministicCompletionValidator()
    assessment = await validator.assess(
        HarnessState(tool_results=(successful_housing_result(),)),
        FinishAction(
            summary="5 类出租房数量全部相同，每类 884 套，各占 20%。"
        ),
    )

    assert assessment.status == "accept"


@pytest.mark.asyncio
async def test_harness_revises_once_then_uses_safe_summary_without_loop() -> None:
    planner = RepeatingUnsupportedInferencePlanner()
    harness = AgentHarness(
        tool_executor=SuccessfulAreaHousingExecutor(),
        validator=DeterministicCompletionValidator(),
    )

    result = await harness.run(planner=planner, auth_context=auth_context())

    assert result.summary == "西湖区出租房共 4420 套。"
    assert result.state.completion_revision_count == 1
    assert "可能反映" not in result.summary
    assert planner.seen_feedback == [
        None,
        None,
        None,
        "回答包含无证据推断；请仅保留已验证事实和可复算计算。",
    ]


@pytest.mark.asyncio
async def test_harness_second_failed_revision_ends_with_stable_safe_summary() -> None:
    harness = AgentHarness(
        tool_executor=SuccessfulToolExecutor(),
        validator=DeterministicCompletionValidator(),
    )

    result = await harness.run(
        planner=RepeatingUnsupportedAreaPlanner(),
        auth_context=auth_context(),
    )

    assert result.summary == (
        "抱歉，当前回答仍包含无法由查询结果核验的内容，已停止生成结论。"
    )
    assert result.state.completion_revision_count == 1
    assert result.state.completion_feedback_code == "unsupported_area"


@pytest.mark.asyncio
async def test_harness_reject_after_revision_ends_safely_without_loop() -> None:
    harness = AgentHarness(
        tool_executor=SuccessfulToolExecutor(),
        validator=DeterministicCompletionValidator(),
        limits=HarnessLimits(max_no_progress=1),
    )

    result = await harness.run(
        planner=RejectAfterRevisionPlanner(),
        auth_context=auth_context(),
    )

    assert result.summary == (
        "抱歉，当前回答仍包含无法由查询结果核验的内容，已停止生成结论。"
    )
    assert result.state.completion_revision_count == 1


@pytest.mark.asyncio
async def test_deterministic_validator_rejects_empty_summary() -> None:
    validator = DeterministicCompletionValidator()
    assert (
        await validator.validate(HarnessState(), FinishAction(summary="  "))
        is False
    )


@pytest.mark.asyncio
async def test_deterministic_validator_rejects_unstructured_inherited_fact() -> None:
    validator = DeterministicCompletionValidator()
    state = HarnessState(
        inherited_result_ids=("res-prior",),
        inherited_evidence_ids=("evd-prior",),
    )

    assert (
        await validator.validate(
            state,
            FinishAction(summary="北山街道 2 人，灵隐街道 1 人。"),
        )
        is False
    )


@pytest.mark.asyncio
async def test_deterministic_validator_accepts_allowed_no_result_prefixes() -> None:
    validator = DeterministicCompletionValidator()
    state = HarnessState()
    assert (
        await validator.validate(state, FinishAction(summary="抱歉，无法完成该查询"))
        is True
    )
    assert (
        await validator.validate(
            state, FinishAction(summary="请提供具体的区划名称")
        )
        is True
    )
    assert (
        await validator.validate(
            state, FinishAction(summary="我可以查询授权范围内的治理数据。")
        )
        is True
    )


@pytest.mark.asyncio
async def test_deterministic_validator_rejects_hallucinated_data_without_tools() -> None:
    validator = DeterministicCompletionValidator()
    state = HarnessState()
    # All must be rejected - no tool results, summary contains numbers
    assert (
        await validator.validate(
            state,
            FinishAction(summary="根据当前数据，西湖区独居老人共 1234 人"),
        )
        is False
    )
    assert (
        await validator.validate(
            state,
            FinishAction(summary="以下是查询结果：西湖区独居老人共 1234 人"),
        )
        is False
    )
    assert (
        await validator.validate(
            state,
            FinishAction(summary="西湖区独居老人共 1234 人"),
        )
        is False
    )


@pytest.mark.asyncio
async def test_deterministic_validator_rejects_fabrication_with_numbers() -> None:
    """Reviewer-specified anti-patterns: numbers in summary without success results."""
    validator = DeterministicCompletionValidator()

    # No tool results at all
    assert (
        await validator.validate(
            HarnessState(),
            FinishAction(summary="该区域独居老人有1234人"),
        )
        is False
    )
    assert (
        await validator.validate(
            HarnessState(),
            FinishAction(summary="没有问题，西湖区独居老人1234人"),
        )
        is False
    )

    # Failed tool result + hallucinated success
    assert (
        await validator.validate(
            HarnessState(
                tool_results=(
                    ToolResult(
                        tool_call_id="tc-1",
                        tool_id="governance.query_population_metrics",
                        tool_version="1.0",
                        status="failed",
                        summary="上游服务超时",
                        warnings=["upstream_timeout"],
                    ),
                )
            ),
            FinishAction(summary="Tool 失败后：查询成功，人数为1234人"),
        )
        is False
    )

    # Denied tool result + hallucinated success
    assert (
        await validator.validate(
            HarnessState(
                tool_results=(
                    ToolResult(
                        tool_call_id="tc-1",
                        tool_id="governance.query_population_metrics",
                        tool_version="1.0",
                        status="denied",
                        summary="无权访问",
                    ),
                )
            ),
            FinishAction(summary="查询成功，西湖区独居老人1234人"),
        )
        is False
    )


@pytest.mark.asyncio
async def test_deterministic_validator_accepts_failure_acknowledgement() -> None:
    validator = DeterministicCompletionValidator()
    state = HarnessState(
        tool_results=(
            ToolResult(
                tool_call_id="tc-1",
                tool_id="governance.query_population_metrics",
                tool_version="1.0",
                status="failed",
                summary="上游服务超时",
                warnings=["upstream_timeout"],
            ),
        )
    )
    assert (
        await validator.validate(
            state,
            FinishAction(summary="抱歉，上游服务暂时不可用，请稍后重试。"),
        )
        is True
    )


@pytest.mark.asyncio
async def test_deterministic_validator_accepts_real_data_result() -> None:
    validator = DeterministicCompletionValidator()
    state = HarnessState(
        tool_results=(
            ToolResult(
                tool_call_id="tc-1",
                tool_id="governance.resolve_area",
                tool_version="1.0",
                status="denied",
                summary="无权",
            ),
            ToolResult(
                tool_call_id="tc-2",
                tool_id="governance.query_population_metrics",
                tool_version="1.0",
                status="success",
                summary="查询完成",
                data_result=_make_table_result(),
            ),
        )
    )
    assert (
        await validator.validate(
            state,
            FinishAction(summary="西湖区独居老人共 1234 人"),
        )
        is True
    )


def _make_table_result():
    from full_view_agent.domain.models import (
        PopulationMetricRow,
        PopulationMetricTable,
        TableDataResult,
    )
    return TableDataResult(
        result_id="res-1",
        data_schema_ref="schema://data/population-metric-table/1.0",
        result_fingerprint="sha256:test",
        data=PopulationMetricTable(
            rows=[
                PopulationMetricRow(
                    area_code="330106001",
                    area_name="北山街道",
                    person_count=1234,
                ),
            ],
        ),
        row_count=1,
    )
