import pytest
from pydantic import ValidationError

from full_view_agent.domain import models


def test_resolve_area_input_rejects_unknown_storage_or_api_parameters() -> None:
    resolve_area_input = models.ResolveAreaInput(
        query="西湖区",
        parent_area_code="330100",
    )

    assert resolve_area_input.query == "西湖区"
    with pytest.raises(ValidationError):
        models.ResolveAreaInput.model_validate(
            {
                "query": "西湖区",
                "table_name": "area_info",
                "legacy_api_path": "/area/getAreaInfoByAreaName",
            }
        )


def test_population_metrics_input_only_accepts_registered_semantics() -> None:
    population_input = models.QueryPopulationMetricsInput.model_validate(
        {
            "query": {
                "metrics": ["person_count"],
                "scope": {
                    "area_code": "330106",
                    "include_descendants": True,
                },
                "filters": [
                    {"field": "person_category", "operator": "eq", "value": "solitary_elderly"},
                    {"field": "age", "operator": "gte", "value": 80},
                ],
                "group_by": ["street"],
                "limit": 200,
            }
        }
    )

    assert population_input.query.scope.area_code == "330106"
    with pytest.raises(ValidationError):
        models.QueryPopulationMetricsInput.model_validate(
            {
                "dataset": "arbitrary_index",
                "query": {
                    "metrics": ["person_count"],
                    "scope": {"area_code": "330106"},
                },
            }
        )


def test_population_filter_rejects_operator_not_registered_for_field() -> None:
    with pytest.raises(ValidationError):
        models.PopulationMetricFilter(
            field="person_category",
            operator="gte",
            value="solitary_elderly",
        )


def test_object_profile_input_uses_registered_field_sets_not_raw_fields() -> None:
    profile_input = models.GetObjectProfileInput.model_validate(
        {
            "object_ref": {
                "object_type": "person",
                "object_id": "person-01",
            },
            "field_sets": ["summary", "demographics"],
        }
    )

    assert profile_input.field_sets == ["summary", "demographics"]
    with pytest.raises(ValidationError):
        models.GetObjectProfileInput.model_validate(
            {
                "object_ref": {
                    "object_type": "person",
                    "object_id": "person-01",
                },
                "fields": ["phone", "id_card"],
            }
        )


def test_tool_result_accepts_area_candidates_and_typed_object_profile() -> None:
    area_result = models.ToolResult.model_validate(
        {
            "tool_call_id": "tcl-area-01",
            "tool_id": "governance.resolve_area",
            "tool_version": "1.0.0",
            "status": "success",
            "summary": "唯一匹配西湖区。",
            "data_result": {
                "result_id": "res-area-01",
                "kind": "area_candidates",
                "data_schema_ref": "schema://data/area-candidates/1.0.0",
                "result_fingerprint": "sha256:area",
                "data": {
                    "resolved_area_code": "330106",
                    "ambiguous": False,
                    "candidates": [
                        {
                            "area_code": "330106",
                            "area_name": "西湖区",
                            "level": "district",
                            "parent_area_code": "330100",
                            "bounds": [120.02, 30.05, 120.20, 30.35],
                        }
                    ],
                },
                "candidate_count": 1,
            },
        }
    )
    profile_result = models.ToolResult.model_validate(
        {
            "tool_call_id": "tcl-profile-01",
            "tool_id": "governance.get_object_profile",
            "tool_version": "1.0.0",
            "status": "success",
            "summary": "返回人员可见画像。",
            "data_result": {
                "result_id": "res-profile-01",
                "kind": "object_profile",
                "data_schema_ref": "schema://data/object-profile/1.0.0",
                "result_fingerprint": "sha256:profile",
                "data": {
                    "object_ref": {
                        "object_type": "person",
                        "object_id": "person-01",
                    },
                    "area_code": "330106001",
                    "title": "张某",
                    "fields": [
                        {
                            "field_id": "age",
                            "label": "年龄",
                            "value": 82,
                            "classification": "internal",
                        }
                    ],
                },
            },
        }
    )

    assert area_result.data_result.kind == "area_candidates"
    assert profile_result.data_result.kind == "object_profile"
