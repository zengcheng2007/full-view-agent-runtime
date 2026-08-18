"""P2 Browser/API smoke tests for capability center (G5).

Tests admin vs user permissions, Connector/Tool CRUD, publishing, and model config
enablement via FastAPI TestClient (which simulates real HTTP requests with real routing).

Also includes a Playwright test that verifies the capability center frontend pages load.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from full_view_agent.api.app import RuntimeContainer, create_app
from full_view_agent.application.model_config_service import (
    InMemoryModelConfigKeyStore,
)
from full_view_agent.infrastructure.capability_repository import (
    InMemoryCapabilityRepository,
    InMemoryModelConfigRepository,
)
from full_view_agent.infrastructure.credential_broker import InMemoryCredentialBroker


@pytest.fixture
async def admin_client():
    """Test client with admin user (role: admin)."""
    from unittest.mock import AsyncMock

    from full_view_agent.domain.models import LegacyIdentitySnapshot, Principal

    runtime = RuntimeContainer(
        credentials=InMemoryCredentialBroker(),
    )
    runtime.capability_repository = InMemoryCapabilityRepository()
    model_config_repo = InMemoryModelConfigRepository()
    model_config_key_store = InMemoryModelConfigKeyStore()
    runtime.model_config_service = __import__(
        "full_view_agent.application.model_config_service",
        fromlist=["ModelConfigService"],
    ).ModelConfigService(
        repository=model_config_repo,
        key_store=model_config_key_store,
    )
    runtime.capability_management_service = __import__(
        "full_view_agent.application.capability_management_service",
        fromlist=["CapabilityManagementService"],
    ).CapabilityManagementService(
        repository=runtime.capability_repository,
    )

    # Mock identity port to return admin user
    admin_identity = LegacyIdentitySnapshot(
        principal=Principal(
            tenant_id="tenant_test",
            user_id="admin_user",
            org_id="org_admin",
            roles=["admin"],  # Admin role
        ),
        source="test",
        source_session_expires_at=__import__("datetime").datetime.now(
            __import__("datetime").timezone.utc
        ) + __import__("datetime").timedelta(hours=1),
        base_area_codes=["330106"],
    )

    mock_identity_port = AsyncMock()
    mock_identity_port.resolve = AsyncMock(return_value=admin_identity)
    runtime.identity_port = mock_identity_port

    app = create_app(runtime=runtime)
    transport = ASGITransport(app=app)

    # Pass geoToken header so auth middleware processes the request
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"geoToken": "admin-test-token"},
    ) as client:
        yield client


@pytest.fixture
async def user_client():
    """Test client with regular user (role: governance_analyst)."""
    from unittest.mock import AsyncMock

    from full_view_agent.domain.models import LegacyIdentitySnapshot, Principal

    runtime = RuntimeContainer(
        credentials=InMemoryCredentialBroker(),
    )
    runtime.capability_repository = InMemoryCapabilityRepository()
    model_config_repo = InMemoryModelConfigRepository()
    model_config_key_store = InMemoryModelConfigKeyStore()
    runtime.model_config_service = __import__(
        "full_view_agent.application.model_config_service",
        fromlist=["ModelConfigService"],
    ).ModelConfigService(
        repository=model_config_repo,
        key_store=model_config_key_store,
    )
    runtime.capability_management_service = __import__(
        "full_view_agent.application.capability_management_service",
        fromlist=["CapabilityManagementService"],
    ).CapabilityManagementService(
        repository=runtime.capability_repository,
    )

    # Mock identity port to return regular user
    user_identity = LegacyIdentitySnapshot(
        principal=Principal(
            tenant_id="tenant_test",
            user_id="regular_user",
            org_id="org_user",
            roles=["governance_analyst"],  # Regular user role
        ),
        source="test",
        source_session_expires_at=__import__("datetime").datetime.now(
            __import__("datetime").timezone.utc
        ) + __import__("datetime").timedelta(hours=1),
        base_area_codes=["330106"],
    )

    mock_identity_port = AsyncMock()
    mock_identity_port.resolve = AsyncMock(return_value=user_identity)
    runtime.identity_port = mock_identity_port

    app = create_app(runtime=runtime)
    transport = ASGITransport(app=app)

    # Pass geoToken header so auth middleware processes the request
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"geoToken": "user-test-token"},
    ) as client:
        yield client


class TestCapabilityCenterPermissions:
    """Test G5: Admin vs user permissions."""

    @pytest.mark.asyncio
    async def test_admin_can_list_connectors(self, admin_client):
        """Admin can list connectors (read operation)."""
        response = await admin_client.get("/capability-api/v1/connectors")
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_independent_console_can_authenticate_with_bearer_token(self):
        from unittest.mock import AsyncMock

        from full_view_agent.domain.models import LegacyIdentitySnapshot, Principal

        runtime = RuntimeContainer(credentials=InMemoryCredentialBroker())
        identity = LegacyIdentitySnapshot(
            principal=Principal(
                tenant_id="platform",
                user_id="capability-admin",
                org_id="platform-admins",
                roles=["admin"],
            ),
            source="capability-console",
            source_session_expires_at=__import__("datetime").datetime.now(
                __import__("datetime").timezone.utc
            )
            + __import__("datetime").timedelta(hours=1),
            base_area_codes=[],
        )
        runtime.capability_identity_port = AsyncMock()
        runtime.capability_identity_port.resolve = AsyncMock(return_value=identity)
        app = create_app(runtime=runtime)

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": "Bearer capability-console-token"},
        ) as client:
            response = await client.get("/capability-api/v1/connectors")

        assert response.status_code == 200
        runtime.capability_identity_port.resolve.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_admin_can_list_registered_applications(self, admin_client):
        response = await admin_client.get("/capability-api/v1/applications")

        assert response.status_code == 200
        payload = response.json()
        assert [item["app_id"] for item in payload["data"]] == [
            "full_information_view",
            "unified_address",
        ]

    @pytest.mark.asyncio
    async def test_regular_user_cannot_list_platform_applications(self, user_client):
        response = await user_client.get("/capability-api/v1/applications")

        assert response.status_code == 403

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "path",
        [
            "/capability-api/v1/tools",
            "/capability-api/v1/skills",
            "/capability-api/v1/workflows",
            "/capability-api/v1/connectors",
            "/capability-api/v1/runtime/tools",
        ],
    )
    async def test_regular_business_user_cannot_read_control_plane_metadata(
        self, user_client, path
    ):
        response = await user_client.get(path)

        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_admin_can_create_connector(self, admin_client, monkeypatch):
        """Admin can create connectors (write operation)."""
        # Mock DNS resolution to avoid real network calls
        import socket
        original_getaddrinfo = socket.getaddrinfo

        def mock_getaddrinfo(host, *args, **kwargs):
            if host == "api.example.com":
                # Return a public IP
                return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("93.184.216.34", 0))]
            return original_getaddrinfo(host, *args, **kwargs)

        monkeypatch.setattr(socket, "getaddrinfo", mock_getaddrinfo)

        response = await admin_client.post(
            "/capability-api/v1/connectors",
            json={
                "connector_id": "test.api",
                "name": "Test API",
                "base_url": "https://api.example.com",
                "description": "Test connector",
                "allowed_path_prefixes": ["/api/v1"],
            },
        )
        assert response.status_code in (200, 201)

    @pytest.mark.asyncio
    async def test_user_cannot_create_connector(self, user_client):
        """Regular user cannot create connectors."""
        response = await user_client.post(
            "/capability-api/v1/connectors",
            json={
                "connector_id": "test.api",
                "name": "Test API",
                "base_url": "https://api.example.com",
                "description": "Test connector",
                "allowed_path_prefixes": ["/api/v1"],
            },
        )
        # Should be forbidden
        assert response.status_code in (401, 403)

    @pytest.mark.asyncio
    async def test_business_user_cannot_read_connector_topology(self, user_client):
        """Connector endpoints and credential refs belong to the control plane."""
        response = await user_client.get("/capability-api/v1/connectors")
        assert response.status_code in (401, 403)

    @pytest.mark.asyncio
    async def test_admin_can_create_tool(self, admin_client, monkeypatch):
        """Admin can create tools."""
        # Mock DNS resolution
        import socket
        original_getaddrinfo = socket.getaddrinfo

        def mock_getaddrinfo(host, *args, **kwargs):
            if host == "api.example.com":
                return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("93.184.216.34", 0))]
            return original_getaddrinfo(host, *args, **kwargs)

        monkeypatch.setattr(socket, "getaddrinfo", mock_getaddrinfo)

        # First create a connector
        await admin_client.post(
            "/capability-api/v1/connectors",
            json={
                "connector_id": "test.api",
                "name": "Test API",
                "base_url": "https://api.example.com",
                "allowed_path_prefixes": ["/api/v1"],
            },
        )

        # Then create a tool
        response = await admin_client.post(
            "/capability-api/v1/tools",
            json={
                "capability_id": "tool.test_query",
                "name": "Test Query",
                "owner": "test-team",
                "version": "1.0.0",
                "connector_ref": "test.api",
                "resource_path": "/api/v1/query",
                "http_method": "GET",
                "required_permissions": ["test.read"],
                "dataset_ids": ["test_dataset"],
            },
        )
        assert response.status_code in (200, 201)

    @pytest.mark.asyncio
    async def test_user_cannot_create_tool(self, user_client):
        """Regular user cannot create tools."""
        response = await user_client.post(
            "/capability-api/v1/tools",
            json={
                "capability_id": "tool.test_query",
                "name": "Test Query",
                "owner": "test-team",
                "version": "1.0.0",
                "connector_ref": "test.api",
                "resource_path": "/api/v1/query",
                "http_method": "GET",
                "dataset_ids": ["test_dataset"],
            },
        )
        assert response.status_code in (401, 403)

    @pytest.mark.asyncio
    async def test_admin_can_manage_model_configs(self, admin_client):
        """Admin can create and manage model configs."""
        response = await admin_client.post(
            "/capability-api/v1/model-configs",
            json={
                "name": "Test Config",
                "api_base_url": "https://api.example.com/v1",
                "api_key": "test-key-12345",
                "model_name": "gpt-4",
            },
        )
        assert response.status_code in (200, 201)

    @pytest.mark.asyncio
    async def test_admin_can_see_environment_model_as_pending_import_candidate(
        self, admin_client, monkeypatch
    ):
        monkeypatch.setenv("FULL_VIEW_MODEL_PROVIDER", "openai_compatible")
        monkeypatch.setenv("FULL_VIEW_MODEL_BASE_URL", "https://model.example/v1")
        monkeypatch.setenv("FULL_VIEW_MODEL_NAME", "deepseek-v4-flash-0731")
        monkeypatch.setenv("FULL_VIEW_MODEL_API_KEY", "server-only-secret")
        monkeypatch.setenv("FULL_VIEW_MODEL_TIMEOUT_SECONDS", "60")
        monkeypatch.setenv("FULL_VIEW_MODEL_MAX_OUTPUT_TOKENS", "131072")
        monkeypatch.setenv("FULL_VIEW_MODEL_MAX_RETRIES", "1")

        response = await admin_client.get(
            "/capability-api/v1/model-configs/environment-candidate"
        )

        assert response.status_code == 200
        assert response.json()["data"] == {
            "status": "pending_import",
            "provider_type": "openai_compatible",
            "api_base_url": "https://model.example/v1",
            "model_name": "deepseek-v4-flash-0731",
            "credential_configured": True,
            "reasoning_capability": {
                "mode": "unsupported",
                "fast_profile": None,
                "deep_profile": None,
            },
        }

    @pytest.mark.asyncio
    async def test_admin_can_import_environment_model_without_key_roundtrip(
        self, admin_client, monkeypatch
    ):
        monkeypatch.setenv("FULL_VIEW_MODEL_PROVIDER", "openai_compatible")
        monkeypatch.setenv("FULL_VIEW_MODEL_BASE_URL", "https://model.example/v1")
        monkeypatch.setenv("FULL_VIEW_MODEL_NAME", "deepseek-v4-flash-0731")
        monkeypatch.setenv("FULL_VIEW_MODEL_API_KEY", "server-only-secret")
        monkeypatch.setenv("FULL_VIEW_MODEL_MAX_OUTPUT_TOKENS", "131072")

        response = await admin_client.post(
            "/capability-api/v1/model-configs/import-effective",
            json={"name": "当前环境模型", "notes": "测试纳管"},
        )

        assert response.status_code == 201
        data = response.json()["data"]
        assert data["is_enabled"] is False
        assert data["lifecycle"] == "draft"
        assert data["model_name"] == "deepseek-v4-flash-0731"
        assert data["max_output_tokens"] == 128000
        assert "server-only-secret" not in response.text
        effective = await admin_client.get(
            "/capability-api/v1/model-configs/effective"
        )
        assert effective.json()["data"]["source"] == "none"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("invalid_value", ["not-an-integer", "0", "-1"])
    async def test_import_environment_model_rejects_invalid_token_limit_cleanly(
        self, admin_client, monkeypatch, invalid_value
    ):
        monkeypatch.setenv("FULL_VIEW_MODEL_PROVIDER", "openai_compatible")
        monkeypatch.setenv("FULL_VIEW_MODEL_BASE_URL", "https://model.example/v1")
        monkeypatch.setenv("FULL_VIEW_MODEL_NAME", "deepseek-v4-flash-0731")
        monkeypatch.setenv("FULL_VIEW_MODEL_API_KEY", "server-only-secret")
        monkeypatch.setenv("FULL_VIEW_MODEL_MAX_OUTPUT_TOKENS", invalid_value)

        response = await admin_client.post(
            "/capability-api/v1/model-configs/import-effective",
            json={"name": "invalid environment model"},
        )

        assert response.status_code == 409, response.text
        assert "FULL_VIEW_MODEL_MAX_OUTPUT_TOKENS" in response.text
        assert "server-only-secret" not in response.text

    @pytest.mark.asyncio
    async def test_user_cannot_manage_model_configs(self, user_client):
        """Regular user cannot manage model configs."""
        response = await user_client.post(
            "/capability-api/v1/model-configs",
            json={
                "name": "Test Config",
                "api_base_url": "https://api.example.com/v1",
                "api_key": "test-key-12345",
                "model_name": "gpt-4",
            },
        )
        assert response.status_code in (401, 403)

    @pytest.mark.asyncio
    async def test_admin_cannot_publish_untested_model_via_legacy_enable(
        self, admin_client
    ):
        """Deprecated enable must enforce the same persisted test gate."""
        # Create a config first
        create_resp = await admin_client.post(
            "/capability-api/v1/model-configs",
            json={
                "name": "Test Config",
                "api_base_url": "https://api.example.com/v1",
                "api_key": "test-key-12345",
                "model_name": "gpt-4",
            },
        )
        assert create_resp.status_code in (200, 201)
        config_id = create_resp.json()["data"]["config_id"]

        # Enable it
        response = await admin_client.post(
            f"/capability-api/v1/model-configs/{config_id}/enable"
        )
        assert response.status_code == 409
        assert "required model tests" in response.text
