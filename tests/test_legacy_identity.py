import httpx
import pytest
from pydantic import SecretStr

from full_view_agent.application.errors import AuthenticationFailed
from full_view_agent.infrastructure.legacy_identity import (
    HashedLegacyIdentityAdapter,
    HttpLegacyIdentityAdapter,
)


async def test_legacy_identity_adapter_returns_standard_identity_without_token() -> None:
    adapter = HashedLegacyIdentityAdapter()

    identity = await adapter.resolve(SecretStr("legacy-token-user-01"))
    repeated = await adapter.resolve(SecretStr("legacy-token-user-01"))
    other = await adapter.resolve(SecretStr("legacy-token-user-02"))

    assert identity.principal.user_id == repeated.principal.user_id
    assert identity.principal.user_id != other.principal.user_id
    assert identity.source == "legacy_geo_user_fixture"
    assert identity.base_area_codes == ["330106"]
    assert "legacy-token-user-01" not in identity.model_dump_json()
    assert "legacy-token-user-01" not in repr(identity)


@pytest.mark.asyncio
async def test_http_legacy_identity_adapter_validates_token_with_gateway() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/getUserByToken"
        assert request.url.query == b""
        assert request.headers["geoToken"] == "valid-legacy-token"
        return httpx.Response(
            200,
            json={
                "state": True,
                "code": 200,
                "data": {
                    "systemid": "user-42",
                    "tenantId": "tenant-hz",
                    "orgId": "org-xh",
                    "roleCodes": "governance_analyst,viewer",
                    "areaCode": "330106",
                },
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = HttpLegacyIdentityAdapter(
            base_url="http://legacy-gateway",
            client=client,
        )
        identity = await adapter.resolve(SecretStr("valid-legacy-token"))

    assert identity.principal.user_id == "user-42"
    assert identity.principal.tenant_id == "tenant-hz"
    assert identity.principal.org_id == "org-xh"
    assert identity.principal.roles == ["governance_analyst", "viewer"]
    assert identity.base_area_codes == ["330106"]
    assert identity.source == "legacy_geo_gateway"
    assert "valid-legacy-token" not in identity.model_dump_json()


@pytest.mark.asyncio
async def test_http_legacy_identity_adapter_reads_actual_legacy_role_field() -> None:
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            json={
                "state": True,
                "code": 200,
                "data": {
                    "systemid": "user-legacy-role",
                    "role": "2,7",
                    "areaCode": "330106",
                    "organizatedId": "org-legacy",
                },
            },
        )
    )
    async with httpx.AsyncClient(transport=transport) as client:
        adapter = HttpLegacyIdentityAdapter(
            base_url="http://legacy-gateway",
            client=client,
        )
        identity = await adapter.resolve(SecretStr("valid-legacy-token"))

    assert identity.principal.roles == ["2", "7"]
    assert identity.principal.org_id == "org-legacy"


@pytest.mark.asyncio
async def test_http_legacy_identity_adapter_rejects_expired_token_response() -> None:
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            json={
                "state": False,
                "code": 401,
                "msg": "token已过期或不存在",
                "data": [],
            },
        )
    )
    async with httpx.AsyncClient(transport=transport) as client:
        adapter = HttpLegacyIdentityAdapter(
            base_url="http://legacy-gateway",
            client=client,
        )
        with pytest.raises(AuthenticationFailed):
            await adapter.resolve(SecretStr("expired-token"))
