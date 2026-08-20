"""生产 HTTP 能力跨层一致性 Gate。

这是部署时的 fail-closed 校验，不替代各层自身的参数和权限校验。它把
Registry、Catalog、模型描述、输入模型、HTTP Adapter 与部署开关之间最容易
漂移的事实集中核对；任何不一致都会阻止 HTTP 运行时启动。
"""

import json
from dataclasses import dataclass

from pydantic import BaseModel

from full_view_agent.application.capability_service import TOOL_INPUT_MODELS
from full_view_agent.application.prompt_catalog import (
    _CANONICAL_TOOL_ORDER,
    _CAPABILITY_LINES,
    build_full_view_system_prompt,
)
from full_view_agent.application.tool_registry import (
    PRODUCTION_HTTP_TOOL_IDS,
    ToolRegistry,
)
from full_view_agent.domain.models import (
    GetObjectProfileInput,
    QueryEnterpriseMetricsInput,
    QueryEventMetricsInput,
    QueryGovernanceOverviewInput,
    QueryGovernancePowerMetricsInput,
    QueryHousingMetricsInput,
    QueryPopulationMetricsInput,
    ResolveAreaInput,
)
from full_view_agent.infrastructure.governance_adapter import (
    PRODUCTION_HTTP_ADAPTER_TOOL_IDS,
)
from full_view_agent.semantic.catalog import SemanticCatalog


@dataclass(frozen=True)
class _CapabilityContract:
    input_model: type[BaseModel]
    dataset_id: str
    permission: str
    input_schema_ref: str
    adapter_ref: str
    subject_id: str | None = None


_PRODUCTION_CONTRACTS: dict[str, _CapabilityContract] = {
    "governance.query_governance_power_metrics": _CapabilityContract(
        input_model=QueryGovernancePowerMetricsInput,
        dataset_id="governance_power",
        permission="governance.power.aggregate.read",
        input_schema_ref="schema://tools/query-governance-power-metrics-input/1.0.0",
        adapter_ref="adapter://geo-qxst/governance-power-metrics/1.0",
        subject_id="governance_power",
    ),
    "governance.query_enterprise_metrics": _CapabilityContract(
        input_model=QueryEnterpriseMetricsInput,
        dataset_id="enterprise",
        permission="governance.enterprise.aggregate.read",
        input_schema_ref="schema://tools/query-enterprise-metrics-input/1.0.0",
        adapter_ref="adapter://geo-qxst/enterprise-metrics/1.0",
        subject_id="enterprise",
    ),
    "governance.get_governance_overview": _CapabilityContract(
        input_model=QueryGovernanceOverviewInput,
        dataset_id="governance_overview",
        permission="governance.overview.aggregate.read",
        input_schema_ref="schema://tools/query-governance-overview-input/1.0.0",
        adapter_ref="adapter://geo-qxst/governance-overview/1.0",
        subject_id="governance_overview",
    ),
    "governance.resolve_area": _CapabilityContract(
        input_model=ResolveAreaInput,
        dataset_id="administrative_area",
        permission="governance.area.read",
        input_schema_ref="schema://tools/resolve-area-input/1.0.0",
        adapter_ref="adapter://geo-qxst/resolve-area/1.0",
    ),
    "governance.query_population_metrics": _CapabilityContract(
        input_model=QueryPopulationMetricsInput,
        dataset_id="population",
        permission="governance.population.aggregate.read",
        input_schema_ref="schema://tools/query-population-metrics-input/1.0.0",
        adapter_ref="adapter://geo-qxst/population-metrics/1.0",
        subject_id="population",
    ),
    "governance.query_housing_metrics": _CapabilityContract(
        input_model=QueryHousingMetricsInput,
        dataset_id="housing",
        permission="governance.housing.aggregate.read",
        input_schema_ref="schema://tools/query-housing-metrics-input/1.0.0",
        adapter_ref="adapter://geo-qxst/housing-metrics/1.0",
        subject_id="housing",
    ),
    "governance.query_event_metrics": _CapabilityContract(
        input_model=QueryEventMetricsInput,
        dataset_id="event",
        permission="governance.event.aggregate.read",
        input_schema_ref="schema://tools/query-event-metrics-input/1.0.0",
        adapter_ref="adapter://geo-qxst/event-metrics/1.0",
        subject_id="event",
    ),
    "governance.get_object_profile": _CapabilityContract(
        input_model=GetObjectProfileInput,
        dataset_id="governance_objects",
        permission="governance.object.profile.read",
        input_schema_ref="schema://tools/get-object-profile-input/1.0.0",
        adapter_ref="adapter://geo-qxst/object-profile/1.0",
    ),
}


class CapabilityConsistencyError(RuntimeError):
    """Raised when deployment capability facts disagree across layers."""


def validate_production_http_capabilities(
    *, registry: ToolRegistry, catalog: SemanticCatalog
) -> None:
    """Fail closed when a production HTTP capability is inconsistent."""

    errors: list[str] = []
    expected_ids = set(_PRODUCTION_CONTRACTS)
    registry_ids = set(registry.list_tool_ids())
    declared_ids = set(PRODUCTION_HTTP_TOOL_IDS)
    adapter_ids = set(PRODUCTION_HTTP_ADAPTER_TOOL_IDS)
    _expect_equal(errors, "production registry tools", registry_ids, expected_ids)
    _expect_equal(errors, "declared HTTP tools", declared_ids, expected_ids)
    _expect_equal(errors, "HTTP adapter tools", adapter_ids, expected_ids)

    for tool_id, contract in _PRODUCTION_CONTRACTS.items():
        if tool_id not in registry_ids:
            continue
        manifest = registry.get_manifest(tool_id)
        descriptor = registry.get_model_descriptor(tool_id)
        if manifest.dataset_id != contract.dataset_id:
            errors.append(f"{tool_id}: dataset_id disagrees with deployment contract")
        if tuple(manifest.required_permissions) != (contract.permission,):
            errors.append(f"{tool_id}: required_permissions disagree with deployment contract")
        if manifest.policy.action != contract.permission:
            errors.append(f"{tool_id}: policy action disagrees with required permission")
        if manifest.input_schema_ref != contract.input_schema_ref:
            errors.append(f"{tool_id}: manifest input schema ref is inconsistent")
        if descriptor.input_schema.ref != contract.input_schema_ref:
            errors.append(f"{tool_id}: model descriptor input schema ref is inconsistent")
        if manifest.adapter_ref != contract.adapter_ref:
            errors.append(f"{tool_id}: adapter_ref disagrees with deployment contract")
        if TOOL_INPUT_MODELS.get(tool_id) is not contract.input_model:
            errors.append(f"{tool_id}: capability input model is inconsistent")

    _validate_semantic_bindings(errors, registry=registry, catalog=catalog)
    _validate_population_surface(errors, registry=registry, catalog=catalog)
    _validate_housing_switch(errors, registry=registry, catalog=catalog)
    _validate_event_category_switch(errors, registry=registry, catalog=catalog)
    _validate_prompt_catalog_coverage(errors)
    if errors:
        details = "; ".join(sorted(set(errors)))
        raise CapabilityConsistencyError(f"production capability drift: {details}")


def _validate_semantic_bindings(
    errors: list[str], *, registry: ToolRegistry, catalog: SemanticCatalog
) -> None:
    expected_subjects = {
        contract.subject_id: tool_id
        for tool_id, contract in _PRODUCTION_CONTRACTS.items()
        if contract.subject_id is not None
    }
    actual_subjects = {
        subject_id: binding.capability_id
        for subject_id, binding in catalog.bindings.items()
        if subject_id in catalog.bindable_subject_ids()
    }
    _expect_equal(errors, "semantic canonical bindings", actual_subjects, expected_subjects)
    for subject_id, tool_id in actual_subjects.items():
        if (
            tool_id not in registry.list_tool_ids()
            or tool_id not in PRODUCTION_HTTP_ADAPTER_TOOL_IDS
        ):
            errors.append(
                f"{subject_id}: semantic_query binds a non-executable canonical tool"
            )
            continue
        subject = catalog.require_subject(subject_id)
        binding = catalog.binding(subject_id)
        manifest = registry.get_manifest(tool_id)
        assert binding is not None
        if subject.logical_dataset_id != manifest.dataset_id:
            errors.append(f"{subject_id}: Catalog dataset disagrees with Registry")
        if subject.required_entitlement not in manifest.required_permissions:
            errors.append(f"{subject_id}: Catalog entitlement disagrees with Registry")
        if binding.adapter_ref != manifest.adapter_ref:
            errors.append(f"{subject_id}: Catalog adapter binding disagrees with Registry")


def _validate_population_surface(
    errors: list[str], *, registry: ToolRegistry, catalog: SemanticCatalog
) -> None:
    descriptor = registry.get_model_descriptor("governance.query_population_metrics")
    surface = f"{descriptor.name} {descriptor.description}"
    for required in ("一般人口", "独居老人", "solitary_elderly"):
        if required not in surface:
            errors.append(f"population model descriptor must state {required}")
    subject = catalog.require_subject("population")
    if subject.required_filters:
        errors.append("population Catalog incorrectly requires a specialized filter")
    if subject.required_user_terms:
        errors.append("population Catalog incorrectly requires specialized intent terms")
    if "人口" not in subject.trigger_user_terms:
        errors.append("population Catalog cannot detect broad population requests")


def _validate_housing_switch(
    errors: list[str], *, registry: ToolRegistry, catalog: SemanticCatalog
) -> None:
    enabled = registry.housing_next_area_enabled
    subject = catalog.require_subject("housing")
    descriptor = registry.get_model_descriptor("governance.query_housing_metrics")
    schema_text = json.dumps(
        registry.get_input_schema("governance.query_housing_metrics"),
        ensure_ascii=False,
        sort_keys=True,
    )
    descriptor_text = json.dumps(descriptor.model_dump(mode="json"), ensure_ascii=False)
    prompt = build_full_view_system_prompt(
        {},
        tool_ids=("governance.query_housing_metrics",),
        housing_next_area_enabled=enabled,
    )
    surfaces = (schema_text, descriptor_text, prompt)
    subject_exposes_next_area = any(
        rule.value == "next_area" for rule in subject.group_by_rules
    ) or any(shape.group_by_selection == ("next_area",) for shape in subject.result_shapes)
    if enabled:
        if not subject_exposes_next_area or any("next_area" not in item for item in surfaces):
            errors.append("housing next_area is enabled but not reachable across all surfaces")
    elif subject_exposes_next_area or any("next_area" in item for item in surfaces):
        errors.append("housing next_area is disabled but remains reachable")


def _validate_event_category_switch(
    errors: list[str], *, registry: ToolRegistry, catalog: SemanticCatalog
) -> None:
    enabled = registry.event_category_enabled
    subject = catalog.require_subject("event")
    descriptor = registry.get_model_descriptor("governance.query_event_metrics")
    schema_text = json.dumps(
        registry.get_input_schema("governance.query_event_metrics"),
        ensure_ascii=False,
        sort_keys=True,
    )
    descriptor_text = json.dumps(
        descriptor.model_dump(mode="json"), ensure_ascii=False
    )
    prompt = build_full_view_system_prompt(
        {},
        tool_ids=("governance.query_event_metrics",),
        event_category_enabled=enabled,
    )
    result_schema_text = json.dumps(
        [
            item.data_schema_ref
            for item in registry.get_manifest(
                "governance.query_event_metrics"
            ).result_schemas
        ],
        ensure_ascii=False,
    )
    catalog_exposes = any(
        rule.value == "event_category" for rule in subject.group_by_rules
    ) or any(
        shape.group_by_selection == ("event_category",)
        for shape in subject.result_shapes
    )
    if enabled:
        if (
            not catalog_exposes
            or "event_category" not in schema_text
            or "一级分类" not in descriptor_text
            or "event_category" not in prompt
            or "event-category" not in result_schema_text
        ):
            errors.append("event category is enabled but not reachable across all surfaces")
    elif (
        catalog_exposes
        or "event_category" in schema_text
        or "一级分类" in descriptor_text
        or "event_category" in prompt
        or "event-category" in result_schema_text
    ):
        errors.append("event category is disabled but remains reachable")


def _validate_prompt_catalog_coverage(errors: list[str]) -> None:
    """Fail closed: every model-callable production tool must have capability guidance.

    A registered tool without guidance leaves the model to guess which tool
    to call, which can silently route queries to the wrong domain (e.g. an
    enterprise query answered with population data).  See WSZC-10 root cause.

    Only tools in _CANONICAL_TOOL_ORDER are model-callable; other production
    HTTP tools may be internal APIs not exposed to the model.
    """
    # Tools whose guidance is generated dynamically in build_full_view_system_prompt.
    dynamic_guidance_tools = {
        "governance.query_housing_metrics",
        "governance.query_event_metrics",
    }
    for tool_id in _CANONICAL_TOOL_ORDER:
        # Skip knowledge.search — it's model-callable but has its own guidance path
        if tool_id == "knowledge.search":
            continue
        # Only check tools that are in the production set
        if tool_id not in PRODUCTION_HTTP_TOOL_IDS and tool_id != "governance.get_object_profile":
            continue
        has_static = tool_id in _CAPABILITY_LINES
        has_dynamic = tool_id in dynamic_guidance_tools
        if not (has_static or has_dynamic):
            errors.append(
                f"{tool_id}: in _CANONICAL_TOOL_ORDER (model-callable) but has no "
                "capability guidance in prompt_catalog._CAPABILITY_LINES — "
                "the model cannot know when or how to call it"
            )


def _expect_equal(errors: list[str], label: str, actual: object, expected: object) -> None:
    if actual != expected:
        errors.append(f"{label} mismatch: actual={actual!r}, expected={expected!r}")
