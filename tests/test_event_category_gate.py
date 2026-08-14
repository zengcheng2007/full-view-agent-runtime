import json

import pytest

from full_view_agent.application.capability_consistency import (
    CapabilityConsistencyError,
    validate_production_http_capabilities,
)
from full_view_agent.application.deployment_capabilities import (
    EVENT_CATEGORY_ENV,
    parse_event_category_enabled,
)
from full_view_agent.application.prompt_catalog import build_full_view_system_prompt
from full_view_agent.application.tool_registry import ToolRegistry
from full_view_agent.semantic.catalog import SemanticCatalog

from .test_semantic_catalog import FULL_AUTH


def _event_surfaces(*, enabled: bool) -> str:
    registry = ToolRegistry.production_http(event_category_enabled=enabled)
    catalog = SemanticCatalog.default(event_category_enabled=enabled)
    subject = catalog.require_subject("event")
    return json.dumps(
        {
            "schema": registry.get_input_schema("governance.query_event_metrics"),
            "descriptor": registry.get_model_descriptor(
                "governance.query_event_metrics"
            ).model_dump(mode="json"),
            "result_schemas": [
                item.data_schema_ref
                for item in registry.get_manifest(
                    "governance.query_event_metrics"
                ).result_schemas
            ],
            "groups": [rule.value for rule in subject.group_by_rules],
            "shapes": [shape.shape_id for shape in subject.result_shapes],
            "model_view": catalog.model_capability_view(FULL_AUTH).model_dump(mode="json"),
            "prompt": build_full_view_system_prompt(
                {},
                tool_ids=("governance.query_event_metrics",),
                event_category_enabled=enabled,
            ),
        },
        ensure_ascii=False,
    )


def test_event_category_gate_defaults_closed_and_requires_explicit_true() -> None:
    assert EVENT_CATEGORY_ENV == "FULL_VIEW_EVENT_CATEGORY_ENABLED"
    assert parse_event_category_enabled(None) is False
    assert parse_event_category_enabled("") is False
    assert parse_event_category_enabled("false") is False
    assert parse_event_category_enabled("true") is True
    with pytest.raises(RuntimeError, match=EVENT_CATEGORY_ENV):
        parse_event_category_enabled("yes")


def test_default_closed_hides_event_category_across_every_model_surface() -> None:
    surfaces = _event_surfaces(enabled=False)
    assert "event_category" not in surfaces
    assert "event-category-table" not in surfaces
    assert "event_count" in surfaces
    assert "month" in surfaces
    assert "finish_rate" in surfaces


def test_explicit_enable_exposes_event_category_consistently() -> None:
    surfaces = _event_surfaces(enabled=True)
    assert "event_category" in surfaces
    assert "event-category-table" in surfaces
    assert "现有主题块口径" in surfaces


@pytest.mark.parametrize("enabled", [False, True])
def test_production_consistency_accepts_matching_event_category_gate(
    enabled: bool,
) -> None:
    validate_production_http_capabilities(
        registry=ToolRegistry.production_http(event_category_enabled=enabled),
        catalog=SemanticCatalog.default(event_category_enabled=enabled),
    )


def test_production_consistency_rejects_event_category_gate_drift() -> None:
    with pytest.raises(CapabilityConsistencyError, match="event category"):
        validate_production_http_capabilities(
            registry=ToolRegistry.production_http(event_category_enabled=False),
            catalog=SemanticCatalog.default(event_category_enabled=True),
        )
