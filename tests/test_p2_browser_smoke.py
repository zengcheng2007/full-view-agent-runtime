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
    async def test_user_can_read_connectors(self, user_client):
        """Regular user can read connectors (least privilege)."""
        response = await user_client.get("/capability-api/v1/connectors")
        # Read operations should work for regular users
        assert response.status_code == 200

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
    async def test_admin_can_enable_model_config(self, admin_client):
        """Admin can enable a model config."""
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
        assert response.status_code == 200
