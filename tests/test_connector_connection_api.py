from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import httpx
import pytest

import full_view_agent.infrastructure.http_connector_executor as connector_executor_module
from full_view_agent.api.app import RuntimeContainer, create_app
from full_view_agent.domain.capability import Connector
from full_view_agent.domain.models import LegacyIdentitySnapshot, Principal
from full_view_agent.infrastructure.capability_repository import (
    InMemoryCapabilityRepository,
)
from full_view_agent.infrastructure.credential_broker import InMemoryCredentialBroker


def test_private_host_allowlist_parser_accepts_only_exact_host_tokens() -> None:
    parser = getattr(
        connector_executor_module,
        "parse_connector_allowed_private_hosts",
        None,
    )

    assert parser is not None
    assert parser(" 127.0.0.1, GOVERNANCE.INTERNAL. , ,bad/path ") == frozenset(
        {"127.0.0.1", "governance.internal"}
    )


def _admin_runtime(
    repository: InMemoryCapabilityRepository,
    transport: httpx.AsyncBaseTransport,
    *,
    roles: list[str] | None = None,
    allowed_private_hosts: frozenset[str] = frozenset(),
) -> RuntimeContainer:
    runtime = RuntimeContainer(
        credentials=InMemoryCredentialBroker(),
        capability_repository=repository,
    )
    identity = LegacyIdentitySnapshot(
        principal=Principal(
            tenant_id="platform",
            user_id="control-admin",
            org_id="platform-admins",
            roles=roles or ["admin"],
        ),
        source="connector-test",
        source_session_expires_at=datetime.now(UTC) + timedelta(minutes=5),
        base_area_codes=[],
    )
    runtime.capability_identity_port = AsyncMock()
    runtime.capability_identity_port.resolve = AsyncMock(return_value=identity)
    runtime.connector_connection_transport = transport
    runtime.connector_allowed_private_hosts = allowed_private_hosts
    return runtime


async def _seed(
    repository: InMemoryCapabilityRepository,
    *,
    base_url: str = "https://93.184.216.34/health",
    credential_ref: str | None = "vault.connector.governance",
    is_active: bool = True,
    denied_hosts: list[str] | None = None,
) -> None:
    await repository.save_connector(
        Connector(
            connector_id="connector.governance",
            name="治理网关",
            base_url=base_url,
            allowed_path_prefixes=["/api/"],
            denied_hosts=denied_hosts or [],
            is_active=is_active,
            credential_ref=credential_ref,
            timeout_ms=1500,
        )
    )


async def _post_test(runtime: RuntimeContainer, *, token: bool = True) -> httpx.Response:
    app = create_app(runtime)
    headers = {"geoToken": "legacy-admin-token"} if token else {}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers=headers,
    ) as client:
        return await client.post(
            "/capability-api/v1/connectors/connector.governance/test-connection"
        )


@pytest.mark.asyncio
async def test_connector_probe_calls_real_target_without_sending_credential_reference() -> None:
    repository = InMemoryCapabilityRepository()
    await _seed(repository)
    seen: dict[str, object] = {}

    async def upstream(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["method"] = request.method
        seen["headers"] = dict(request.headers)
        return httpx.Response(204, request=request)

    response = await _post_test(
        _admin_runtime(repository, httpx.MockTransport(upstream))
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert isinstance(data["latency_ms"], int)
    assert data["latency_ms"] >= 0
    assert data == {
        "connector_id": "connector.governance",
        "success": True,
        "reachable": True,
        "status_code": 204,
        "latency_ms": data["latency_ms"],
        "error_code": None,
        "message": "连接成功，上游服务可达。",
    }
    assert seen["url"] == "https://93.184.216.34/health"
    assert seen["method"] == "HEAD"
    headers = seen["headers"]
    assert isinstance(headers, dict)
    assert "authorization" not in headers
    assert "vault.connector.governance" not in response.text
    assert "93.184.216.34" not in response.text


@pytest.mark.asyncio
async def test_connector_probe_reports_real_upstream_401_without_leaking_response_body() -> None:
    repository = InMemoryCapabilityRepository()
    await _seed(repository)

    async def upstream(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            text="secret upstream diagnostics and bearer material",
            headers={"WWW-Authenticate": 'Bearer realm="private"'},
            request=request,
        )

    response = await _post_test(
        _admin_runtime(repository, httpx.MockTransport(upstream))
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["success"] is False
    assert data["reachable"] is True
    assert data["status_code"] == 401
    assert data["error_code"] == "upstream_authentication_required"
    assert data["message"] == "网络可达，但上游服务要求认证。"
    assert "secret upstream" not in response.text
    assert "realm" not in response.text


@pytest.mark.asyncio
async def test_connector_probe_revalidates_ssrf_at_execution_time_and_never_sends() -> None:
    repository = InMemoryCapabilityRepository()
    await _seed(repository, base_url="http://127.0.0.1:9999/internal")
    calls = 0

    async def upstream(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, request=request)

    response = await _post_test(
        _admin_runtime(repository, httpx.MockTransport(upstream))
    )

    assert response.status_code == 200
    assert response.json()["data"] == {
        "connector_id": "connector.governance",
        "success": False,
        "reachable": False,
        "status_code": None,
        "latency_ms": None,
        "error_code": "target_blocked",
        "message": "连接目标未通过安全校验，已拒绝发起请求。",
    }
    assert calls == 0
    assert "127.0.0.1" not in response.text


@pytest.mark.asyncio
async def test_connector_probe_requires_control_plane_identity() -> None:
    repository = InMemoryCapabilityRepository()
    await _seed(repository)

    response = await _post_test(
        _admin_runtime(repository, httpx.MockTransport(lambda request: httpx.Response(204))),
        token=False,
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthenticated"


@pytest.mark.asyncio
async def test_connector_probe_requires_capability_manage_permission() -> None:
    repository = InMemoryCapabilityRepository()
    await _seed(repository)

    response = await _post_test(
        _admin_runtime(
            repository,
            httpx.MockTransport(lambda request: httpx.Response(204)),
            roles=["business_viewer"],
        )
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"


@pytest.mark.asyncio
async def test_connector_probe_blocks_credentials_embedded_in_url_before_network() -> None:
    repository = InMemoryCapabilityRepository()
    await _seed(repository, base_url="https://user:password@example.com/health")
    calls = 0

    async def upstream(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(204, request=request)

    response = await _post_test(
        _admin_runtime(repository, httpx.MockTransport(upstream))
    )

    assert response.status_code == 200
    assert response.json()["data"]["error_code"] == "target_blocked"
    assert calls == 0
    assert "password" not in response.text


@pytest.mark.asyncio
async def test_connector_probe_allows_only_an_exact_allowlisted_private_host() -> None:
    repository = InMemoryCapabilityRepository()
    await _seed(repository, base_url="http://127.0.0.1:9666/health")
    calls = 0

    async def upstream(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(204, request=request)

    response = await _post_test(
        _admin_runtime(
            repository,
            httpx.MockTransport(upstream),
            allowed_private_hosts=frozenset({"127.0.0.1"}),
        )
    )

    assert response.status_code == 200
    assert response.json()["data"]["success"] is True
    assert calls == 1


@pytest.mark.asyncio
async def test_connector_probe_denied_host_wins_over_private_allowlist() -> None:
    repository = InMemoryCapabilityRepository()
    await _seed(
        repository,
        base_url="http://127.0.0.1:9666/health",
        denied_hosts=["127.0.0.1"],
    )
    calls = 0

    async def upstream(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(204, request=request)

    response = await _post_test(
        _admin_runtime(
            repository,
            httpx.MockTransport(upstream),
            allowed_private_hosts=frozenset({"127.0.0.1"}),
        )
    )

    assert response.json()["data"]["error_code"] == "target_blocked"
    assert calls == 0


@pytest.mark.asyncio
async def test_connector_probe_never_allows_metadata_when_private_host_is_allowlisted() -> None:
    repository = InMemoryCapabilityRepository()
    await _seed(repository, base_url="http://169.254.169.254/latest/meta-data")
    calls = 0

    async def upstream(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(204, request=request)

    response = await _post_test(
        _admin_runtime(
            repository,
            httpx.MockTransport(upstream),
            allowed_private_hosts=frozenset({"169.254.169.254"}),
        )
    )

    assert response.json()["data"]["error_code"] == "target_blocked"
    assert calls == 0


@pytest.mark.asyncio
async def test_connector_probe_fails_closed_for_dns_hostname_before_network() -> None:
    repository = InMemoryCapabilityRepository()
    await _seed(repository, base_url="https://example.com/health")
    calls = 0

    async def upstream(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(204, request=request)

    response = await _post_test(
        _admin_runtime(repository, httpx.MockTransport(upstream))
    )

    assert response.json()["data"]["error_code"] == "dns_target_not_pinned"
    assert calls == 0
