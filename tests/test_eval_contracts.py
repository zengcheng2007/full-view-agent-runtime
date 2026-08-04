from pathlib import Path

import pytest
from pydantic import ValidationError

from full_view_agent.evaluation.contracts import (
    EvalErrorStep,
    EvalFinishStep,
    EvalToolCallStep,
)
from full_view_agent.evaluation.loader import load_eval_case


def test_load_eval_case_from_versioned_yaml(tmp_path: Path) -> None:
    case_path = tmp_path / "population-success.yaml"
    case_path.write_text(
        """
schema_version: "1.0"
case_id: population-success
description: 授权范围内人口聚合查询
user_message: 查询西湖区独居老人数量
auth:
  area_codes: ["330106"]
  datasets: [population]
  entitlements: [governance.population.aggregate.read]
model_steps:
  - type: tool_call
    tool_id: governance.query_population_metrics
    arguments:
      query:
        metrics: [person_count]
        scope: {area_code: "330106"}
        filters: []
        group_by: [street]
  - type: finish
    content: 查询完成
expected:
  terminal_status: completed
  outcome: success
  completion_reason_code: goal_completed
  tool_ids: [governance.query_population_metrics]
  required_tool_ids: [governance.query_population_metrics]
  forbidden_tool_ids: [governance.semantic_query]
  max_tool_calls: 2
  min_evidence_count: 1
""".strip(),
        encoding="utf-8",
    )

    case = load_eval_case(case_path)

    assert case.case_id == "population-success"
    assert isinstance(case.model_steps[0], EvalToolCallStep)
    assert isinstance(case.model_steps[1], EvalFinishStep)
    assert case.expected.min_evidence_count == 1
    assert case.expected.required_tool_ids == [
        "governance.query_population_metrics"
    ]
    assert case.expected.forbidden_tool_ids == ["governance.semantic_query"]
    assert case.expected.max_tool_calls == 2


def test_eval_finish_step_preserves_structured_claims() -> None:
    step = EvalFinishStep.model_validate(
        {
            "type": "finish",
            "content": "住宅出租为884套。",
            "structured_finish": {
                "kind": "claims",
                "summary": "住宅出租为884套。",
                "claims": [
                    {
                        "claim_id": "claim-1",
                        "result_id": "res-housing",
                        "result_fingerprint": "sha256:housing",
                        "collection": "rows",
                        "row_locator": {"lease_type": "住宅出租"},
                        "field": "dwelling_count",
                        "operation": "value",
                        "value": 884,
                    }
                ],
            },
        }
    )

    restored = EvalFinishStep.model_validate(step.model_dump(mode="json"))

    assert restored.structured_finish is not None
    assert restored.structured_finish.claims[0].result_id == "res-housing"


def test_load_eval_case_supports_normalized_model_error_steps(tmp_path: Path) -> None:
    case_path = tmp_path / "model-timeout.yaml"
    case_path.write_text(
        """
schema_version: "1.0"
case_id: model-timeout
description: 模型超时必须可靠终止
user_message: 查询人口
model_steps:
  - type: error
    error_code: model_timeout
    message: provider timed out
expected:
  terminal_status: failed
  outcome: failed
  completion_reason_code: model_timeout
""".strip(),
        encoding="utf-8",
    )

    case = load_eval_case(case_path)

    assert isinstance(case.model_steps[0], EvalErrorStep)
    assert case.model_steps[0].error_code == "model_timeout"


def test_eval_case_rejects_unknown_fields(tmp_path: Path) -> None:
    case_path = tmp_path / "invalid.yaml"
    case_path.write_text(
        """
schema_version: "1.0"
case_id: invalid-case
description: 非法字段
user_message: 查询人口
unexpected: true
model_steps:
  - type: finish
    content: 完成
expected:
  terminal_status: completed
  outcome: success
  completion_reason_code: goal_completed
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ValidationError, match="unexpected"):
        load_eval_case(case_path)
