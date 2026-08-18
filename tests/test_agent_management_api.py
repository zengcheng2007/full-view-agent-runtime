from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import httpx
import pytest

from full_view_agent.api.app import RuntimeContainer, create_app
from full_view_agent.domain.models import LegacyIdentitySnapshot, Principal
from tests.model_resource_helpers import publish_tested_model


def _identity_port() -> AsyncMock:
    port = AsyncMock()
    port.resolve = AsyncMock(
        return_value=LegacyIdentitySnapshot(
            principal=Principal(
                tenant_id="platform",
                user_id="agent-admin",
                org_id="platform-admins",
                roles=["admin"],
            ),
            source="agent-management-test",
            source_session_expires_at=datetime.now(UTC) + timedelta(hours=1),
            base_area_codes=[],
        )
    )
    return port


@pytest.mark.asyncio
async def test_agent_control_plane_vertical_api() -> None:
    identity_port = _identity_port()
    runtime = RuntimeContainer(identity_port=identity_port)
    runtime.capability_identity_port = identity_port
    await runtime.initialize()
    assert runtime.model_config_service is not None
    model = await publish_tested_model(
        runtime.model_config_service,
        name="公共模型 A",
        model_name="model-a",
    )
    app = create_app(runtime)
    headers = {"geoToken": "admin-token"}
    base = "/capability-api/v1/applications/full_information_view"

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        created = await client.post(
            f"{base}/agents",
            headers=headers,
            json={
                "agent_id": "regional_analysis_agent",
                "name": "区域研判智能体",
            },
        )
        assert created.status_code == 201, created.text
        detail = await client.get(
            f"{base}/agents/regional_analysis_agent", headers=headers
        )
        assert detail.status_code == 200
        assert detail.json()["data"]["name"] == "区域研判智能体"
        listed = await client.get(f"{base}/agents", headers=headers)
        assert {item["agent_id"] for item in listed.json()["data"]} == {
            "governance_general_agent",
            "regional_analysis_agent",
        }

        version = await client.post(
            f"{base}/agents/regional_analysis_agent/versions",
            headers=headers,
            json={"version": "1.0.0"},
        )
        assert version.status_code == 201, version.text
        policy = await client.put(
            f"{base}/agents/regional_analysis_agent/versions/1.0.0/model-policy",
            headers=headers,
            json={
                "primary_model_config_id": model.config_id,
                "fallback_model_config_ids": [],
            },
        )
        assert policy.status_code == 200, policy.text
        validation = await client.post(
            f"{base}/agents/regional_analysis_agent/versions/1.0.0/validate",
            headers=headers,
        )
        assert validation.json()["data"]["is_valid"] is True
        published = await client.post(
            f"{base}/agents/regional_analysis_agent/versions/1.0.0/publish",
            headers=headers,
            json={"reason": "验收通过"},
        )
        assert published.status_code == 200, published.text
        active = await client.get(
            f"{base}/agents/regional_analysis_agent/releases/active",
            headers=headers,
        )

    assert active.status_code == 200
    assert active.json()["data"]["agent_version"] == "1.0.0"
    assert active.json()["data"]["model_refs"][0]["model_config_id"] == model.config_id
