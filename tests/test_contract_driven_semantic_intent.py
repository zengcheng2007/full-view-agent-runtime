from __future__ import annotations

import pytest

from full_view_agent.application.builtin_capability_seeds import (
    population_semantic_contract_v1_3,
)
from full_view_agent.application.capability_management_service import (
    CapabilityManagementService,
)
from full_view_agent.application.capability_service import CapabilityService
from full_view_agent.application.dynamic_tool_bridge import (
    build_dynamic_input_schemas,
    build_dynamic_tool_registry_entries,
)
from full_view_agent.application.errors import RunStateConflict
from full_view_agent.application.policy import MinimalPolicyAdapter
from full_view_agent.application.tool_registry import ToolRegistry
from full_view_agent.domain.capability import (
    Connector,
    ToolCapability,
    ToolSemanticContract,
)
from full_view_agent.domain.models import (
    AuthorizedAreaScope,
    DynamicTableData,
    TableDataResult,
)
from full_view_agent.infrastructure.capability_repository import (
    InMemoryCapabilityRepository,
)
from full_view_agent.semantic.action_resolver import (
    ResolvedSemanticAction,
    SemanticActionResolver,
)
from full_view_agent.semantic.catalog import SemanticCatalog
from full_view_agent.semantic.contract_intent_resolver import (
    ContractSemanticIntentResolver,
)
from tests.test_policy import population_auth_context


def _contract(*, subject: str, subject_terms: list[str]) -> ToolSemanticContract:
    return ToolSemanticContract.model_validate(
        {
            "subject": subject,
            "intent_terms": subject_terms,
            "excluded_intent_terms": ["明细"],
            "metrics": [
                {
                    "metric_id": "item_count",
                    "label": "数量",
                    "unit": "个",
                    "value_type": "integer",
                    "intent_terms": ["数量", "多少"],
                }
            ],
            "dimensions": [
                {
                    "dimension_id": "descendant_unit",
                    "label": "下辖单元",
                    "kind": "administrative_area",
                    "intent_terms": ["单元", "下辖单元"],
                }
            ],
            "filters": [],
            "operators": ["avg", "top", "bottom"],
            "operator_intents": [
                {"operator": "avg", "terms": ["平均", "均值"]},
                {"operator": "top", "terms": ["最多", "最高"]},
                {"operator": "bottom", "terms": ["最少", "最低"]},
            ],
            "sort": {
                "allowed_fields": ["item_count", "descendant_unit"],
                "default_direction": "desc",
                "tie_policy": "include_all",
                "tie_breakers": [],
            },
            "completeness": {
                "mode": "complete",
                "statement": "使用完整上游行集。",
            },
            "output_forms": ["table"],
            "examples": [],
            "limitations": [],
            "query_shapes": [
                {
                    "shape_id": f"{subject}_descendant_unit_{operator}_table",
                    "metric_selection": ["item_count"],
                    "dimension_selection": ["descendant_unit"],
                    "operator_selection": [operator],
                    "scope_levels": [4],
                    "allowed_filters": [],
                    "output_forms": ["table"],
                    "completeness": {
                        "mode": "complete",
                        "statement": "使用完整上游行集。",
                    },
                    "result_schema_ref": "schema://data/generic-table/1.0.0",
                    "result_row_fields": ["unit_id", "item_count"],
                    "result_fingerprint_domain": "data-result:generic-table:1.0.0",
                    "argument_template": {
                        "query": {
                            "metrics": "$semantic.metrics",
                            "operator": "$semantic.operator",
                            "scope": "$semantic.scope",
                            "group_by": "$semantic.group_by",
                            "limit": "$semantic.limit",
                        }
                    },
                }
                for operator in ("avg", "top", "bottom")
            ],
        }
    )


def test_resolver_uses_contract_terms_instead_of_subject_specific_code() -> None:
    resolver = ContractSemanticIntentResolver()
    contract = _contract(subject="device", subject_terms=["设备", "感知设备"])

    resolved = resolver.resolve(
        message="杭州市下辖单元的设备平均数量是多少",
        scope_area_code="3301",
        contracts=(contract,),
    )

    assert resolved is not None
    assert resolved.shape_id == "device_descendant_unit_avg_table"
    assert resolved.spec.model_dump(mode="json") == {
        "schema_version": "s0.1",
        "subject": "device",
        "operator": "avg",
        "metrics": ["item_count"],
        "scope": {"area_code": "3301", "include_descendants": True},
        "group_by": ["descendant_unit"],
        "filters": [],
        "order_by": [],
        "limit": 200,
        "time_range": None,
        "output": "table",
    }


def test_resolver_selects_bottom_from_the_published_contract() -> None:
    resolver = ContractSemanticIntentResolver()
    contract = _contract(subject="population", subject_terms=["人口", "人数"])

    resolved = resolver.resolve(
        message="杭州市哪个下辖单元人口数量最少",
        scope_area_code="3301",
        contracts=(contract,),
    )

    assert resolved is not None
    assert resolved.shape_id == "population_descendant_unit_bottom_table"
    assert resolved.spec.operator == "bottom"
    assert resolved.spec.limit == 1


def test_resolver_fails_closed_for_excluded_or_ambiguous_intent() -> None:
    resolver = ContractSemanticIntentResolver()
    population = _contract(subject="population", subject_terms=["人口"])
    people = _contract(subject="people", subject_terms=["人口"])

    assert (
        resolver.resolve(
            message="杭州市人口明细最多的下辖单元",
            scope_area_code="3301",
            contracts=(population,),
        )
        is None
    )
    assert (
        resolver.resolve(
            message="杭州市人口最多的下辖单元",
            scope_area_code="3301",
            contracts=(population, people),
        )
        is None
    )


def test_published_population_contract_rejects_median_ranking_until_a_shape_exists() -> None:
    """Never reinterpret a median-ranking request as an ordinary rank/list.

    Median semantics need an explicit, published rule for odd/even row counts
    and ties.  Until an administrator publishes that query shape, the contract
    must return an explainable unsupported outcome.
    """
    preview = ContractSemanticIntentResolver().preview(
        message="杭州市人口排名中位数的是哪个街道，人口是多少",
        scope_area_code="3301",
        contracts=(population_semantic_contract_v1_3(),),
    )

    assert preview.status == "unsupported"
    assert preview.reason_code == "EXCLUDED_INTENT"


def test_contract_intent_preview_explains_match_and_rejection() -> None:
    contract = _contract(subject="device", subject_terms=["设备", "感知设备"])
    resolver = ContractSemanticIntentResolver()

    matched = resolver.preview(
        message="查询全市下辖单元设备平均数量",
        scope_area_code="3301",
        contracts=(contract,),
    )
    excluded = resolver.preview(
        message="查询全市下辖单元设备明细平均数量",
        scope_area_code="3301",
        contracts=(contract,),
    )
    unsupported_scope = resolver.preview(
        message="查询下辖单元设备平均数量",
        scope_area_code="330106",
        contracts=(contract,),
    )

    assert matched.status == "matched"
    assert matched.reason_code == "MATCHED_EXACT_SHAPE"
    assert matched.shape_id == "device_descendant_unit_avg_table"
    assert matched.spec is not None
    assert excluded.status == "unsupported"
    assert excluded.reason_code == "EXCLUDED_INTENT"
    assert unsupported_scope.status == "unsupported"
    assert unsupported_scope.reason_code == "SCOPE_NOT_SUPPORTED"


def test_intent_routed_contract_rejects_unknown_argument_placeholder() -> None:
    payload = _contract(subject="device", subject_terms=["设备"]).model_dump(
        mode="python"
    )
    payload["query_shapes"][0]["argument_template"] = {
        "query": {"metric": "$semantic.physical_column"}
    }

    with pytest.raises(ValueError, match="unknown semantic argument placeholder"):
        ToolSemanticContract.model_validate(payload)


async def test_publish_rejects_positive_example_that_is_not_one_exact_shape() -> None:
    payload = _contract(subject="device", subject_terms=["设备"]).model_dump(
        mode="python"
    )
    payload["examples"] = [
        {
            "question": "哪个下辖单元设备数量最多",
            "operator": "top",
            "metric": "item_count",
            "dimension": "descendant_unit",
        }
    ]
    duplicate = dict(payload["query_shapes"][1])
    duplicate["shape_id"] = "device_descendant_unit_top_duplicate_table"
    payload["query_shapes"] = (*payload["query_shapes"], duplicate)
    contract = ToolSemanticContract.model_validate(payload)
    repo = InMemoryCapabilityRepository()
    await repo.save_connector(
        Connector(
            connector_id="device-api",
            name="设备接口",
            base_url="https://device.internal",
            allowed_path_prefixes=["/device/"],
        )
    )
    service = CapabilityManagementService(repo)
    tool = await service.create_tool(
        capability_id="governance.query_device_metrics",
        name="设备聚合查询",
        owner="device-team",
        version="1.0.0",
        connector_ref="device-api",
        resource_path="/device/metrics",
        semantic_contract=contract,
    )
    tool = await service.advance_status(
        capability_id=tool.capability_id,
        version=tool.version,
        to_status="testing",
        changed_by="tester",
    )
    tool = await service.advance_status(
        capability_id=tool.capability_id,
        version=tool.version,
        to_status="pending_approval",
        changed_by="reviewer",
    )

    with pytest.raises(RunStateConflict, match="do not resolve uniquely"):
        await service.publish(
            capability_id=tool.capability_id,
            version=tool.version,
            published_by="publisher",
        )


async def test_new_semantic_subject_compiles_and_executes_without_catalog_source_edit() -> None:
    contract = _contract(subject="device", subject_terms=["设备"])
    payload = contract.model_dump(mode="python")
    for shape in payload["query_shapes"]:
        shape["argument_template"] = {
            "query": {
                "metrics": "$semantic.metrics",
                "operator": "$semantic.operator",
                "scope": "$semantic.scope",
                "group_by": "$semantic.group_by",
                "limit": "$semantic.limit",
            }
        }
    tool = ToolCapability(
        capability_id="governance.query_device_metrics",
        name="设备聚合查询",
        owner="device-team",
        version="1.0.0",
        status="published",
        guidance="Test guidance for 设备聚合查询",
        connector_ref="device-api",
        resource_path="/device/metrics",
        input_schema={
            "type": "object",
            "required": ["query"],
            "properties": {"query": {"type": "object"}},
        },
        output_schema={"type": "array"},
        data_schema_ref="schema://data/generic-table/1.0.0",
        required_permissions=["governance.device.aggregate.read"],
        dataset_ids=["devices"],
        semantic_contract=ToolSemanticContract.model_validate(payload),
    )
    base = ToolRegistry.default()
    manifests, descriptors = build_dynamic_tool_registry_entries(
        [tool], base_registry=base
    )
    registry = base.merge_dynamic(
        manifests=manifests,
        descriptors=descriptors,
        dynamic_input_schemas=build_dynamic_input_schemas([tool]),
    )
    resolver = SemanticActionResolver(
        catalog=SemanticCatalog.default(),
        registry=registry,
        policy=MinimalPolicyAdapter(),
    )
    auth = population_auth_context().model_copy(
        update={
            "entitlements": ["governance.device.aggregate.read"],
            "data_scopes": population_auth_context().data_scopes.model_copy(
                update={
                    "datasets": ["devices"],
                    "areas": [AuthorizedAreaScope(area_code="3301")],
                }
            ),
        }
    )

    resolution = resolver.resolve(
        {
            "catalog_version": resolver.catalog.catalog_version,
            "catalog_fingerprint": resolver.catalog.execution_fingerprint,
            "spec": {
                "subject": "device",
                "operator": "avg",
                "metrics": ["item_count"],
                "scope": {"area_code": "3301"},
                "group_by": ["descendant_unit"],
                "limit": 200,
                "output": "table",
            },
        },
        auth_context=auth,
    )

    assert isinstance(resolution, ResolvedSemanticAction)
    assert resolution.canonical_action.tool_id == "governance.query_device_metrics"
    assert resolution.canonical_action.arguments == {
        "query": {
            "metrics": ["item_count"],
            "operator": "avg",
            "scope": {"area_code": "3301", "include_descendants": True},
            "group_by": ["descendant_unit"],
            "limit": 200,
        }
    }

    class UnusedStaticAdapter:
        async def execute(self, **_kwargs):
            raise AssertionError("dynamic semantic Tool must not use static adapter")

    class RecordingDynamicAdapter:
        def __init__(self) -> None:
            self.arguments: dict[str, object] | None = None

        async def execute(self, *, arguments: dict[str, object], **_kwargs):
            self.arguments = arguments
            return TableDataResult(
                result_id="device-result",
                data_schema_ref="schema://data/generic-table/1.0.0",
                result_fingerprint="sha256:device-result",
                data=DynamicTableData(
                    rows=[{"unit_id": "u-1", "item_count": 12}]
                ),
                row_count=1,
            )

    dynamic_adapter = RecordingDynamicAdapter()
    result = await CapabilityService(
        registry=registry,
        policy=MinimalPolicyAdapter(),
        adapter=UnusedStaticAdapter(),
        dynamic_tool_adapter=dynamic_adapter,
    ).execute(
        tool_call_id="device-semantic-call",
        tool_id=resolution.canonical_action.tool_id,
        raw_arguments=resolution.canonical_action.arguments,
        auth_context=auth,
    )

    assert result.status == "success"
    assert result.data_result is not None
    assert dynamic_adapter.arguments == resolution.canonical_action.arguments
