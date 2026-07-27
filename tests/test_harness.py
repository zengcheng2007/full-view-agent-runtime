from datetime import UTC, datetime

import pytest

from full_view_agent.application.errors import BudgetExceeded, LoopDetected
from full_view_agent.application.harness import (
    AgentHarness,
    DeterministicCompletionValidator,
    FinishAction,
    HarnessLimits,
    HarnessState,
    ToolAction,
)
from full_view_agent.domain.models import AuthContext, ToolResult


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
async def test_deterministic_validator_rejects_empty_summary() -> None:
    validator = DeterministicCompletionValidator()
    assert (
        await validator.validate(HarnessState(), FinishAction(summary="  "))
        is False
    )


@pytest.mark.asyncio
async def test_deterministic_validator_accepts_followup_grounded_by_inherited_evidence() -> None:
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
        is True
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
