import pytest

from full_view_agent.application.capability_consistency import (
    CapabilityConsistencyError,
    validate_production_http_capabilities,
)
from full_view_agent.application.capability_service import TOOL_INPUT_MODELS
from full_view_agent.application.tool_registry import ToolRegistry
from full_view_agent.domain.models import QueryEventMetricsInput
from full_view_agent.semantic.catalog import CapabilityBinding, SemanticCatalog


@pytest.mark.parametrize("housing_next_area_enabled", [False, True])
def test_production_http_capabilities_are_consistent(
    housing_next_area_enabled: bool,
) -> None:
    validate_production_http_capabilities(
        registry=ToolRegistry.production_http(
            housing_next_area_enabled=housing_next_area_enabled
        ),
        catalog=SemanticCatalog.default(
            housing_next_area_enabled=housing_next_area_enabled
        ),
    )


def test_default_registry_cannot_expose_unverified_object_profile_over_http() -> None:
    with pytest.raises(
        CapabilityConsistencyError,
        match="governance.get_object_profile",
    ):
        validate_production_http_capabilities(
            registry=ToolRegistry.default(),
            catalog=SemanticCatalog.default(housing_next_area_enabled=True),
        )


def test_population_descriptor_must_distinguish_general_population() -> None:
    baseline = ToolRegistry.production_http()
    descriptors = [
        baseline.get_model_descriptor(tool_id)
        for tool_id in baseline.list_tool_ids()
    ]
    descriptors = [
        descriptor.model_copy(
            update={
                "name": "查询人口指标",
                "description": "查询人口聚合指标，也可以查询独居老人 solitary_elderly。",
            }
        )
        if descriptor.tool_id == "governance.query_population_metrics"
        else descriptor
        for descriptor in descriptors
    ]
    drifted = ToolRegistry(
        manifests=[
            baseline.get_manifest(tool_id) for tool_id in baseline.list_tool_ids()
        ],
        descriptors=descriptors,
        housing_next_area_enabled=False,
    )

    with pytest.raises(CapabilityConsistencyError, match="must state 一般人口"):
        validate_production_http_capabilities(
            registry=drifted,
            catalog=SemanticCatalog.default(),
        )


def test_registry_dataset_and_catalog_entitlement_must_agree() -> None:
    baseline = ToolRegistry.production_http()
    manifests = [
        baseline.get_manifest(tool_id) for tool_id in baseline.list_tool_ids()
    ]
    manifests = [
        manifest.model_copy(update={"dataset_id": "generic_population"})
        if manifest.tool_id == "governance.query_population_metrics"
        else manifest
        for manifest in manifests
    ]
    drifted = ToolRegistry(
        manifests=manifests,
        descriptors=[
            baseline.get_model_descriptor(tool_id)
            for tool_id in baseline.list_tool_ids()
        ],
        housing_next_area_enabled=False,
    )

    with pytest.raises(CapabilityConsistencyError, match="dataset"):
        validate_production_http_capabilities(
            registry=drifted,
            catalog=SemanticCatalog.default(),
        )


def test_semantic_query_cannot_bind_non_executable_canonical_tool() -> None:
    baseline = SemanticCatalog.default()
    bindings = baseline.bindings
    bindings["population"] = CapabilityBinding(
        capability_id="governance.get_object_profile",
        capability_version="1.0.0",
        adapter_ref="adapter://geo-qxst/object-profile/1.0",
    )
    drifted = SemanticCatalog(
        catalog_version=baseline.catalog_version,
        supported_spec_versions=baseline.supported_spec_versions,
        subjects=baseline.subjects,
        bindings=bindings,
    )

    with pytest.raises(CapabilityConsistencyError, match="semantic"):
        validate_production_http_capabilities(
            registry=ToolRegistry.production_http(),
            catalog=drifted,
        )


def test_disabled_housing_next_area_cannot_leak_from_catalog() -> None:
    with pytest.raises(CapabilityConsistencyError, match="housing next_area"):
        validate_production_http_capabilities(
            registry=ToolRegistry.production_http(housing_next_area_enabled=False),
            catalog=SemanticCatalog.default(housing_next_area_enabled=True),
        )


def test_capability_input_model_binding_is_checked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(
        TOOL_INPUT_MODELS,
        "governance.query_population_metrics",
        QueryEventMetricsInput,
    )
    with pytest.raises(CapabilityConsistencyError, match="input model"):
        validate_production_http_capabilities(
            registry=ToolRegistry.production_http(),
            catalog=SemanticCatalog.default(),
        )
