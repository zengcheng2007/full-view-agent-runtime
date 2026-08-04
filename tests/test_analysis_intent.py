"""P2 意图契约测试：模型只能提交受控 AnalysisIntentV1。

契约边界必须保证：
- kind 固定 regional_analysis，schema_version 固定 1.0；
- goals 是受限词表内的非空、去重、有界集合；
- scope_strategy 只允许 named_area / current_area；
- named_area 必须携带 area_query；current_area 不允许携带任何区划编码；
- extra=forbid：steps/sql/url/adapter/budget/area_code 等敏感字段一律拒绝。
"""

import pytest
from pydantic import ValidationError

from full_view_agent.domain.analysis_intent import (
    ANALYSIS_INTENT_SCHEMA_VERSION,
    AnalysisIntentV1,
)

VALID_NAMED_PAYLOAD = {
    "kind": "regional_analysis",
    "goals": ["population"],
    "scope": {"kind": "named_area", "area_query": "西湖区"},
}

VALID_CURRENT_PAYLOAD = {
    "kind": "regional_analysis",
    "goals": ["overview"],
    "scope": {"kind": "current_area"},
}


def test_valid_named_area_intent_parses() -> None:
    intent = AnalysisIntentV1.model_validate(VALID_NAMED_PAYLOAD)
    assert intent.kind == "regional_analysis"
    assert intent.schema_version == ANALYSIS_INTENT_SCHEMA_VERSION == "1.0"
    assert intent.goals == ("population",)
    assert intent.scope.kind == "named_area"
    assert intent.scope.area_query == "西湖区"


def test_valid_current_area_intent_parses() -> None:
    intent = AnalysisIntentV1.model_validate(VALID_CURRENT_PAYLOAD)
    assert intent.scope.kind == "current_area"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("kind", "data_export"),
        ("schema_version", "2.0"),
    ],
)
def test_kind_and_schema_version_are_fixed(field: str, value: str) -> None:
    with pytest.raises(ValidationError):
        AnalysisIntentV1.model_validate({**VALID_NAMED_PAYLOAD, field: value})


def test_goals_are_deduped_into_canonical_order() -> None:
    intent = AnalysisIntentV1.model_validate(
        {**VALID_NAMED_PAYLOAD, "goals": ["housing", "population", "housing"]}
    )
    assert intent.goals == ("population", "housing")


def test_goals_must_be_non_empty() -> None:
    with pytest.raises(ValidationError):
        AnalysisIntentV1.model_validate({**VALID_NAMED_PAYLOAD, "goals": []})


def test_goals_are_bounded() -> None:
    with pytest.raises(ValidationError):
        AnalysisIntentV1.model_validate(
            {
                **VALID_NAMED_PAYLOAD,
                "goals": ["overview", "population", "housing", "event", "overview"],
            }
        )


def test_unknown_goal_is_rejected() -> None:
    with pytest.raises(ValidationError):
        AnalysisIntentV1.model_validate({**VALID_NAMED_PAYLOAD, "goals": ["economy"]})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("steps", [{"step_id": "s1", "sql": "SELECT 1"}]),
        ("sql", "SELECT * FROM population"),
        ("url", "https://internal.example/query"),
        ("adapter", "adapter://geo-qxst/population-metrics/1.0"),
        ("budget", {"max_tool_calls": 64}),
        ("area_code", "330106"),
        ("request_id", "req-injected"),
    ],
)
def test_sensitive_fields_are_rejected_at_intent_boundary(
    field: str, value: object
) -> None:
    with pytest.raises(ValidationError):
        AnalysisIntentV1.model_validate({**VALID_NAMED_PAYLOAD, field: value})


@pytest.mark.parametrize(
    "scope",
    [
        {"kind": "named_area", "area_query": "西湖区", "area_code": "330106"},
        {"kind": "current_area", "area_code": "330106"},
        {"kind": "current_area", "area_query": "西湖区"},
        {"kind": "saved_polygon", "polygon_ref": "polygon-01"},
        {"kind": "area", "scope": {"area_code": "330106"}},
    ],
)
def test_scope_injection_is_rejected(scope: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        AnalysisIntentV1.model_validate({**VALID_NAMED_PAYLOAD, "scope": scope})


def test_named_area_requires_area_query() -> None:
    with pytest.raises(ValidationError):
        AnalysisIntentV1.model_validate(
            {**VALID_NAMED_PAYLOAD, "scope": {"kind": "named_area"}}
        )
    with pytest.raises(ValidationError):
        AnalysisIntentV1.model_validate(
            {**VALID_NAMED_PAYLOAD, "scope": {"kind": "named_area", "area_query": ""}}
        )


def test_scope_is_required_and_typed() -> None:
    without_scope = {k: v for k, v in VALID_NAMED_PAYLOAD.items() if k != "scope"}
    with pytest.raises(ValidationError):
        AnalysisIntentV1.model_validate(without_scope)
    with pytest.raises(ValidationError):
        AnalysisIntentV1.model_validate({**VALID_NAMED_PAYLOAD, "scope": "330106"})


def test_intent_is_immutable() -> None:
    intent = AnalysisIntentV1.model_validate(VALID_NAMED_PAYLOAD)
    with pytest.raises(ValidationError):
        intent.goals = ("overview",)  # type: ignore[misc]
