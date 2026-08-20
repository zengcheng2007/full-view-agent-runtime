from __future__ import annotations

from pathlib import Path

import pytest

from full_view_agent.api.app import RuntimeContainer, create_app
from full_view_agent.application.capability_management_service import (
    CapabilityManagementService,
)
from full_view_agent.application.dynamic_tool_bridge import (
    build_dynamic_tool_registry_entries,
    convert_tool_capability_to_manifest,
)
from full_view_agent.application.errors import RunStateConflict, UpstreamContractError
from full_view_agent.application.harness import ToolAction
from full_view_agent.application.policy import MinimalPolicyAdapter
from full_view_agent.application.run_capability_snapshot import (
    RunCapabilitySnapshotService,
)
from full_view_agent.application.run_capability_snapshot_store import (
    InMemoryRunCapabilitySnapshotStore,
)
from full_view_agent.application.tool_observation_service import _with_presentation
from full_view_agent.application.tool_registry import ToolRegistry
from full_view_agent.domain.capability import (
    Connector,
    ToolCapability,
    ToolSemanticContract,
)
from full_view_agent.domain.models import QueryPopulationMetricsInput
from full_view_agent.infrastructure.capability_repository import (
    InMemoryCapabilityRepository,
)
from full_view_agent.infrastructure.governance_adapter import _population_result
from full_view_agent.semantic.action_resolver import (
    RejectedSemanticAction,
    ResolvedSemanticAction,
    SemanticActionResolver,
)
from full_view_agent.semantic.catalog import SemanticCatalog
from full_view_agent.semantic.presenter import SemanticToolPresenter
from full_view_agent.semantic.tool_contract_compiler import (
    ToolSemanticContractCompiler,
    ToolSemanticQuery,
    ToolSemanticQueryRejected,
)
from tests.test_policy import population_auth_context


def _population_contract(
    *,
    operators: list[str] | None = None,
    output_forms: list[str] | None = None,
) -> ToolSemanticContract:
    selected_operators = operators or [
        "list", "sum", "avg", "min", "max", "top", "bottom", "rank"
    ]
    selected_outputs = output_forms or ["table"]
    return ToolSemanticContract.model_validate(
        {
            "schema_version": "1.0",
            "subject": "population",
            "metrics": [
                {
                    "metric_id": "person_count",
                    "label": "人口数",
                    "unit": "人",
                    "value_type": "integer",
                }
            ],
            "dimensions": [
                {
                    "dimension_id": "descendant_street",
                    "label": "下辖街道",
                    "kind": "administrative_area",
                }
            ],
            "filters": [
                {
                    "field": "person_category",
                    "label": "人口类别",
                    "operators": ["eq"],
                    "allowed_values": ["solitary_elderly"],
                }
            ],
            "operators": selected_operators,
            "sort": {
                "allowed_fields": ["person_count", "descendant_street"],
                "default_direction": "desc",
                "tie_policy": "include_all",
                "tie_breakers": ["descendant_street"],
            },
            "completeness": {
                "mode": "complete",
                "statement": "覆盖授权范围内全部下辖街道。",
            },
            "output_forms": selected_outputs,
            "examples": ([
                {
                    "question": "哪个街道人口最多？",
                    "operator": "top",
                    "metric": "person_count",
                    "dimension": "descendant_street",
                }
            ] if "top" in selected_operators else []),
            "limitations": ["不提供个人明细"],
            "query_shapes": [
                {
                    "shape_id": f"population_descendant_street_{operator}_{output}",
                    "metric_selection": ["person_count"],
                    "dimension_selection": ["descendant_street"],
                    "operator_selection": [operator],
                    "scope_levels": [4],
                    "allowed_filters": ["person_category"],
                    "output_forms": [output],
                    "completeness": {
                        "mode": "complete",
                        "statement": "覆盖授权范围内全部下辖街道。",
                    },
                    "result_schema_ref": (
                        "schema://data/population-aggregate-table/1.0.0"
                        if operator in {"sum", "avg", "min", "max"}
                        else "schema://data/population-ranking-table/1.0.0"
                    ),
                    "result_row_fields": (
                        ["operator", "metric", "value", "area_count", "completeness"]
                        if operator in {"sum", "avg", "min", "max"}
                        else ["rank", "area_code", "area_name", "person_count"]
                    ),
                    "result_fingerprint_domain": (
                        "data-result:population-aggregate-table:1.0.0"
                        if operator in {"sum", "avg", "min", "max"}
                        else "data-result:population-ranking-table:1.0.0"
                    ),
                }
                for operator in selected_operators
                for output in selected_outputs
            ],
        }
    )


def _tool(*, version: str, contract: ToolSemanticContract | None) -> ToolCapability:
    return ToolCapability(
        capability_id="governance.query_population_metrics",
        name="查询人口聚合",
        owner="governance",
        version=version,
        status="published",
        guidance="Test guidance for population query tool",
        connector_ref="geo",
        resource_path="/population",
        dataset_ids=["population"],
        semantic_contract=contract,
    )


def test_contract_rejects_unknown_operator_and_dangling_sort_field() -> None:
    payload = _population_contract().model_dump(mode="python")
    payload["operators"] = ["list", "median"]
    with pytest.raises(ValueError):
        ToolSemanticContract.model_validate(payload)

    payload = _population_contract().model_dump(mode="python")
    payload["sort"]["allowed_fields"] = (
        *payload["sort"]["allowed_fields"],
        "physical_column",
    )
    with pytest.raises(ValueError, match="sort field"):
        ToolSemanticContract.model_validate(payload)


@pytest.mark.parametrize(
    ("operator", "expected_direction"),
    [("top", "desc"), ("bottom", "asc"), ("avg", None)],
)
def test_population_street_operators_compile_deterministically(
    operator: str, expected_direction: str | None
) -> None:
    tool = _tool(version="1.2.0", contract=_population_contract())
    compiler = ToolSemanticContractCompiler()

    plan = compiler.compile(
        tool,
        ToolSemanticQuery(
            operator=operator,
            metric="person_count",
            dimension="descendant_street",
            scope_level=4,
            limit=10,
            output_form="table",
        ),
    )

    assert (plan.tool_id, plan.tool_version, plan.subject) == (
        tool.capability_id,
        "1.2.0",
        "population",
    )
    assert plan.sort_direction == expected_direction
    assert plan.tie_policy == "include_all"
    assert plan.completeness.mode == "complete"


def test_compiler_fails_closed_for_operator_not_supported_by_exact_version() -> None:
    tool = _tool(version="1.0.0", contract=_population_contract(operators=["list"]))
    with pytest.raises(ToolSemanticQueryRejected) as exc_info:
        ToolSemanticContractCompiler().compile(
            tool,
            ToolSemanticQuery(
                operator="avg",
                metric="person_count",
                dimension="descendant_street",
                scope_level=4,
            ),
        )
    assert exc_info.value.code == "SEMANTIC_OPERATOR_UNSUPPORTED"


@pytest.mark.asyncio
async def test_publish_rejects_missing_contract_without_mutating_status() -> None:
    repo = InMemoryCapabilityRepository()
    await repo.save_connector(
        Connector(
            connector_id="geo",
            name="Geo",
            base_url="https://geo.invalid",
            allowed_path_prefixes=["/population"],
        )
    )
    service = CapabilityManagementService(repo)
    tool = await service.create_tool(
        capability_id="governance.query_population_metrics",
        name="人口查询",
        owner="governance",
        version="1.1.0",
        connector_ref="geo",
        resource_path="/population",
        dataset_ids=["population"],
    )
    await service.mark_testing(
        capability_id=tool.capability_id,
        version=tool.version,
        changed_by="tester",
        expected_etag=tool.etag,
    )
    pending = await service.advance_status(
        capability_id=tool.capability_id,
        version=tool.version,
        to_status="pending_approval",
        changed_by="approver",
        expected_etag=tool.etag + 1,
    )

    with pytest.raises(RunStateConflict, match="semantic contract"):
        await service.publish(
            capability_id=tool.capability_id,
            version=tool.version,
            published_by="publisher",
            expected_etag=pending.etag,
        )

    unchanged = await repo.get(tool.capability_id, tool.version)
    assert unchanged is not None
    assert unchanged.status == "pending_approval"
    assert await repo.get_active_snapshot(tool.capability_id) is None


@pytest.mark.asyncio
async def test_run_snapshot_pins_contract_version_across_publish_and_restart() -> None:
    repo = InMemoryCapabilityRepository()
    store = InMemoryRunCapabilitySnapshotStore()
    v1 = _tool(version="1.0.0", contract=_population_contract(operators=["list"]))
    await repo.save_tool(v1)
    snapshots = RunCapabilitySnapshotService(repo, store)
    first = await snapshots.create_snapshot_for_run("run-old", ToolRegistry.default())

    v2 = _tool(version="2.0.0", contract=_population_contract())
    await repo.save_tool(
        v1.model_copy(update={"status": "disabled", "etag": v1.etag + 1})
    )
    await repo.save_tool(v2)
    second = await snapshots.create_snapshot_for_run("run-new", ToolRegistry.default())

    old_contract = first.tool_registry.get_semantic_contract(v1.capability_id)
    new_contract = second.tool_registry.get_semantic_contract(v2.capability_id)
    assert first.tool_versions[v1.capability_id] == "1.0.0"
    assert second.tool_versions[v2.capability_id] == "2.0.0"
    assert old_contract.operators == ("list",)
    assert "avg" in new_contract.operators

    restarted = RunCapabilitySnapshotService(repo, store)
    restored = await restarted.create_snapshot_for_run(
        "run-old", ToolRegistry.default()
    )
    assert restored.tool_versions[v1.capability_id] == "1.0.0"
    assert restored.tool_registry.get_semantic_contract(
        v1.capability_id
    ).operators == ("list",)


@pytest.mark.parametrize(
    ("operator", "expected_direction"),
    [("top", "desc"), ("bottom", "asc"), ("avg", None)],
)
def test_semantic_query_compiles_contract_into_canonical_tool_action(
    operator: str, expected_direction: str | None
) -> None:
    tool = _tool(version="1.0.0", contract=_population_contract())
    base = ToolRegistry.default()
    manifests, descriptors = build_dynamic_tool_registry_entries(
        [tool], base_registry=base
    )
    registry = base.merge_dynamic(manifests=manifests, descriptors=descriptors)
    catalog = SemanticCatalog.default()
    resolver = SemanticActionResolver(
        catalog=catalog,
        registry=registry,
        policy=MinimalPolicyAdapter(),
    )
    resolution = resolver.compile_action(
        {
            "catalog_version": catalog.catalog_version,
            "catalog_fingerprint": catalog.execution_fingerprint,
            "spec": {
                "subject": "population",
                "operator": operator,
                "metrics": ["person_count"],
                "scope": {"area_code": "3301"},
                "group_by": ["descendant_street"],
            },
        },
        auth_context=population_auth_context().model_copy(
            update={
                "data_scopes": population_auth_context().data_scopes.model_copy(
                    update={"areas": [{"area_code": "3301"}]}
                )
            }
        ),
    )
    assert isinstance(resolution, ResolvedSemanticAction)
    query = resolution.canonical_action.arguments["query"]
    assert query["operator"] == operator
    assert query["group_by"] == ["descendant_street"]
    if expected_direction is None:
        assert query["order_by"] == []
    else:
        assert query["order_by"] == [
            {"field": "person_count", "direction": expected_direction}
        ]
    description = registry.get_model_descriptor(tool.capability_id).description
    assert "Tool@1.0.0" in description
    assert "top" in description and "avg" in description
    presentation = SemanticToolPresenter(
        catalog=catalog, registry=registry
    ).present(auth_context=population_auth_context())
    assert presentation is not None
    assert "operators=['list', 'sum', 'avg'" in presentation.description
    assert "tie_policy=include_all" in presentation.description


def test_semantic_query_rejects_operation_missing_from_run_pinned_contract() -> None:
    tool = _tool(version="1.0.0", contract=_population_contract(operators=["list"]))
    base = ToolRegistry.default()
    manifests, descriptors = build_dynamic_tool_registry_entries(
        [tool], base_registry=base
    )
    registry = base.merge_dynamic(manifests=manifests, descriptors=descriptors)
    catalog = SemanticCatalog.default()
    resolution = SemanticActionResolver(
        catalog=catalog,
        registry=registry,
        policy=MinimalPolicyAdapter(),
    ).compile_action(
        {
            "catalog_version": catalog.catalog_version,
            "catalog_fingerprint": catalog.execution_fingerprint,
            "spec": {
                "subject": "population",
                "operator": "avg",
                "metrics": ["person_count"],
                "scope": {"area_code": "3301"},
                "group_by": ["descendant_street"],
            },
        },
        auth_context=population_auth_context().model_copy(
            update={
                "data_scopes": population_auth_context().data_scopes.model_copy(
                    update={"areas": [{"area_code": "3301"}]}
                )
            }
        ),
    )
    assert isinstance(resolution, RejectedSemanticAction)
    assert resolution.codes == ("SEMANTIC_OPERATOR_UNSUPPORTED",)


def test_tool_semantic_http_contract_is_typed_in_openapi() -> None:
    schema = create_app(RuntimeContainer()).openapi()
    paths = schema["paths"]
    assert paths["/capability-api/v1/tools"]["post"]["responses"]["201"][
        "content"
    ]["application/json"]["schema"]["$ref"].endswith("/ToolDefinitionResponse")
    assert paths["/capability-api/v1/tools/{capability_id}/{version}"]["get"][
        "responses"
    ]["200"]["content"]["application/json"]["schema"]["$ref"].endswith(
        "/ToolDefinitionResponse"
    )
    assert paths[
        "/capability-api/v1/tools/{capability_id}/{version}/publish"
    ]["post"]["responses"]["200"]["content"]["application/json"]["schema"][
        "$ref"
    ].endswith("/ToolPublishResponse")
    for path, response_name in (
        (
            "/capability-api/v1/tools/{capability_id}/{version}/semantic-contract/validate",
            "ToolSemanticValidationResponse",
        ),
        (
            "/capability-api/v1/tools/{capability_id}/semantic-contract/effective",
            "ToolSemanticEffectiveResponse",
        ),
        (
            "/capability-api/v1/tools/{capability_id}/{version}/semantic-contract/diff",
            "ToolSemanticDiffResponse",
        ),
        (
            "/capability-api/v1/tools/{capability_id}/{version}/semantic-contract/preview",
            "ToolSemanticPreviewResponse",
        ),
    ):
        assert path in paths
        method = "post" if path.endswith(("validate", "preview")) else "get"
        response = paths[path][method]["responses"]["200"]["content"][
            "application/json"
        ]["schema"]["$ref"]
        assert response.endswith(f"/{response_name}")

    contract_schema = schema["components"]["schemas"]["ToolSemanticContract"]
    assert set(contract_schema["required"]) >= {
        "subject",
        "metrics",
        "dimensions",
        "filters",
        "operators",
        "sort",
        "completeness",
        "output_forms",
        "examples",
        "limitations",
        "query_shapes",
    }
    shape_schema = schema["components"]["schemas"]["ToolSemanticQueryShape"]
    assert set(shape_schema["required"]) >= {
        "shape_id",
        "metric_selection",
        "dimension_selection",
        "operator_selection",
        "scope_levels",
        "allowed_filters",
        "output_forms",
        "completeness",
        "result_schema_ref",
        "result_row_fields",
        "result_fingerprint_domain",
    }


def test_v027_persists_and_backfills_population_contract() -> None:
    migration = (
        Path(__file__).parents[1]
        / "scripts"
        / "migrations"
        / "V027_tool_semantic_contracts.sql"
    ).read_text(encoding="utf-8")
    assert "ADD COLUMN IF NOT EXISTS semantic_contract JSONB" in migration
    assert "governance.query_population_metrics" in migration
    assert "source.version = '1.0.0'" in migration
    assert "'1.1.0'" in migration
    assert "SET semantic_contract" not in migration
    assert "capability_version <> '1.1.0'" in migration
    assert "CROSS JOIN outputs" not in migration
    assert "('list', 'choropleth')" in migration
    assert "('avg', 'table')" in migration
    assert "('avg', 'choropleth')" not in migration
    assert '"top"' in migration and '"bottom"' in migration and '"avg"' in migration
    assert "SELECT 27" in migration


@pytest.mark.parametrize(
    ("operator", "expected_names", "expected_value"),
    [
        ("top", ["甲", "乙"], None),
        ("rank", ["甲", "乙"], None),
        ("bottom", ["丁"], None),
        ("avg", [], 17.5),
    ],
)
def test_population_operations_apply_ties_and_complete_average(
    operator: str, expected_names: list[str], expected_value: float | None
) -> None:
    arguments = QueryPopulationMetricsInput.model_validate(
        {
            "query": {
                "metrics": ["person_count"],
                "operator": operator,
                "scope": {"area_code": "3301"},
                "group_by": ["descendant_street"],
                "limit": 1,
            }
        }
    )
    result = _population_result(
        arguments=arguments,
        parsed_rows=[
            ("330101001", "甲", 30),
            ("330102001", "乙", 30),
            ("330103001", "丙", 10),
            ("330104001", "丁", 0),
        ],
        upstream_truncated=False,
        ranking=True,
    )
    rows = result.data.rows
    if expected_value is None:
        assert [row.area_name for row in rows] == expected_names
    else:
        assert len(rows) == 1
        assert rows[0].operator == "avg"
        assert rows[0].value == expected_value
        assert rows[0].area_count == 4
        assert rows[0].completeness == "complete"
        presented = _with_presentation(
            result,
            action=ToolAction(
                tool_id="governance.query_population_metrics",
                arguments=arguments.model_dump(mode="python"),
            ),
        )
        assert presented.presentation is not None
        assert "平均值" in presented.presentation.title
        assert presented.presentation.visualizations[1].kind == "metric"


def test_population_average_fails_closed_when_upstream_is_incomplete() -> None:
    arguments = QueryPopulationMetricsInput.model_validate(
        {
            "query": {
                "metrics": ["person_count"],
                "operator": "avg",
                "scope": {"area_code": "3301"},
                "group_by": ["descendant_street"],
            }
        }
    )
    with pytest.raises(UpstreamContractError, match="complete"):
        _population_result(
            arguments=arguments,
            parsed_rows=[("330101001", "甲", 30)],
            upstream_truncated=True,
            ranking=True,
        )


def test_contract_compiler_rejects_undeclared_scope_dimension_combination() -> None:
    tool = _tool(version="1.0.0", contract=_population_contract())
    with pytest.raises(ToolSemanticQueryRejected) as exc_info:
        ToolSemanticContractCompiler().compile(
            tool,
            ToolSemanticQuery(
                operator="top",
                metric="person_count",
                dimension="descendant_street",
                scope_level=6,
            ),
        )
    assert exc_info.value.code == "SEMANTIC_SHAPE_UNSUPPORTED"


@pytest.mark.asyncio
async def test_run_restart_fails_closed_if_same_tool_version_contract_drifts() -> None:
    repo = InMemoryCapabilityRepository()
    store = InMemoryRunCapabilitySnapshotStore()
    original = _tool(version="1.1.0", contract=_population_contract())
    await repo.save_tool(original)
    first = RunCapabilitySnapshotService(repo, store)
    await first.create_snapshot_for_run("run-contract-drift", ToolRegistry.default())

    changed = _tool(
        version="1.1.0", contract=_population_contract(operators=["list"])
    ).model_copy(update={"etag": original.etag + 1})
    await repo.save_tool(changed)
    restarted = RunCapabilitySnapshotService(repo, store)
    with pytest.raises(RuntimeError, match="cannot rebuild capability snapshot"):
        await restarted.create_snapshot_for_run(
            "run-contract-drift", ToolRegistry.default()
        )


def test_builtin_physical_manifest_has_no_semantic_fallback_and_bridge_keeps_contract() -> None:
    manifest = ToolRegistry.default().get_manifest(
        "governance.query_population_metrics"
    )
    assert manifest.semantic_contract is None
    tool = _tool(version="1.1.0", contract=_population_contract())
    converted = convert_tool_capability_to_manifest(tool)
    assert converted.tool_version == "1.1.0"
    assert converted.semantic_contract == tool.semantic_contract


@pytest.mark.asyncio
async def test_no_database_runtime_loads_published_population_contract_from_control_plane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("FULL_VIEW_DATABASE_URL", raising=False)
    runtime = RuntimeContainer()
    await runtime.initialize()

    manifest = runtime.tool_registry.get_manifest(
        "governance.query_population_metrics"
    )
    assert manifest.tool_version == "1.3.0"
    assert manifest.semantic_contract == _population_contract_from_runtime_seed()
    assert runtime.application_registry is not None
    bindings = await runtime.application_registry.list_capability_bindings(
        app_id="full_information_view", enabled_only=True
    )
    population_bindings = [
        item
        for item in bindings
        if item.capability_id == "governance.query_population_metrics"
    ]
    assert [item.capability_version for item in population_bindings] == ["1.3.0"]


def _population_contract_from_runtime_seed() -> ToolSemanticContract:
    from full_view_agent.application.builtin_capability_seeds import (
        population_semantic_contract_v1_3,
    )

    return population_semantic_contract_v1_3()


def test_every_in_memory_published_population_shape_compiles_and_matches_result() -> None:
    from full_view_agent.application.builtin_capability_seeds import population_tool_v1_1

    tool = population_tool_v1_1()
    assert tool.semantic_contract is not None
    compiler = ToolSemanticContractCompiler()
    for shape in tool.semantic_contract.query_shapes:
        operator = shape.operator_selection[0]
        output = shape.output_forms[0]
        dimension = shape.dimension_selection[0]
        plan = compiler.compile(
            tool,
            ToolSemanticQuery.model_validate(
                {
                    "operator": operator,
                    "metric": "person_count",
                    "dimension": dimension,
                    "scope_level": shape.scope_levels[0],
                    "output_form": output,
                }
            ),
        )
        arguments = QueryPopulationMetricsInput.model_validate(
            {
                "query": {
                    "metrics": ["person_count"],
                    "operator": operator,
                    "scope": {"area_code": "3301"},
                    "group_by": [dimension],
                    "limit": 10,
                    "presentation_hint": output,
                }
            }
        )
        result = _population_result(
            arguments=arguments,
            parsed_rows=[("330101001", "甲", 30), ("330102001", "乙", 10)],
            upstream_truncated=False,
            ranking="ranking-table" in shape.result_schema_ref,
        )
        assert result.data_schema_ref == plan.result_schema_ref
        actual_fields = set(result.data.rows[0].model_dump())
        assert actual_fields == set(plan.result_row_fields)
