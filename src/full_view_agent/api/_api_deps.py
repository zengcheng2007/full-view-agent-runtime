"""Shared API dependency types.

Breaks the circular import between app.py and capability_routes.py.
Both modules should import these shared types from here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Annotated

from fastapi import Depends, Header, Request
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer
from pydantic import SecretStr

from full_view_agent.application.session_run_service import new_id
from full_view_agent.domain.contract_model import ContractModel
from full_view_agent.domain.models import LegacyIdentitySnapshot


class UnauthenticatedError(Exception):
    pass


@dataclass(frozen=True, repr=False)
class CurrentUser:
    user_id: str
    identity: LegacyIdentitySnapshot
    raw_token: SecretStr


class ResponseMeta(ContractModel):
    request_id: str
    trace_id: str = field(default_factory=lambda: new_id("trc"))
    idempotency_replayed: bool | None = None


geo_token_header = APIKeyHeader(
    name="geoToken",
    scheme_name="GeoToken",
    auto_error=False,
)
control_bearer_header = HTTPBearer(
    scheme_name="BearerAuth",
    auto_error=False,
)


async def require_geotoken(
    request: Request,
    geo_token: Annotated[str | None, Depends(geo_token_header)],
    authorization: Annotated[str | None, Header()] = None,
) -> CurrentUser:
    from full_view_agent.application.errors import (
        InvalidAuthenticationTransport,
    )

    if any(key.casefold() == "geotoken" for key in request.query_params):
        raise InvalidAuthenticationTransport("geoToken must not be sent in the URL")
    if geo_token and authorization and authorization.casefold().startswith("bearer "):
        raise InvalidAuthenticationTransport(
            "geoToken and Bearer authentication cannot be used together"
        )
    if not geo_token:
        raise UnauthenticatedError
    raw_token = SecretStr(geo_token)
    identity = await request.app.state.runtime.identity_port.resolve(raw_token)
    return CurrentUser(
        user_id=identity.principal.user_id,
        identity=identity,
        raw_token=raw_token,
    )


async def require_capability_identity(
    request: Request,
    geo_token: Annotated[str | None, Depends(geo_token_header)],
    bearer: Annotated[
        HTTPAuthorizationCredentials | None,
        Depends(control_bearer_header),
    ],
) -> CurrentUser:
    """Authenticate the independently deployable capability console.

    Bearer is the platform-facing transport. ``geoToken`` remains accepted as
    a compatibility bridge for the existing full-view administrator while the
    standalone identity adapter is being deployed.
    """

    from full_view_agent.application.errors import InvalidAuthenticationTransport

    if any(key.casefold() == "geotoken" for key in request.query_params):
        raise InvalidAuthenticationTransport("authentication must not use the URL")
    bearer_token = bearer.credentials.strip() if bearer is not None else None
    if geo_token and bearer_token:
        raise InvalidAuthenticationTransport(
            "geoToken and Bearer authentication cannot be used together"
        )
    token = bearer_token or geo_token
    if not token:
        raise UnauthenticatedError
    raw_token = SecretStr(token)
    identity_port = (
        request.app.state.runtime.capability_identity_port
        or request.app.state.runtime.identity_port
    )
    identity = await identity_port.resolve(raw_token)
    return CurrentUser(
        user_id=identity.principal.user_id,
        identity=identity,
        raw_token=raw_token,
    )
