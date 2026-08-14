from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import httpx
import pytest

from full_view_agent.api.app import RuntimeContainer, create_app
from full_view_agent.api.capability_routes import ModelConfigCreateBody
from full_view_agent.domain.capability import ModelConfig, ToolCapability
from full_view_agent.domain.models import LegacyIdentitySnapshot, Principal
from full_view_agent.infrastructure.credential_broker import InMemoryCredentialBroker


def _admin_runtime() -> RuntimeContainer:
    runtime = RuntimeContainer(credentials=InMemoryCredentialBroker())
    identity = LegacyIdentitySnapshot(
        principal=Principal(
            tenant_id="platform",
            user_id="control-admin",
            org_id="platform-admins",
            roles=["admin"],
        ),
        source="control-contract-test",
        source_session_expires_at=datetime.now(UTC) + timedelta(minutes=5),
        base_area_codes=[],
    )
    runtime.capability_identity_port = AsyncMock()
    runtime.capability_identity_port.resolve = AsyncMock(return_value=identity)
    return runtime


def test_model_create_numeric_limits_match_the_domain_contract() -> None:
    api_properties = ModelConfigCreateBody.model_json_schema()["properties"]
    domain_properties = ModelConfig.model_json_schema()["properties"]

    for field in ("timeout_seconds", "max_output_tokens", "max_retries"):
        assert api_properties[field]["minimum"] == domain_properties[field]["minimum"]
        assert api_properties[field]["maximum"] == domain_properties[field]["maximum"]
        assert api_properties[field]["default"] == domain_properties[field]["default"]


def test_lifecycle_reason_is_required_with_non_empty_openapi_contract() -> None:
    schemas = create_app(RuntimeContainer()).openapi()["components"]["schemas"]

    for schema_name in (
        "ApplicationLifecycleBody",
        "LifecycleActionBody",
        "RollbackBody",
    ):
        schema = schemas[schema_name]
        assert "reason" in schema["required"]
        assert schema["properties"]["reason"]["minLength"] == 1


@pytest.mark.asyncio
async def test_workflow_detail_and_dry_run_round_trip_condition_join_contract() -> None:
    app = create_app(_admin_runtime())
    payload = {
        "capability_id": "workflow.condition-join",
        "name": "Condition join",
        "owner": "runtime-team",
        "version": "1.0.0",
        "nodes": [
            {"node_id": "start", "node_type": "start"},
            {
                "node_id": "query",
                "node_type": "tool",
                "tool_capability_id": "governance.query_population_metrics",
                "tool_version": "1.0.0",
                "config": {
                    "arguments": {
                        "query": {
                            "metrics": ["person_count"],
                            "scope": {"area_code": "330106"},
                            "group_by": ["street"],
                        }
                    }
                },
            },
            {
                "node_id": "condition",
                "node_type": "condition",
                "condition_expression": 'result.query.status == "success"',
            },
            {"node_id": "ok", "node_type": "summary", "config": {"text": "ok"}},
            {
                "node_id": "partial",
                "node_type": "summary",
                "config": {"text": "partial"},
            },
            {
                "node_id": "merge",
                "node_type": "join",
                "config": {"mode": "selected"},
            },
            {"node_id": "end", "node_type": "end"},
        ],
        "edges": [
            {"source_node_id": "start", "target_node_id": "query"},
            {"source_node_id": "query", "target_node_id": "condition"},
            {
                "source_node_id": "condition",
                "target_node_id": "ok",
                "condition": "true",
            },
            {
                "source_node_id": "condition",
                "target_node_id": "partial",
                "condition": "false",
            },
            {"source_node_id": "ok", "target_node_id": "merge"},
            {"source_node_id": "partial", "target_node_id": "merge"},
            {"source_node_id": "merge", "target_node_id": "end"},
        ],
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"geoToken": "legacy-admin-token"},
    ) as client:
        created = await client.post("/capability-api/v1/workflows", json=payload)
        detail = await client.get(
            "/capability-api/v1/workflows/workflow.condition-join/1.0.0"
        )
        dry_run = await client.post(
            "/capability-api/v1/workflows/workflow.condition-join/1.0.0/dry-run"
        )

    assert created.status_code == 201, created.text
    assert detail.status_code == 200, detail.text
    assert dry_run.status_code == 200, dry_run.text
    detail_data = detail.json()["data"]
    assert detail_data["nodes"] == created.json()["data"]["nodes"]
    assert detail_data["edges"] == created.json()["data"]["edges"]
    validated = dry_run.json()["data"]
    assert validated["valid"] is True
    assert validated["executable"] is True
    runtime_nodes = {node["node_id"]: node for node in validated["workflow"]["nodes"]}
    assert runtime_nodes["condition"]["condition_expression"] == (
        'result.query.status == "success"'
    )
    assert runtime_nodes["merge"]["config_json"] == '{"mode":"selected"}'


@pytest.mark.asyncio
async def test_workflow_dry_run_uses_published_dynamic_tool_registry() -> None:
    runtime = _admin_runtime()
    assert runtime.capability_repository is not None
    await runtime.capability_repository.save_tool(
        ToolCapability(
            capability_id="governance.dynamic_dry_run",
            name="Dynamic dry-run tool",
            domain="governance",
            owner="runtime-team",
            version="1.0.0",
            status="published",
            risk_level="low",
            required_permissions=["governance.population.aggregate.read"],
            dataset_ids=["population"],
            description="Dynamic workflow dry-run fixture",
            connector_ref="connector.dynamic-dry-run",
            http_method="GET",
            resource_path="/v1/population",
            input_schema={
                "type": "object",
                "properties": {"area_code": {"type": "string"}},
                "required": ["area_code"],
                "additionalProperties": False,
            },
            output_schema={"type": "object"},
            parameter_mapping={"area_code": "$.area_code"},
            result_mapping={"rows": "$.rows"},
            result_kind="table",
            data_schema_ref="schema://population-metric-table/1.0",
        )
    )
    app = create_app(runtime)
    payload = {
        "capability_id": "workflow.dynamic-dry-run",
        "name": "Dynamic dry-run workflow",
        "owner": "runtime-team",
        "version": "1.0.0",
        "nodes": [
            {"node_id": "start", "node_type": "start"},
            {
                "node_id": "dynamic",
                "node_type": "tool",
                "tool_capability_id": "governance.dynamic_dry_run",
                "tool_version": "1.0.0",
                "config": {"arguments": {"area_code": "330106"}},
            },
            {"node_id": "end", "node_type": "end"},
        ],
        "edges": [
            {"source_node_id": "start", "target_node_id": "dynamic"},
            {"source_node_id": "dynamic", "target_node_id": "end"},
        ],
    }

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"geoToken": "legacy-admin-token"},
    ) as client:
        created = await client.post("/capability-api/v1/workflows", json=payload)
        dry_run = await client.post(
            "/capability-api/v1/workflows/workflow.dynamic-dry-run/1.0.0/dry-run"
        )

    assert created.status_code == 201, created.text
    assert dry_run.status_code == 200, dry_run.text
    assert dry_run.json()["data"]["executable"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path,payload",
    [
        ("/capability-api/v1/applications/full-view/enable", {"expected_etag": 1}),
        (
            "/capability-api/v1/skills/governance.area-analysis/1.0.0/publish",
            {"expected_etag": 1},
        ),
        (
            "/capability-api/v1/tools/governance.resolve-area/rollback",
            {"to_version": "1.0.0", "expected_etag": 1},
        ),
    ],
)
async def test_lifecycle_http_routes_cannot_bypass_required_reason(
    path: str, payload: dict[str, object]
) -> None:
    app = create_app(_admin_runtime())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"geoToken": "legacy-admin-token"},
    ) as client:
        response = await client.post(path, json=payload)

    assert response.status_code == 422
    assert response.json()["error"]["details"] == [
        {
            "field": "reason",
            "code": "missing",
            "message": "Field required",
        }
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers,url,expected_status,expected_code",
    [
        ({}, "/capability-api/v1/applications", 401, "unauthenticated"),
        (
            {
                "geoToken": "legacy-token",
                "Authorization": "Bearer control-token",
            },
            "/capability-api/v1/applications",
            400,
            "invalid_authentication_transport",
        ),
        (
            {"Authorization": "Bearer control-token"},
            "/capability-api/v1/applications?geoToken=url-token",
            400,
            "invalid_authentication_transport",
        ),
    ],
)
async def test_control_plane_identity_transports_fail_closed(
    headers: dict[str, str],
    url: str,
    expected_status: int,
    expected_code: str,
) -> None:
    app = create_app(RuntimeContainer(credentials=InMemoryCredentialBroker()))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers=headers,
    ) as client:
        response = await client.get(url)

    assert response.status_code == expected_status
    assert response.json()["error"]["code"] == expected_code


@pytest.mark.asyncio
async def test_model_enable_and_disable_return_the_common_api_envelope() -> None:
    app = create_app(_admin_runtime())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"geoToken": "legacy-admin-token"},
    ) as client:
        created = await client.post(
            "/capability-api/v1/model-configs",
            json={
                "name": "contract-model",
                "api_base_url": "https://model.example/v1",
                "api_key": "test-key",
                "model_name": "test-model",
            },
        )
        assert created.status_code == 201
        config_id = created.json()["data"]["config_id"]

        enabled = await client.post(
            f"/capability-api/v1/model-configs/{config_id}/enable"
        )
        disabled = await client.post(
            f"/capability-api/v1/model-configs/{config_id}/disable"
        )

    for response in (enabled, disabled):
        assert response.status_code == 200
        assert response.json()["data"] == {"status": "ok"}
        assert response.json()["meta"]["request_id"].startswith("req_")
        assert response.json()["meta"]["trace_id"].startswith("trc_")
