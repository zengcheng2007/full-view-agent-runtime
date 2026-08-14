"""Vertical acceptance test for a dynamically configured HTTP Tool.

The test deliberately crosses both public HTTP surfaces: the control plane
creates and publishes the capability, then the agent API creates a new Run
that must discover, execute, persist, and expose that exact capability.
"""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import cast
from unittest.mock import AsyncMock

import httpx
import pytest

from full_view_agent.api.app import RuntimeContainer, create_app
from full_view_agent.application.answer_claims import StructuredFinish
from full_view_agent.application.dynamic_tool_adapter import HttpDynamicToolAdapter
from full_view_agent.domain.models import LegacyIdentitySnapshot, Principal
from full_view_agent.evaluation.contracts import EvalFinishStep, EvalToolCallStep
from full_view_agent.evaluation.scripted_provider import ScriptedModelProvider
from full_view_agent.infrastructure.capability_repository import (
    InMemoryCapabilityRepository,
)
from full_view_agent.infrastructure.credential_broker import InMemoryCredentialBroker
from full_view_agent.infrastructure.event_broker import InMemoryEventBroker
from full_view_agent.infrastructure.http_connector_executor import HttpConnectorExecutor


class _ControlledUpstream(BaseHTTPRequestHandler):
    requests: list[dict[str, object]] = []
    response_payload: dict[str, object] = {
        "rows": [
            {
                "area_code": "330106",
                "area_name": "西湖区",
                "person_count": 100_000,
            }
        ]
    }

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def do_GET(self) -> None:
        self.__class__.requests.append({"method": "GET", "path": self.path})
        encoded = json.dumps(self.response_payload, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


@pytest.fixture
def controlled_upstream() -> Iterator[tuple[str, type[_ControlledUpstream]]]:
    server = HTTPServer(("127.0.0.1", 0), _ControlledUpstream)
    _ControlledUpstream.requests = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", _ControlledUpstream
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _identity_port() -> AsyncMock:
    identity = LegacyIdentitySnapshot(
        principal=Principal(
            tenant_id="tenant-dynamic-e2e",
            user_id="admin-dynamic-e2e",
            org_id="org-admin",
            roles=["admin", "governance_analyst"],
        ),
        source="legacy_geo_user_fixture",
        source_session_expires_at=datetime.now(UTC) + timedelta(hours=1),
        base_area_codes=["330106"],
    )
    port = AsyncMock()
    port.resolve = AsyncMock(return_value=identity)
    return port


def _run_body() -> dict[str, object]:
    return {
        "input": {
            "client_message_id": "message-dynamic-tool-e2e",
            "content": [{"type": "text", "text": "查询西湖区人口"}],
        },
        "client": {
            "client_instance_id": "client-dynamic-tool-e2e",
            "frontend_command_schema_versions": ["1.1"],
            "supported_commands": ["panel.show_table"],
        },
        "mode": "agent",
    }


@pytest.mark.asyncio
async def test_published_bound_dynamic_tool_executes_in_new_run_with_evidence(
    controlled_upstream: tuple[str, type[_ControlledUpstream]],
) -> None:
    upstream_url, upstream = controlled_upstream

    repository = InMemoryCapabilityRepository()
    dynamic_adapter = HttpDynamicToolAdapter(
        repository=repository,
        http_executor=HttpConnectorExecutor(
            repository,
            allowed_private_hosts=frozenset({"127.0.0.1"}),
        ),
    )
    provider = ScriptedModelProvider(
        [
            EvalToolCallStep(
                type="tool_call",
                tool_id="governance.dynamic_population",
                arguments={"area_code": "330106"},
            ),
            EvalFinishStep(
                type="finish",
                content="查询完成。",
                structured_finish=StructuredFinish.model_validate(
                    {
                        "kind": "reference_only",
                        "summary": "查询结果已生成。",
                    }
                ),
            ),
        ]
    )
    runtime = RuntimeContainer(
        identity_port=_identity_port(),
        credentials=InMemoryCredentialBroker(),
        capability_repository=repository,
        dynamic_tool_adapter=dynamic_adapter,
        model_provider=provider,
        connector_allowed_private_hosts=frozenset({"127.0.0.1"}),
    )
    app = create_app(runtime)
    headers = {"geoToken": "dynamic-e2e-token"}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        connector = await client.post(
            "/capability-api/v1/connectors",
            headers=headers,
            json={
                "connector_id": "connector.dynamic_population",
                "name": "受控人口服务",
                "base_url": upstream_url,
                "allowed_path_prefixes": ["/v1"],
            },
        )
        assert connector.status_code == 201, connector.text

        created = await client.post(
            "/capability-api/v1/tools",
            headers=headers,
            json={
                "capability_id": "governance.dynamic_population",
                "name": "动态人口查询",
                "owner": "test",
                "version": "1.0.0",
                "connector_ref": "connector.dynamic_population",
                "resource_path": "/v1/population",
                "http_method": "GET",
                "input_schema": {
                    "type": "object",
                    "properties": {"area_code": {"type": "string"}},
                    "required": ["area_code"],
                    "additionalProperties": False,
                },
                "output_schema": {
                    "type": "object",
                    "properties": {
                        "rows": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "area_code": {"type": "string"},
                                    "area_name": {"type": "string"},
                                    "person_count": {"type": "integer"},
                                },
                                "required": [
                                    "area_code",
                                    "area_name",
                                    "person_count",
                                ],
                            },
                        }
                    },
                    "required": ["rows"],
                },
                "parameter_mapping": {"area_code": "$.area_code"},
                "result_mapping": {"rows": "$.rows"},
                "result_kind": "table",
                "data_schema_ref": "schema://population-metric-table/1.0",
                "required_permissions": ["governance.population.aggregate.read"],
                "dataset_ids": ["population"],
            },
        )
        assert created.status_code == 201, created.text
        etag = created.json()["data"]["etag"]

        testing = await client.post(
            "/capability-api/v1/tools/governance.dynamic_population/1.0.0/testing",
            headers=headers,
            json={"reason": "contract test", "expected_etag": etag},
        )
        assert testing.status_code == 200, testing.text
        etag = testing.json()["data"]["etag"]

        approved = await client.post(
            "/capability-api/v1/tools/governance.dynamic_population/1.0.0/approve",
            headers=headers,
            json={"reason": "reviewed", "expected_etag": etag},
        )
        assert approved.status_code == 200, approved.text
        etag = approved.json()["data"]["etag"]

        published = await client.post(
            "/capability-api/v1/tools/governance.dynamic_population/1.0.0/publish",
            headers=headers,
            json={"reason": "approved for e2e", "expected_etag": etag},
        )
        assert published.status_code == 200, published.text

        binding = await client.post(
            "/capability-api/v1/applications/full_information_view/capabilities",
            headers=headers,
            json={
                "capability_id": "governance.dynamic_population",
                "capability_version": "1.0.0",
                "reason": "enable in full view",
            },
        )
        assert binding.status_code == 201, binding.text
        binding_etag = binding.json()["data"]["etag"]
        enabled = await client.post(
            "/capability-api/v1/applications/full_information_view/capabilities/"
            "governance.dynamic_population/1.0.0/enable",
            headers=headers,
            json={"reason": "e2e enabled", "expected_etag": binding_etag},
        )
        assert enabled.status_code == 200, enabled.text

        session = await client.post(
            "/agent-api/v1/sessions",
            headers={**headers, "Idempotency-Key": "dynamic-e2e-session"},
            json={"title": "动态能力验收"},
        )
        assert session.status_code == 201, session.text
        session_id = session.json()["data"]["session_id"]
        run_response = await client.post(
            f"/agent-api/v1/sessions/{session_id}/runs",
            headers={**headers, "Idempotency-Key": "dynamic-e2e-run"},
            json=_run_body(),
        )
        assert run_response.status_code == 202, run_response.text
        run_id = run_response.json()["data"]["run_id"]

        for _ in range(200):
            run = await client.get(f"/agent-api/v1/runs/{run_id}", headers=headers)
            if run.json()["data"]["status"] in {"completed", "failed", "cancelled"}:
                break
            await asyncio.sleep(0.005)
        event_broker = cast(InMemoryEventBroker, runtime.events)
        terminal_events = await event_broker.list_events(run_id=run_id)
        advertised_tool_ids = [
            tool.tool_id for request in provider.requests for tool in request.tools
        ]
        assert run.json()["data"]["status"] == "completed", {
            "run": run.json()["data"],
            "events": [
                {"type": event.type, "data": event.data} for event in terminal_events
            ],
            "advertised_tool_ids": advertised_tool_ids,
            "consumed_steps": [
                step.model_dump(mode="json") for step in provider.consumed_steps
            ],
        }

        events = terminal_events
        event_types = [event.type for event in events]
        for event_type in (
            "run.started",
            "tool.started",
            "tool.completed",
            "result.available",
            "evidence.available",
            "run.completed",
        ):
            assert event_type in event_types
        result_id = next(
            event.data["result_id"]
            for event in events
            if event.type == "result.available"
        )
        evidence_id = next(
            event.data["evidence_id"]
            for event in events
            if event.type == "evidence.available"
        )

        result = await client.get(
            f"/agent-api/v1/results/{result_id}", headers=headers
        )
        evidence = await client.get(
            f"/agent-api/v1/evidence/{evidence_id}", headers=headers
        )
        lifecycle = await client.get(
            "/capability-api/v1/tools/governance.dynamic_population/lifecycle",
            headers=headers,
        )

    assert upstream.requests == [
        {"method": "GET", "path": "/v1/population?area_code=330106"}
    ]
    assert result.status_code == 200, result.text
    assert result.json()["data"]["kind"] == "table"
    assert result.json()["data"]["data"]["rows"] == [
        {
            "area_code": "330106",
            "area_name": "西湖区",
            "person_count": 100_000,
        }
    ]
    assert evidence.status_code == 200, evidence.text
    assert evidence.json()["data"]["tool"] == {
        "tool_id": "governance.dynamic_population",
        "tool_version": "1.0.0",
    }
    assert evidence.json()["data"]["dataset_id"] == "population"
    assert lifecycle.status_code == 200, lifecycle.text
    assert [item["to_status"] for item in lifecycle.json()["data"]] == [
        "testing",
        "pending_approval",
        "published",
    ]
    assert provider.requests
    assert any(
        tool.tool_id == "governance.dynamic_population"
        for tool in provider.requests[0].tools
    )
