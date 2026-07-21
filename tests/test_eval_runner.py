import pytest

from full_view_agent.application.model_provider import (
    ModelRequest,
    ModelResponse,
    ModelToolCall,
    ModelUsage,
)
from full_view_agent.evaluation.contracts import EvalCase
from full_view_agent.evaluation.runner import EvalRunner, StaticEvalEnvironment
from full_view_agent.infrastructure.governance_adapter import InMemoryGovernanceAdapter


class TwoStepLiveModelProvider:
    def __init__(self) -> None:
        self.call_count = 0

    async def complete(self, request: ModelRequest) -> ModelResponse:
        del request
        self.call_count += 1
        if self.call_count == 1:
            return ModelResponse(
                content=None,
                tool_calls=(
                    ModelToolCall(
                        tool_id="governance.query_population_metrics",
                        arguments={
                            "query": {
                                "metrics": ["person_count"],
                                "scope": {"area_code": "330106"},
                                "filters": [],
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
        return ModelResponse(
            content="人口指标查询已完成",
            tool_calls=(),
            finish_reason="stop",
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
            "model_steps": [
                {
                    "type": "tool_call",
                    "tool_id": "governance.query_population_metrics",
                    "arguments": {
                        "query": {
                            "metrics": ["person_count"],
                            "scope": {"area_code": area_code},
                            "filters": [],
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
                "tool_ids": ["governance.query_population_metrics"],
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
    assert trace.tool_ids == ["governance.query_population_metrics"]
    assert len(trace.evidence_ids) == 1
    assert trace.total_tokens == 150
    assert all(grade.passed for grade in trace.grades)
    assert "查询西湖区独居老人数量" in trace.model_requests[0].messages[-1].content
    serialized = trace.model_dump_json()
    assert "credential_ref" not in serialized
    assert "cred-eval" not in serialized


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
    assert trace.prompt_version == "full-view-governance-readonly-v6"
    assert [step.type for step in trace.model_steps] == ["tool_call", "finish"]
    assert trace.total_tokens == 210
    assert provider.call_count == 2


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
        self._delegate = InMemoryGovernanceAdapter()

    async def execute(self, **kwargs):
        self.calls += 1
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
            "fault": {
                "type": "upstream_timeout",
                "tool_id": "governance.query_population_metrics",
            },
            "model_steps": [
                {
                    "type": "tool_call",
                    "tool_id": "governance.query_population_metrics",
                    "arguments": {
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
                "tool_ids": ["governance.query_population_metrics"],
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
                        "tool_ids": ["governance.query_population_metrics"],
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
