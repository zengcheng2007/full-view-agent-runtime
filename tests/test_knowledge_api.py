from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import httpx
import pytest

from full_view_agent.api.app import RuntimeContainer, create_app
from full_view_agent.domain.models import LegacyIdentitySnapshot, Principal
from full_view_agent.infrastructure.credential_broker import InMemoryCredentialBroker


def _runtime() -> RuntimeContainer:
    runtime = RuntimeContainer(credentials=InMemoryCredentialBroker())
    runtime.capability_identity_port = AsyncMock()
    runtime.capability_identity_port.resolve = AsyncMock(
        return_value=LegacyIdentitySnapshot(
            principal=Principal(
                tenant_id="tenant-a",
                user_id="knowledge-admin",
                org_id="platform-admins",
                roles=["admin"],
            ),
            source="knowledge-api-test",
            source_session_expires_at=datetime.now(UTC) + timedelta(minutes=5),
            base_area_codes=[],
        )
    )
    return runtime


@pytest.mark.asyncio
async def test_knowledge_http_full_lifecycle_and_citations() -> None:
    app = create_app(_runtime())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"geoToken": "admin-token", "X-Tenant-Id": "forged-tenant"},
    ) as client:
        created = await client.post(
            "/capability-api/v1/knowledge-bases",
            json={
                "app_id": "full_information_view",
                "knowledge_base_id": "kb.policy",
                "name": "治理政策库",
                "description": "政策和口径说明",
            },
        )
        assert created.status_code == 201

        imported = await client.post(
            "/capability-api/v1/knowledge-bases/kb.policy/documents/import",
            params={"app_id": "full_information_view"},
            json={
                "data_source_id": "upload.policy",
                "document_id": "doc.policy",
                "filename": "policy.md",
                "media_type": "text/markdown",
                "content_base64": base64.b64encode(
                    "西湖区治理口径以正式发布版本为准。".encode()
                ).decode(),
            },
        )
        assert imported.status_code == 201

        published = await client.post(
            "/capability-api/v1/knowledge-bases/kb.policy/publish",
            params={"app_id": "full_information_view"},
            json={"reason": "发布首版"},
        )
        assert published.status_code == 200
        assert published.json()["data"]["version"] == 1

        search = await client.post(
            "/capability-api/v1/knowledge-bases/search-test",
            params={"app_id": "full_information_view"},
            json={
                "knowledge_base_ids": ["kb.policy"],
                "query": "西湖区治理口径",
                "limit": 5,
            },
        )
        assert search.status_code == 200
        citation = search.json()["data"][0]["citation"]
        assert citation["document_id"] == "doc.policy"
        assert citation["knowledge_base_version"] == 1
        assert citation["paragraph_start"] == 1

        policy = await client.patch(
            "/capability-api/v1/knowledge-bases/kb.policy/access-policy",
            params={"app_id": "full_information_view"},
            json={
                "public_within_app": False,
                "allowed_user_ids": ["knowledge-admin"],
                "allowed_roles": ["auditor"],
            },
        )
        assert policy.status_code == 200

        audits = await client.get(
            "/capability-api/v1/knowledge-bases/kb.policy/audit-events",
            params={"app_id": "full_information_view"},
        )
        assert audits.status_code == 200
        assert {item["action"] for item in audits.json()["data"]} >= {
            "knowledge_base.created",
            "document.imported",
            "knowledge_base.published",
            "access_policy.updated",
        }


@pytest.mark.asyncio
async def test_knowledge_scope_is_derived_from_authenticated_tenant() -> None:
    app = create_app(_runtime())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"geoToken": "admin-token"},
    ) as client:
        created = await client.post(
            "/capability-api/v1/knowledge-bases",
            json={
                "app_id": "full_information_view",
                "knowledge_base_id": "kb.scope",
                "name": "隔离测试",
            },
        )

    assert created.status_code == 201
    assert created.json()["data"]["tenant_id"] == "tenant-a"
