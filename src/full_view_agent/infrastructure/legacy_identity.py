import hashlib
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import httpx
from pydantic import SecretStr

from full_view_agent.application.errors import (
    AuthenticationFailed,
    IdentityProviderUnavailable,
)
from full_view_agent.domain.models import LegacyIdentitySnapshot, Principal


class HashedLegacyIdentityAdapter:
    """Deterministic test adapter. Production runtime must use the HTTP adapter."""

    def __init__(self, *, now: Callable[[], datetime] | None = None) -> None:
        self._now = now or (lambda: datetime.now(UTC))

    async def resolve(self, raw_token: SecretStr) -> LegacyIdentitySnapshot:
        token_hash = hashlib.sha256(
            raw_token.get_secret_value().encode("utf-8")
        ).hexdigest()[:24]
        return LegacyIdentitySnapshot(
            principal=Principal(
                tenant_id="tenant_hz",
                user_id=f"legacy_{token_hash}",
                org_id="org_legacy_fixture",
                roles=["governance_analyst"],
            ),
            source="legacy_geo_user_fixture",
            source_session_expires_at=self._now() + timedelta(hours=1),
            base_area_codes=["330106"],
        )


class HttpLegacyIdentityAdapter:
    def __init__(
        self,
        *,
        base_url: str,
        client: httpx.AsyncClient | None = None,
        timeout_seconds: float = 5.0,
        source_session_ttl_seconds: int = 300,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._endpoint = f"{base_url.rstrip('/')}/getUserByToken"
        self._client = client
        self._timeout = timeout_seconds
        self._source_session_ttl = timedelta(seconds=source_session_ttl_seconds)
        self._now = now or (lambda: datetime.now(UTC))

    async def resolve(self, raw_token: SecretStr) -> LegacyIdentitySnapshot:
        payload = await self._fetch_identity(raw_token)
        data = payload.get("data")
        if payload.get("state") is not True or payload.get("code") != 200:
            raise AuthenticationFailed("geoToken is expired or invalid")
        if not isinstance(data, dict) or not data:
            raise AuthenticationFailed("geoToken has no active identity")

        user_id = _first_text(data, "systemid", "userId", "id", "loginName", "username")
        if user_id is None:
            raise AuthenticationFailed("legacy identity does not contain a user id")
        tenant_id = _first_text(data, "tenantId", "tenant_id") or "legacy"
        org_id = _first_text(
            data,
            "orgId",
            "org_id",
            "departmentId",
            "organizationCode",
            "organizatedId",
        ) or "legacy:unknown"
        roles = _text_list(data, "roleCodes", "roles", "roleIds", "roleName", "role")
        area_codes = _text_list(
            data,
            "areaCode",
            "areacode",
            "countyCode",
            "gridCode",
        )
        return LegacyIdentitySnapshot(
            principal=Principal(
                tenant_id=tenant_id,
                user_id=user_id,
                org_id=org_id,
                roles=roles,
            ),
            source="legacy_geo_gateway",
            source_session_expires_at=self._now() + self._source_session_ttl,
            base_area_codes=area_codes,
        )

    async def _fetch_identity(self, raw_token: SecretStr) -> dict[str, object]:
        headers = {"geoToken": raw_token.get_secret_value()}
        try:
            if self._client is not None:
                response = await self._client.get(
                    self._endpoint,
                    headers=headers,
                    timeout=self._timeout,
                )
            else:
                async with httpx.AsyncClient(follow_redirects=False) as client:
                    response = await client.get(
                        self._endpoint,
                        headers=headers,
                        timeout=self._timeout,
                    )
        except httpx.HTTPError as exc:
            raise IdentityProviderUnavailable(
                "legacy identity service is unavailable"
            ) from exc

        if response.status_code in {401, 403}:
            raise AuthenticationFailed("geoToken is expired or invalid")
        if not response.is_success:
            raise IdentityProviderUnavailable("legacy identity service is unavailable")
        try:
            payload = response.json()
        except ValueError as exc:
            raise IdentityProviderUnavailable(
                "legacy identity service returned an invalid response"
            ) from exc
        if not isinstance(payload, dict):
            raise IdentityProviderUnavailable(
                "legacy identity service returned an invalid response"
            )
        return payload


def _first_text(data: dict[str, object], *keys: str) -> str | None:
    for key in keys:
        value = data.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def _text_list(data: dict[str, object], *keys: str) -> list[str]:
    for key in keys:
        value = data.get(key)
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        if value is not None and str(value).strip():
            normalized = str(value).replace(";", ",")
            return [item.strip() for item in normalized.split(",") if item.strip()]
    return []
