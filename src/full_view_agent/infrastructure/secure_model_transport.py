"""Execution-time network controls for outbound model requests."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import ssl
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpcore
import httpx

from full_view_agent.application.errors import ModelProviderUnavailable

ResolveHost = Callable[[str, int], Awaitable[tuple[str, ...]]]
ModelClientFactory = Callable[["ResolvedModelEndpoint"], httpx.AsyncClient]


class ModelEndpointSecurityError(ModelProviderUnavailable):
    """The configured endpoint cannot be connected to safely."""


@dataclass(frozen=True)
class ResolvedModelEndpoint:
    scheme: str
    hostname: str
    port: int
    pinned_ip: str


class ModelEndpointResolver:
    """Parse, resolve and validate a model URL immediately before a request."""

    def __init__(self, *, resolve_host: ResolveHost | None = None) -> None:
        self._resolve_host = resolve_host or _resolve_host

    async def resolve(self, url: str) -> ResolvedModelEndpoint:
        parsed = urlsplit(url)
        if parsed.scheme != "https":
            raise ModelEndpointSecurityError("model endpoint requires HTTPS")
        if parsed.username is not None or parsed.password is not None:
            raise ModelEndpointSecurityError("model endpoint credentials are forbidden")
        if parsed.fragment:
            raise ModelEndpointSecurityError("model endpoint fragments are forbidden")
        if not parsed.hostname:
            raise ModelEndpointSecurityError("model endpoint hostname is required")
        hostname = parsed.hostname.rstrip(".").lower()
        if hostname in {"localhost", "localhost.localdomain"}:
            raise ModelEndpointSecurityError("model endpoint localhost is blocked")
        if hostname.endswith((".local", ".internal")):
            raise ModelEndpointSecurityError("model endpoint internal hostname is blocked")
        port = parsed.port or 443
        try:
            literal = ipaddress.ip_address(hostname)
        except ValueError:
            addresses = await self._resolve_host(hostname, port)
        else:
            addresses = (str(literal),)
        if not addresses:
            raise ModelEndpointSecurityError("model endpoint DNS returned no address")
        normalized: list[str] = []
        for value in addresses:
            try:
                address = ipaddress.ip_address(value)
            except ValueError as exc:
                raise ModelEndpointSecurityError(
                    "model endpoint DNS returned an invalid address"
                ) from exc
            if not address.is_global:
                raise ModelEndpointSecurityError(
                    "model endpoint DNS resolved to a non-public address"
                )
            normalized.append(str(address))
        return ResolvedModelEndpoint(
            scheme=parsed.scheme,
            hostname=hostname,
            port=port,
            pinned_ip=sorted(set(normalized))[0],
        )


async def _resolve_host(hostname: str, port: int) -> tuple[str, ...]:
    loop = asyncio.get_running_loop()
    try:
        records = await loop.getaddrinfo(
            hostname,
            port,
            type=socket.SOCK_STREAM,
        )
    except socket.gaierror as exc:
        raise ModelEndpointSecurityError("model endpoint DNS resolution failed") from exc
    return tuple(str(record[4][0]) for record in records)


class PinnedAsyncNetworkBackend(httpcore.AsyncNetworkBackend):
    """Connect an httpcore origin to its already-validated address.

    The HTTP origin remains the configured hostname. Consequently httpcore
    still supplies that hostname to TLS for SNI/certificate verification and
    to HTTP as the Host header; only the TCP destination is replaced.
    """

    def __init__(self, *, endpoint: ResolvedModelEndpoint, delegate: object) -> None:
        self._endpoint = endpoint
        self._delegate = delegate

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[tuple[int, int, int | bytes]] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        if host.rstrip(".").lower() != self._endpoint.hostname:
            raise ModelEndpointSecurityError("model transport origin changed")
        if port != self._endpoint.port:
            raise ModelEndpointSecurityError("model transport port changed")
        return await self._delegate.connect_tcp(  # type: ignore[attr-defined, no-any-return]
            self._endpoint.pinned_ip,
            port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[tuple[int, int, int | bytes]] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        del path, timeout, socket_options
        raise ModelEndpointSecurityError("unix sockets are not valid model endpoints")

    async def sleep(self, seconds: float) -> None:
        await self._delegate.sleep(seconds)  # type: ignore[attr-defined]


class _PinnedHTTPTransport(httpx.AsyncHTTPTransport):
    def __init__(self, endpoint: ResolvedModelEndpoint) -> None:
        super().__init__(verify=True, trust_env=False, retries=0)
        delegate = httpcore._backends.auto.AutoBackend()  # type: ignore[attr-defined]
        self._pool = httpcore.AsyncConnectionPool(  # type: ignore[assignment]
            ssl_context=ssl.create_default_context(),
            retries=0,
            network_backend=PinnedAsyncNetworkBackend(
                endpoint=endpoint,
                delegate=delegate,
            ),
        )


class SecureModelHttpTransport:
    """Resolve and pin the endpoint afresh for every outbound request."""

    def __init__(
        self,
        *,
        resolver: ModelEndpointResolver | None = None,
        client_factory: ModelClientFactory | None = None,
    ) -> None:
        self._resolver = resolver or ModelEndpointResolver()
        self._client_factory = client_factory or _pinned_client

    async def post(
        self,
        url: str,
        *,
        json: dict[str, object],
        headers: dict[str, str],
        timeout: float,
    ) -> httpx.Response:
        endpoint = await self._resolver.resolve(url)
        async with self._client_factory(endpoint) as client:
            response = await client.post(
                url,
                json=json,
                headers=headers,
                timeout=timeout,
            )
        if response.is_redirect:
            raise ModelEndpointSecurityError("model endpoint redirects are forbidden")
        return response


def _pinned_client(endpoint: ResolvedModelEndpoint) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=_PinnedHTTPTransport(endpoint),
        follow_redirects=False,
        trust_env=False,
    )
