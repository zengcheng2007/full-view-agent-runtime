import json
from pathlib import Path

import pytest

from full_view_agent.application.answer_claims import FINISH_TOOL_ID
from full_view_agent.application.model_provider import (
    ModelRequest,
    ModelResponse,
    ModelToolCall,
    ModelUsage,
)
from full_view_agent.evaluation.contracts import (
    EvalCase,
    EvalErrorStep,
    EvalExpected,
    EvalFinishStep,
    EvalTrace,
)
from full_view_agent.evaluation.loader import load_eval_case
from full_view_agent.evaluation.runner import EvalRunner, StaticEvalEnvironment, _grade
from full_view_agent.infrastructure.governance_adapter import InMemoryGovernanceAdapter

EVAL_CASES = Path(__file__).parents[1] / "evals" / "cases"


def test_open_eval_grades_required_tools_without_fixing_exact_call_sequence() -> None:
    expected = EvalExpected(
        terminal_status="completed",
        outcome="success",
        completion_reason_code="goal_completed",
        required_tool_ids=[
            "governance.resolve_area",
            "governance.semantic_query",
        ],
        forbidden_tool_ids=["governance.query_event_metrics"],
        max_tool_calls=4,
    )

    grades = _grade(
        expected,
        terminal_status="completed",
        outcome="success",
        completion_reason_code="goal_completed",
        tool_ids=[
            "governance.resolve_area",
            "governance.resolve_area",
            "governance.semantic_query",
        ],
        evidence_count=3,
        event_types=["tool.completed"] * 3,
        final_answer="办结率查询完成",
    )

    assert all(grade.passed for grade in grades)


def test_open_eval_rejects_missing_forbidden_or_excessive_tool_calls() -> None:
    expected = EvalExpected(
        terminal_status="completed",
        outcome="success",
        completion_reason_code="goal_completed",
        required_tool_ids=["governance.semantic_query"],
        forbidden_tool_ids=["governance.query_event_metrics"],
        max_tool_calls=1,
    )

    grades = _grade(
        expected,
        terminal_status="completed",
        outcome="success",
        completion_reason_code="goal_completed",
        tool_ids=[
            "governance.query_event_metrics",
            "governance.resolve_area",
        ],
        evidence_count=0,
        event_types=["tool.completed"] * 2,
        final_answer="无法完成",
    )
    by_name = {grade.name: grade for grade in grades}

    assert by_name["required_tool_ids"].passed is False
    assert by_name["forbidden_tool_ids"].passed is False
    assert by_name["max_tool_calls"].passed is False


def test_open_eval_accepts_any_supported_boundary_wording() -> None:
    expected = EvalExpected(
        terminal_status="completed",
        outcome="success",
        completion_reason_code="goal_completed",
        required_answer_any_substrings=["不支持", "未提供", "无法按年龄"],
    )

    grades = _grade(
        expected,
        terminal_status="completed",
        outcome="success",
        completion_reason_code="goal_completed",
        tool_ids=[],
        evidence_count=0,
        event_types=[],
        final_answer="当前目录未提供年龄段筛选能力。",
    )

    assert all(grade.passed for grade in grades)


class TwoStepLiveModelProvider:
    def __init__(self) -> None:
        self.call_count = 0

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.call_count += 1
        if self.call_count == 1:
            return ModelResponse(
                content=None,
                tool_calls=(
                    ModelToolCall(
                        tool_id="governance.semantic_query",
                        arguments={
                            "spec": {
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
                        },
                    ),
                ),
                finish_reason="tool_calls",
                usage=ModelUsage(
                    prompt_tokens=80,
                    completion_tokens=20,
                    total_tokens=100,
                ),
            )
        observation = json.loads(request.messages[-1].content or "{}")
        data_result = observation["data_result"]
        row = data_result["sample_rows"][0]
        return ModelResponse(
            content=None,
            tool_calls=(
                ModelToolCall(
                    tool_id=FINISH_TOOL_ID,
                    arguments={
                        "kind": "claims",
                        "summary": "人口指标查询已完成",
                        "claims": [
                            {
                                "claim_id": "claim-1",
                                "result_id": data_result["result_id"],
                                "result_fingerprint": data_result[
                                    "result_fingerprint"
                                ],
                                "collection": "rows",
                                "row_locator": {"area_code": row["area_code"]},
                                "field": "person_count",
                                "operation": "value",
                                "value": row["person_count"],
                            }
                        ],
                    },
                ),
            ),
            finish_reason="tool_calls",
            usage=ModelUsage(
                prompt_tokens=100,
                completion_tokens=10,
                total_tokens=110,
            ),
        )


def population_case(*, area_code: str = "330106") -> EvalCase:
    return EvalCase.model_validate(
        {
            "case_id": "population-success",
            "description": "授权范围内人口聚合查询",
            "user_message": "查询西湖区独居老人数量",
            "auth": {
                "area_codes": ["330106"],
                "datasets": ["population"],
                "entitlements": ["governance.population.aggregate.read"],
                "field_policy_set": "governance_analyst_v1",
            },
            "model_steps": [
                {
                    "type": "tool_call",
                    "tool_id": "governance.semantic_query",
                    "arguments": {
                        "spec": {
                            "subject": "population",
                            "metrics": ["person_count"],
                            "scope": {"area_code": area_code},
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
                    "total_tokens": 120,
                },
                {
                    "type": "finish",
                    "content": "人口指标查询已完成",
                    "total_tokens": 30,
                },
            ],
            "expected": {
                "terminal_status": "completed",
                "outcome": "success",
                "completion_reason_code": "goal_completed",
                "tool_ids": ["governance.semantic_query"],
                "min_evidence_count": 1,
                "required_event_types": [
                    "tool.completed",
                    "evidence.available",
                    "run.completed",
                ],
            },
        }
    )


@pytest.mark.asyncio
async def test_eval_runner_executes_real_runtime_components_and_grades_success() -> None:
    trace = await EvalRunner().run(population_case())

    assert trace.passed is True
    assert trace.terminal_status == "completed"
    assert trace.tool_ids == ["governance.semantic_query"]
    assert len(trace.evidence_ids) == 1
    assert trace.total_tokens == 150
    assert trace.environment_kind == "static"
    assert trace.evidence_source_system == "eval_fixture"
    assert trace.runtime_version == "unknown"
    assert trace.outbound_requests == []
    assert all(grade.passed for grade in trace.grades)
    assert "查询西湖区独居老人数量" in trace.model_requests[0].messages[-1].content
    serialized = trace.model_dump_json()
    assert "credential_ref" not in serialized
    assert "cred-eval" not in serialized

    legacy_payload = trace.model_dump(
        exclude={
            "environment_kind",
            "evidence_source_system",
            "runtime_version",
            "outbound_requests",
        }
    )
    legacy_trace = EvalTrace.model_validate(legacy_payload)
    assert legacy_trace.environment_kind == "unknown"
    assert legacy_trace.evidence_source_system == "unknown"
    assert legacy_trace.runtime_version == "unknown"
    assert legacy_trace.outbound_requests == []


@pytest.mark.asyncio
async def test_eval_runner_can_compare_native_and_langgraph_semantics() -> None:
    native = await EvalRunner(orchestrator="native").run(population_case())
    langgraph = await EvalRunner(orchestrator="langgraph").run(population_case())

    assert native.passed is True
    assert langgraph.passed is True
    assert langgraph.terminal_status == native.terminal_status
    assert langgraph.outcome == native.outcome
    assert langgraph.completion_reason_code == native.completion_reason_code
    assert langgraph.tool_ids == native.tool_ids
    assert langgraph.event_types == native.event_types
    assert [grade.model_dump() for grade in langgraph.grades] == [
        grade.model_dump() for grade in native.grades
    ]


@pytest.mark.asyncio
async def test_eval_runner_semantic_entry_is_differential_safe() -> None:
    # S1-A：语义入口脚本用例在 Native 与 LangGraph 上外部行为一致。
    case = load_eval_case(EVAL_CASES / "planning-population-semantic-success.yaml")
    native = await EvalRunner(orchestrator="native").run(case)
    langgraph = await EvalRunner(orchestrator="langgraph").run(case)

    assert native.passed is True
    assert langgraph.passed is True
    assert native.tool_ids == ["governance.semantic_query"]
    assert langgraph.tool_ids == native.tool_ids
    assert langgraph.event_types == native.event_types
    assert langgraph.terminal_status == native.terminal_status == "completed"
    assert langgraph.outcome == native.outcome == "success"
    assert len(native.evidence_ids) == len(langgraph.evidence_ids) == 1
    assert [grade.model_dump() for grade in langgraph.grades] == [
        grade.model_dump() for grade in native.grades
    ]
    # 模型可见面包含语义虚拟 Tool，且不泄漏物理实现。
    serialized = langgraph.model_dump_json()
    assert "governance.semantic_query" in serialized
    assert "getNextSiteData" not in serialized


@pytest.mark.asyncio
@pytest.mark.parametrize("orchestrator", ["native", "langgraph"])
async def test_eval_runner_executes_follow_up_in_same_grounded_session(
    orchestrator: str,
) -> None:
    case = load_eval_case(EVAL_CASES / "multiturn-population-followup.yaml")

    trace = await EvalRunner(orchestrator=orchestrator).run(case)

    assert trace.passed is True
    assert len(trace.turns) == 2
    assert trace.turns[0].tool_ids == [
        "governance.resolve_area",
        "governance.semantic_query",
    ]
    assert trace.turns[1].tool_ids == []
    assert trace.turns[1].evidence_ids == []
    follow_up_request = trace.model_requests[-1]
    serialized = follow_up_request.model_dump_json()
    assert "哪个街道最多" in serialized
    assert "会话中已有可引用的历史结果" in serialized
    assert not any(
        '"person_count": 128' in (message.content or "")
        for message in follow_up_request.messages
    )


@pytest.mark.asyncio
async def test_eval_runner_replays_all_follow_up_turns() -> None:
    case = load_eval_case(EVAL_CASES / "multiturn-population-followup.yaml")
    runner = EvalRunner(orchestrator="langgraph")
    source = await runner.run(case)

    replayed = await runner.replay(case, source)

    assert replayed.passed is True
    assert replayed.replayed_from_eval_run_id == source.eval_run_id
    assert len(replayed.turns) == 2
    assert [turn.tool_ids for turn in replayed.turns] == [
        ["governance.resolve_area", "governance.semantic_query"],
        [],
    ]


@pytest.mark.asyncio
async def test_eval_runner_stops_follow_ups_after_failed_turn() -> None:
    case = load_eval_case(EVAL_CASES / "multiturn-population-followup.yaml")
    failed_first_turn = case.model_copy(
        update={
            "model_steps": [
                EvalErrorStep(
                    type="error",
                    error_code="model_provider_unavailable",
                    message="provider unavailable",
                )
            ]
        }
    )

    trace = await EvalRunner(orchestrator="langgraph").run(failed_first_turn)

    assert trace.passed is False
    assert len(trace.turns) == 1
    assert trace.turns[0].terminal_status == "failed"
    assert trace.turns[0].completion_reason_code == "model_provider_unavailable"


@pytest.mark.asyncio
async def test_eval_runner_replays_early_stopped_multiturn_trace() -> None:
    case = load_eval_case(EVAL_CASES / "multiturn-population-followup.yaml")
    failed_first_turn = case.model_copy(
        update={
            "model_steps": [
                EvalErrorStep(
                    type="error",
                    error_code="model_provider_unavailable",
                    message="provider unavailable",
                )
            ]
        }
    )
    runner = EvalRunner(orchestrator="langgraph")
    source = await runner.run(failed_first_turn)

    replayed = await runner.replay(failed_first_turn, source)

    assert replayed.passed is False
    assert replayed.replayed_from_eval_run_id == source.eval_run_id
    assert len(replayed.turns) == len(source.turns) == 1
    assert replayed.turns[0].terminal_status == "failed"
    assert (
        replayed.completion_reason_code
        == source.completion_reason_code
        == "model_provider_unavailable"
    )


@pytest.mark.asyncio
async def test_eval_runner_applies_one_token_budget_across_all_turns() -> None:
    case = load_eval_case(EVAL_CASES / "multiturn-population-followup.yaml")
    budget_case = case.model_copy(
        update={
            "model_steps": [
                EvalFinishStep(
                    type="finish",
                    content="当前能力说明已完成。",
                    total_tokens=7,
                )
            ],
            "expected": case.expected.model_copy(
                update={
                    "tool_ids": [],
                    "min_evidence_count": 0,
                    "required_event_types": ["run.completed"],
                }
            ),
            "follow_up_turns": [
                case.follow_up_turns[0].model_copy(
                    update={
                        "model_steps": [
                            EvalFinishStep(
                                type="finish",
                                content="当前能力说明已更新。",
                                total_tokens=4,
                            )
                        ]
                    }
                )
            ],
        }
    )

    trace = await EvalRunner(
        orchestrator="langgraph",
        max_total_tokens=10,
    ).run(budget_case)

    assert trace.passed is False
    assert len(trace.turns) == 2
    assert trace.turns[0].terminal_status == "completed"
    assert trace.turns[1].terminal_status == "failed"
    assert trace.turns[1].completion_reason_code == "budget_exceeded"


@pytest.mark.asyncio
async def test_eval_runner_does_not_call_provider_after_budget_is_exhausted() -> None:
    case = load_eval_case(EVAL_CASES / "multiturn-population-followup.yaml")
    budget_case = case.model_copy(
        update={
            "model_steps": [
                EvalFinishStep(
                    type="finish",
                    content="当前能力说明已完成。",
                    total_tokens=10,
                )
            ],
            "expected": case.expected.model_copy(
                update={
                    "tool_ids": [],
                    "min_evidence_count": 0,
                    "required_event_types": ["run.completed"],
                }
            ),
            "follow_up_turns": [
                case.follow_up_turns[0].model_copy(
                    update={
                        "model_steps": [
                            EvalFinishStep(
                                type="finish",
                                content="这一步不应被模型执行。",
                                total_tokens=4,
                            )
                        ]
                    }
                )
            ],
        }
    )

    trace = await EvalRunner(
        orchestrator="langgraph",
        max_total_tokens=10,
    ).run(budget_case)

    assert trace.passed is False
    assert trace.total_tokens == 10
    assert len(trace.model_steps) == 1
    assert len(trace.turns) == 2
    assert trace.turns[1].model_steps == []
    assert trace.turns[1].completion_reason_code == "budget_exceeded"


@pytest.mark.asyncio
async def test_eval_runner_records_live_provider_steps_and_version_metadata() -> None:
    provider = TwoStepLiveModelProvider()
    runner = EvalRunner(
        provider=provider,
        model_provider="openai_compatible",
        model_name="qwen-live-test",
    )

    trace = await runner.run(population_case())

    assert trace.passed is True
    assert trace.model_provider == "openai_compatible"
    assert trace.model_name == "qwen-live-test"
    assert trace.prompt_version == "full-view-governance-readonly-v13"
    assert [step.type for step in trace.model_steps] == ["tool_call", "finish"]
    finish_step = trace.model_steps[-1]
    assert isinstance(finish_step, EvalFinishStep)
    assert finish_step.structured_finish is not None
    assert finish_step.structured_finish.claims[0].operation == "value"

    replayed = await runner.replay(population_case(), trace)

    assert replayed.passed is True
    replay_finish = replayed.model_steps[-1]
    assert isinstance(replay_finish, EvalFinishStep)
    assert replay_finish.structured_finish is not None
    assert replay_finish.structured_finish.claims[0].result_fingerprint == (
        finish_step.structured_finish.claims[0].result_fingerprint
    )
    assert trace.total_tokens == 210
    assert provider.call_count == 2
    tool_messages = [
        message for message in trace.model_requests[1].messages if message.role == "tool"
    ]
    assert len(tool_messages) == 1
    assert tool_messages[0].tool_call_id is not None
    assert tool_messages[0].tool_call_id.startswith("tcl_")


@pytest.mark.asyncio
async def test_eval_runner_grades_policy_denial_for_out_of_scope_area() -> None:
    case = population_case(area_code="330108").model_copy(
        update={
            "case_id": "population-area-denied",
            "expected": population_case().expected.model_copy(
                update={
                    "outcome": "denied",
                    "completion_reason_code": "AREA_OUT_OF_SCOPE",
                    "min_evidence_count": 0,
                    "required_event_types": ["tool.completed", "run.completed"],
                }
            ),
        }
    )

    trace = await EvalRunner().run(case)

    assert trace.passed is True
    assert trace.outcome == "denied"
    assert trace.completion_reason_code == "AREA_OUT_OF_SCOPE"
    assert trace.evidence_ids == []


@pytest.mark.asyncio
async def test_eval_runner_grades_normalized_model_failure() -> None:
    case = EvalCase.model_validate(
        {
            "case_id": "model-timeout",
            "description": "模型超时可靠收敛",
            "user_message": "查询人口",
            "auth": {
                "area_codes": ["330106"],
                "datasets": ["population"],
                "entitlements": ["governance.population.aggregate.read"],
                "field_policy_set": "governance_analyst_v1",
            },
            "model_steps": [
                {
                    "type": "error",
                    "error_code": "model_timeout",
                    "message": "provider timed out",
                }
            ],
            "expected": {
                "terminal_status": "failed",
                "outcome": "failed",
                "completion_reason_code": "model_timeout",
                "required_event_types": ["run.failed"],
            },
        }
    )

    trace = await EvalRunner().run(case)

    assert trace.passed is True
    assert trace.terminal_status == "failed"
    assert trace.model_steps[0].type == "error"


@pytest.mark.asyncio
async def test_eval_runner_returns_failed_grade_instead_of_hiding_regression() -> None:
    case = population_case().model_copy(
        update={
            "expected": population_case().expected.model_copy(
                update={"completion_reason_code": "wrong-baseline"}
            )
        }
    )

    trace = await EvalRunner().run(case)

    assert trace.passed is False
    failed_grades = [grade for grade in trace.grades if not grade.passed]
    assert [grade.name for grade in failed_grades] == ["completion_reason_code"]
    assert failed_grades[0].actual == "goal_completed"


class CountingGovernanceAdapter:
    def __init__(self) -> None:
        self.calls = 0
        self.tool_ids: list[str] = []
        self._delegate = InMemoryGovernanceAdapter()

    async def execute(self, **kwargs):
        self.calls += 1
        self.tool_ids.append(kwargs["manifest"].tool_id)
        return await self._delegate.execute(**kwargs)


class CountingEvalEnvironment(StaticEvalEnvironment):
    def __init__(self, adapter: CountingGovernanceAdapter) -> None:
        self._counting_adapter = adapter

    @property
    def adapter(self):
        return self._counting_adapter


@pytest.mark.asyncio
async def test_eval_runner_injects_tool_timeout_before_downstream_call() -> None:
    case = EvalCase.model_validate(
        {
            "case_id": "population-tool-timeout",
            "description": "人口 Tool 超时后可靠失败",
            "user_message": "查询西湖区独居老人数量",
            "auth": {
                "area_codes": ["330106"],
                "datasets": ["population"],
                "entitlements": ["governance.population.aggregate.read"],
                "field_policy_set": "governance_analyst_v1",
            },
            "fault": {
                "type": "upstream_timeout",
                "tool_id": "governance.query_population_metrics",
            },
            "model_steps": [
                {
                    "type": "tool_call",
                    "tool_id": "governance.semantic_query",
                    "arguments": {
                        "spec": {
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
                    },
                },
                {"type": "finish", "content": "下游查询超时，当前无法完成。"},
            ],
            "expected": {
                "terminal_status": "failed",
                "outcome": "failed",
                "completion_reason_code": "upstream_timeout",
                "tool_ids": ["governance.semantic_query"],
                "min_evidence_count": 0,
                "required_event_types": ["tool.failed", "run.failed"],
            },
        }
    )
    adapter = CountingGovernanceAdapter()

    trace = await EvalRunner(
        environment=CountingEvalEnvironment(adapter)
    ).run(case)

    assert trace.passed is True
    assert trace.completion_reason_code == "upstream_timeout"
    assert adapter.calls == 0


@pytest.mark.parametrize(
    ("case_file", "reason_code"),
    [
        ("housing-upstream-timeout.yaml", "upstream_timeout"),
        ("housing-upstream-unavailable.yaml", "upstream_unavailable"),
        ("housing-upstream-contract-error.yaml", "upstream_contract_error"),
    ],
)
@pytest.mark.asyncio
async def test_housing_fault_gate_fails_once_before_business_adapter(
    case_file: str,
    reason_code: str,
) -> None:
    case = load_eval_case(EVAL_CASES / case_file)
    adapter = CountingGovernanceAdapter()

    trace = await EvalRunner(
        environment=CountingEvalEnvironment(adapter)
    ).run(case)

    assert trace.passed is True
    assert trace.terminal_status == "failed"
    assert trace.outcome == "failed"
    assert trace.completion_reason_code == reason_code
    # 模型只调用统一语义入口；内部仍编译到住房规范 Tool，并在故障注入
    # 层命中一次后停止，不能通过重新暴露规范 Tool 绕过语义门禁。
    assert trace.tool_ids.count("governance.semantic_query") == 1
    assert "governance.query_housing_metrics" not in trace.tool_ids
    assert trace.event_types.count("tool.failed") == 1
    assert "result.available" not in trace.event_types
    assert "evidence.available" not in trace.event_types
    assert trace.evidence_ids == []
    assert adapter.tool_ids == ["governance.resolve_area"]


@pytest.mark.asyncio
async def test_eval_runner_rejects_forbidden_claim_in_final_answer() -> None:
    case = EvalCase.model_validate(
        {
            "case_id": "area-empty-no-zero-claim",
            "description": "空区划不能被解释为人口数量为零",
            "user_message": "查询不存在区域的独居老人数量",
            "auth": {
                "area_codes": ["330106"],
                "datasets": ["administrative_area"],
                "entitlements": ["governance.area.read"],
            },
            "model_steps": [
                {
                    "type": "tool_call",
                    "tool_id": "governance.resolve_area",
                    "arguments": {"query": "不存在区域"},
                },
                {
                    "type": "finish",
                    "content": "未找到这个区划，因此独居老人数量 = 0。",
                },
            ],
            "expected": {
                "terminal_status": "completed",
                "outcome": "success",
                "completion_reason_code": "goal_completed",
                "tool_ids": ["governance.resolve_area"],
                "min_evidence_count": 1,
                "forbidden_answer_substrings": ["因此独居老人数量 = 0"],
            },
        }
    )

    trace = await EvalRunner().run(case)

    assert trace.passed is False
    failed = [grade for grade in trace.grades if not grade.passed]
    assert [grade.name for grade in failed] == ["forbidden_answer_substrings"]


@pytest.mark.asyncio
async def test_eval_runner_requires_scenario_specific_final_answer_fact() -> None:
    case = EvalCase.model_validate(
        {
            "case_id": "area-empty-requires-uncertainty",
            "description": "空区划回答必须说明不能推断业务指标",
            "user_message": "查询不存在区域的独居老人数量",
            "model_steps": [
                {"type": "finish", "content": "没有找到这个区划。"},
            ],
            "expected": {
                "terminal_status": "completed",
                "outcome": "success",
                "completion_reason_code": "goal_completed",
                "required_answer_substrings": ["不代表任何业务指标为零"],
            },
        }
    )

    trace = await EvalRunner().run(case)

    assert trace.passed is False
    failed = [grade for grade in trace.grades if not grade.passed]
    assert [grade.name for grade in failed] == ["required_answer_substrings"]


@pytest.mark.asyncio
async def test_eval_runner_accepts_one_of_multiple_complete_terminal_variants() -> None:
    case = EvalCase.model_validate(
        {
            "case_id": "area-denied-safe-paths",
            "description": "越权请求允许模型主动拒绝或 Policy 拒绝",
            "user_message": "查询授权范围外区域",
            "model_steps": [
                {"type": "finish", "content": "该区域不在授权范围内。"},
            ],
            "expected": {
                "terminal_status": "completed",
                "outcome": "success",
                "completion_reason_code": "goal_completed",
                "tool_ids": [],
                "acceptable_terminal_variants": [
                    {
                        "outcome": "success",
                        "completion_reason_code": "goal_completed",
                        "tool_ids": [],
                    },
                    {
                        "outcome": "denied",
                        "completion_reason_code": "AREA_OUT_OF_SCOPE",
                        "tool_ids": ["governance.semantic_query"],
                    },
                ],
            },
        }
    )

    trace = await EvalRunner().run(case)

    assert trace.passed is True
    terminal_grade = next(
        grade for grade in trace.grades if grade.name == "terminal_variant"
    )
    assert terminal_grade.passed is True


@pytest.mark.asyncio
async def test_eval_runner_uses_deterministic_grounding_grader() -> None:
    original = population_case()
    case = original.model_copy(
        update={
            "case_id": "population-grounding-grade",
            "expected": original.expected.model_copy(
                update={"grounding_reason_code": "grounded"}
            ),
        }
    )

    trace = await EvalRunner().run(case)

    grounding = next(grade for grade in trace.grades if grade.name == "grounding")
    assert grounding.passed is True
    assert grounding.actual == "grounded"
