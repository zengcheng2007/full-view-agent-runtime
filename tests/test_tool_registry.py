import pytest

from full_view_agent.application.errors import ResourceNotFound


def test_registry_separates_internal_manifest_from_model_descriptor() -> None:
    from full_view_agent.application.tool_registry import ToolRegistry

    registry = ToolRegistry.default()
    manifest = registry.get_manifest("governance.query_population_metrics")
    descriptor = registry.get_model_descriptor(
        "governance.query_population_metrics"
    )

    assert manifest.adapter_ref == "adapter://geo-qxst/population-metrics/1.0"
    assert manifest.required_permissions == [
        "governance.population.aggregate.read"
    ]
    assert descriptor.name == "查询独居老人指标"
    assert "adapter_ref" not in descriptor.model_dump(mode="json")
    assert "required_permissions" not in descriptor.model_dump(mode="json")


def test_registry_contains_only_the_three_implemented_tools() -> None:
    from full_view_agent.application.tool_registry import ToolRegistry

    registry = ToolRegistry.default()

    assert registry.list_tool_ids() == [
        "governance.get_object_profile",
        "governance.query_event_metrics",
        "governance.query_housing_metrics",
        "governance.query_population_metrics",
        "governance.resolve_area",
    ]
    with pytest.raises(ResourceNotFound):
        registry.get_manifest("governance.query_any_table")


def test_metric_manifests_bind_to_their_own_result_schemas() -> None:
    from full_view_agent.application.tool_registry import ToolRegistry

    registry = ToolRegistry.default()
    expected = {
        "governance.query_population_metrics": (
            "schema://data/population-metric-table/1.0.0"
        ),
        "governance.query_housing_metrics": (
            "schema://data/housing-lease-type-table/1.0.0"
        ),
        "governance.query_event_metrics": (
            "schema://data/event-finish-rate-table/1.0.0"
        ),
    }

    for tool_id, schema_ref in expected.items():
        manifest = registry.get_manifest(tool_id)
        assert manifest.result_schemas[0].data_schema_ref == schema_ref
