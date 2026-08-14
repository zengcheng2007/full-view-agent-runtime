from __future__ import annotations

import asyncio
import base64
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import httpx
import pytest

from full_view_agent.api.app import RuntimeContainer, create_app
from full_view_agent.application.answer_claims import StructuredFinish
from full_view_agent.domain.models import LegacyIdentitySnapshot, Principal
from full_view_agent.evaluation.contracts import EvalFinishStep, EvalToolCallStep
from full_view_agent.evaluation.scripted_provider import ScriptedModelProvider
from full_view_agent.infrastructure.credential_broker import InMemoryCredentialBroker
from full_view_agent.infrastructure.event_broker import InMemoryEventBroker


def _identity_port() -> AsyncMock:
    identity = LegacyIdentitySnapshot(
        principal=Principal(
            tenant_id="tenant-control-e2e",
            user_id="admin-control-e2e",
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
            "client_message_id": "message-knowledge-control-e2e",
            "content": [{"type": "text", "text": "西湖区治理口径是什么？"}],
        },
        "client": {
            "client_instance_id": "client-knowledge-control-e2e",
            "frontend_command_schema_versions": ["1.1"],
            "supported_commands": ["panel.show_table"],
        },
        "mode": "agent",
    }


@pytest.mark.asyncio
async def test_control_plane_published_knowledge_is_used_by_new_run_with_citation() -> None:
    """Control-plane content must cross the real agent API and become evidence."""

    identity_port = _identity_port()
    provider = ScriptedModelProvider(
        [
            EvalToolCallStep(
                type="tool_call",
                tool_id="knowledge.search",
                arguments={"query": "西湖区治理口径", "limit": 5},
            ),
            EvalFinishStep(
                type="finish",
                content="已依据知识库回答。",
                structured_finish=StructuredFinish.model_validate(
                    {"kind": "reference_only", "summary": "已依据知识库回答。"}
                ),
            ),
        ]
    )
    runtime = RuntimeContainer(
        identity_port=identity_port,
        credentials=InMemoryCredentialBroker(),
        model_provider=provider,
    )
    runtime.capability_identity_port = identity_port
    app = create_app(runtime)
    headers = {"geoToken": "control-e2e-token"}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        created = await client.post(
            "/capability-api/v1/knowledge-bases",
            headers=headers,
            json={
                "app_id": "full_information_view",
                "knowledge_base_id": "kb.control-e2e",
                "name": "治理口径库",
            },
        )
        assert created.status_code == 201, created.text
        imported = await client.post(
            "/capability-api/v1/knowledge-bases/kb.control-e2e/documents/import",
            params={"app_id": "full_information_view"},
            headers=headers,
            json={
                "data_source_id": "upload.control-e2e",
                "document_id": "doc.control-e2e",
                "filename": "governance.md",
                "media_type": "text/markdown",
                "content_base64": base64.b64encode(
                    "西湖区治理口径以正式发布版本为准。".encode()
                ).decode(),
            },
        )
        assert imported.status_code == 201, imported.text
        published = await client.post(
            "/capability-api/v1/knowledge-bases/kb.control-e2e/publish",
            params={"app_id": "full_information_view"},
            headers=headers,
            json={"reason": "纵向验收发布"},
        )
        assert published.status_code == 200, published.text

        session = await client.post(
            "/agent-api/v1/sessions",
            headers={**headers, "Idempotency-Key": "control-e2e-session"},
            json={"title": "控制面纵向验收"},
        )
        assert session.status_code == 201, session.text
        run_response = await client.post(
            f"/agent-api/v1/sessions/{session.json()['data']['session_id']}/runs",
            headers={**headers, "Idempotency-Key": "control-e2e-run"},
            json=_run_body(),
        )
        assert run_response.status_code == 202, run_response.text
        run_id = run_response.json()["data"]["run_id"]

        for _ in range(200):
            run = await client.get(f"/agent-api/v1/runs/{run_id}", headers=headers)
            if run.json()["data"]["status"] in {"completed", "failed", "cancelled"}:
                break
            await asyncio.sleep(0.005)

        events = await runtime.events.list_events(run_id=run_id)  # type: ignore[union-attr]
        assert run.json()["data"]["status"] == "completed", [
            {"type": event.type, "data": event.data} for event in events
        ]
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
        result = await client.get(f"/agent-api/v1/results/{result_id}", headers=headers)
        evidence = await client.get(
            f"/agent-api/v1/evidence/{evidence_id}", headers=headers
        )
        audits = await client.get(
            "/capability-api/v1/knowledge-bases/kb.control-e2e/audit-events",
            params={"app_id": "full_information_view"},
            headers=headers,
        )

    row = result.json()["data"]["data"]["rows"][0]
    assert row["document_id"] == "doc.control-e2e"
    assert row["knowledge_base_version"] == 1
    assert row["paragraph_start"] == 1
    assert evidence.json()["data"]["tool"] == {
        "tool_id": "knowledge.search",
        "tool_version": "1.0.0",
    }
    assert "knowledge_base.published" in {
        item["action"] for item in audits.json()["data"]
    }
    assert isinstance(runtime.events, InMemoryEventBroker)
