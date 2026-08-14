"""R2G1 real integration test: dynamic tool execution against a controlled upstream.

This test proves that:
1. A Connector/Tool can be created, approved, and published through full lifecycle
2. The published Tool can be executed through CapabilityService + HttpConnectorExecutor
3. The controlled upstream receives the actual HTTP request with correct parameters
4. Result/Evidence is formed correctly
5. Version isolation: old Run keeps old snapshot, new Run uses new version
"""

from __future__ import annotations

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import pytest

from full_view_agent.application.capability_management_service import (
    CapabilityManagementService,
)
from full_view_agent.application.capability_service import CapabilityService
from full_view_agent.application.dynamic_tool_adapter import HttpDynamicToolAdapter
from full_view_agent.application.policy import MinimalPolicyAdapter
from full_view_agent.application.run_capability_snapshot import (
    RunCapabilitySnapshotService,
)
from full_view_agent.application.tool_registry import ToolRegistry
from full_view_agent.domain import models
from full_view_agent.domain.capability import Connector, ToolCapability
from full_view_agent.infrastructure.capability_repository import (
    InMemoryCapabilityRepository,
)
from full_view_agent.infrastructure.http_connector_executor import (
    HttpConnectorExecutor,
)


class _UpstreamHandler(BaseHTTPRequestHandler):
    """Controlled upstream that records requests."""

    received_requests: list[dict[str, Any]] = []
    response_payload: dict[str, Any] = {
        "rows": [
            {"area_code": "330106", "area_name": "西湖区", "population": 100000},
            {"area_code": "330102", "area_name": "上城区", "population": 50000},
        ],
    }

    def log_message(self, format: str, *args: Any) -> None:
        pass  # suppress logs

    def do_GET(self) -> None:
        _UpstreamHandler.received_requests.append(
            {
                "method": "GET",
                "path": self.path,
                "headers": dict(self.headers),
            }
        )
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(self.response_payload).encode())

    def do_POST(self) -> None:
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length).decode() if content_length else ""
        _UpstreamHandler.received_requests.append(
            {
                "method": "POST",
                "path": self.path,
                "headers": dict(self.headers),
                "body": body,
            }
        )
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(self.response_payload).encode())


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture()
def upstream_server():
    """Start a local HTTP server as the controlled upstream."""
    port = _find_free_port()
    server = HTTPServer(("127.0.0.1", port), _UpstreamHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    _UpstreamHandler.received_requests = []
    yield f"http://127.0.0.1:{port}"
    server.shutdown()


def _sample_auth() -> models.AuthContext:
    """Build a minimal valid AuthContext for tests."""
    return models.AuthContext.model_validate(
        {
            "auth_context_id": "authctx-test",
            "auth_context_fingerprint": "sha256:test",
            "principal": {
                "tenant_id": "tenant-hz",
                "user_id": "test-user",
                "org_id": "org-01",
                "roles": ["admin"],
            },
            "application": {
                "app_id": "full_information_view",
                "agent_id": "governance_general_agent",
            },
            "entitlements": ["governance.query"],
            "data_scopes": {
                "areas": [{"area_code": "330106", "include_descendants": True}],
                "datasets": ["population"],
                "field_policy_set": "governance_analyst_v1",
            },
            "purpose": "interactive_analysis",
            "session_id": "session-test",
            "run_id": "run-test",
            "credential_ref": "",
            "issued_at": "2026-01-01T00:00:00Z",
            "expires_at": "2099-01-01T00:00:00Z",
            "policy_version": "v1",
        }
    )


async def _publish_through_lifecycle(
    management: CapabilityManagementService,
    capability_id: str,
    version: str,
    published_by: str,
) -> None:
    """Move a capability through full lifecycle."""
    await management.advance_status(
        capability_id=capability_id,
        version=version,
        to_status="testing",
        changed_by=published_by,
        reason="auto-advance",
    )
    await management.advance_status(
        capability_id=capability_id,
        version=version,
        to_status="pending_approval",
        changed_by=published_by,
        reason="auto-advance",
    )
    await management.publish(
        capability_id=capability_id,
        version=version,
        published_by=published_by,
    )


class _AllowAllPolicy(MinimalPolicyAdapter):
    """Policy that always allows - for testing only."""

    def _evaluate(
        self,
        *,
        manifest: Any,
        auth_context: Any,
        arguments: Any,
        phase: str,
        result: Any,
    ) -> Any:
        decision = super()._evaluate(
            manifest=manifest,
            auth_context=auth_context,
            arguments=arguments,
            phase=phase,
            result=result,
        )
        return decision.model_copy(update={"decision": "allow"})


@pytest.mark.asyncio
async def test_real_dynamic_tool_execution(upstream_server: str) -> None:
    """Prove that a published Tool is executed against the real upstream.

    This is NOT an import/constructor check. It:
    1. Creates a real Connector + Tool
    2. Publishes through the full lifecycle
    3. Executes through CapabilityService with real HttpConnectorExecutor
    4. Asserts the upstream received the actual HTTP request
    """
    repo = InMemoryCapabilityRepository()
    management = CapabilityManagementService(repo)

    executor = HttpConnectorExecutor(
        repo,
        allowed_private_hosts=frozenset({"127.0.0.1"}),
    )
    adapter = HttpDynamicToolAdapter(repository=repo, http_executor=executor)

    registry = ToolRegistry.default()
    # Merge dynamic tools from repo
    from full_view_agent.application.dynamic_tool_bridge import (
        build_dynamic_tool_registry_entries,
        load_published_tools,
    )

    published = await load_published_tools(repo)
    manifests, descriptors = build_dynamic_tool_registry_entries(published)
    dynamic_input_schemas = {t.capability_id: t.input_schema for t in published}
    registry = registry.merge_dynamic(
        manifests=manifests,
        descriptors=descriptors,
        dynamic_input_schemas=dynamic_input_schemas,
    )

    policy = _AllowAllPolicy()
    service = CapabilityService(
        registry=registry,
        policy=policy,
        adapter=adapter,
        dynamic_tool_adapter=adapter,
    )

    # Create Connector pointing to our test upstream
    connector = Connector(
        connector_id="test-upstream-connector",
        name="Test Upstream",
        base_url=upstream_server,
        allowed_path_prefixes=["/api"],
    )
    await repo.save_connector(connector)

    # Create Tool v1
    tool_v1 = ToolCapability(
        capability_id="test.dynamic.query",
        name="Dynamic Query Tool",
        domain="governance",
        owner="test",
        version="1.0.0",
        status="draft",
        risk_level="low",
        required_permissions=[],
        dataset_ids=["population"],
        connector_ref="test-upstream-connector",
        http_method="GET",
        resource_path="/api/v1/population",
        input_schema={
            "type": "object",
            "properties": {"area_name": {"type": "string"}},
        },
        parameter_mapping={
            "query.area_name": {"source": "arguments", "path": "area_name"},
        },
        result_kind="table",
        output_schema={
            "type": "object",
            "required": ["rows"],
            "properties": {
                "rows": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["area_code", "area_name", "population"],
                    },
                }
            },
        },
        created_by="test",
        updated_by="test",
    )
    await repo.save_tool(tool_v1)

    # Publish through full lifecycle
    await _publish_through_lifecycle(management, "test.dynamic.query", "1.0.0", "test")

    # Re-load dynamic tools after publish
    published = await load_published_tools(repo)
    manifests, descriptors = build_dynamic_tool_registry_entries(published)
    dynamic_input_schemas = {t.capability_id: t.input_schema for t in published}
    registry = ToolRegistry.default().merge_dynamic(
        manifests=manifests,
        descriptors=descriptors,
        dynamic_input_schemas=dynamic_input_schemas,
    )
    service = CapabilityService(
        registry=registry,
        policy=policy,
        adapter=adapter,
        dynamic_tool_adapter=adapter,
    )

    # Execute through CapabilityService
    auth = _sample_auth()
    result = await service.execute(
        tool_call_id="call-1",
        tool_id="test.dynamic.query",
        raw_arguments={"area_name": "西湖区"},
        auth_context=auth,
    )

    # Assert: upstream received the request
    assert len(_UpstreamHandler.received_requests) == 1, (
        f"Expected 1 request, got {len(_UpstreamHandler.received_requests)}"
    )
    req = _UpstreamHandler.received_requests[0]
    assert req["method"] == "GET"
    assert "/api/v1/population" in req["path"]

    # Assert: the upstream payload is preserved as a real table result rather
    # than being replaced by an empty generic placeholder.
    assert isinstance(result.data_result, models.TableDataResult)
    assert result.data_result.kind == "table"
    assert result.data_result.row_count == 2
    assert result.data_result.data.model_dump(mode="json")["rows"] == (
        _UpstreamHandler.response_payload["rows"]
    )


@pytest.mark.asyncio
async def test_version_isolation_across_runs(upstream_server: str) -> None:
    """Prove that old Run keeps old snapshot while new Run uses new version."""
    repo = InMemoryCapabilityRepository()
    management = CapabilityManagementService(repo)
    snapshot_service = RunCapabilitySnapshotService(repo)
    base_registry = ToolRegistry.default()

    connector = Connector(
        connector_id="version-test-connector",
        name="Version Test",
        base_url=upstream_server,
        allowed_path_prefixes=["/api"],
    )
    await repo.save_connector(connector)

    # Create and publish v1
    tool_v1 = ToolCapability(
        capability_id="version.test.tool",
        name="Version Test Tool",
        domain="governance",
        owner="test",
        version="1.0.0",
        status="draft",
        risk_level="low",
        required_permissions=[],
        dataset_ids=[],
        connector_ref="version-test-connector",
        http_method="GET",
        resource_path="/api/v1",
        input_schema={"type": "object", "properties": {}},
        result_kind="table",
        created_by="test",
        updated_by="test",
    )
    await repo.save_tool(tool_v1)
    await _publish_through_lifecycle(
        management, "version.test.tool", "1.0.0", "test"
    )

    # Create Run A snapshot
    snapshot_a = await snapshot_service.create_snapshot_for_run(
        run_id="run-version-A",
        base_registry=base_registry,
    )

    # Create and publish v2
    tool_v2 = ToolCapability(
        capability_id="version.test.tool",
        name="Version Test Tool",
        domain="governance",
        owner="test",
        version="2.0.0",
        status="draft",
        risk_level="low",
        required_permissions=[],
        dataset_ids=[],
        connector_ref="version-test-connector",
        http_method="POST",
        resource_path="/api/v2",
        input_schema={"type": "object", "properties": {}},
        result_kind="table",
        created_by="test",
        updated_by="test",
    )
    await repo.save_tool(tool_v2)
    await _publish_through_lifecycle(
        management, "version.test.tool", "2.0.0", "test"
    )

    # Create Run B snapshot
    snapshot_b = await snapshot_service.create_snapshot_for_run(
        run_id="run-version-B",
        base_registry=base_registry,
    )

    # Assert: version isolation
    version_a = snapshot_a.tool_versions.get("version.test.tool")
    version_b = snapshot_b.tool_versions.get("version.test.tool")

    assert version_a == "1.0.0", f"Run A should see v1.0.0, got {version_a}"
    assert version_b == "2.0.0", f"Run B should see v2.0.0, got {version_b}"
