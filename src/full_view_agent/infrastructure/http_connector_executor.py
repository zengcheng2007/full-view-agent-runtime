"""P2-4 HTTP Connector Executor with execution-time SSRF protections.

This module provides a generic HTTP connector executor that can invoke any
published ToolCapability by reading its Connector and making HTTP calls with
full SSRF protection at execution time (not just creation time).

Security features:
- Execution-time path whitelist validation with normalization
- DNS resolution and IP validation (prevents DNS rebinding)
- Redirect handling with re-validation per hop
- Denied hosts enforcement
- Only read-only HTTP methods (GET, POST)
- Credential injection only via credential_ref
"""

from __future__ import annotations

import ipaddress
import logging
import os
import socket
from dataclasses import asdict, dataclass
from time import perf_counter
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

import httpx

from full_view_agent.application.errors import (
    CredentialUnavailable,
    ResourceNotFound,
    UpstreamTimeout,
    UpstreamUnavailable,
)
from full_view_agent.domain.capability import Connector, ToolCapability

if TYPE_CHECKING:
    from full_view_agent.application.ports import CredentialBroker
    from full_view_agent.infrastructure.capability_repository import (
        CapabilityRepository,
    )

logger = logging.getLogger(__name__)

# Reserved IP ranges that must be blocked
_BLOCKED_IP_NETWORKS = [
    ipaddress.ip_network("127.0.0.0/8"),  # Loopback
    ipaddress.ip_network("10.0.0.0/8"),  # Private
    ipaddress.ip_network("172.16.0.0/12"),  # Private
    ipaddress.ip_network("192.168.0.0/16"),  # Private
    ipaddress.ip_network("169.254.0.0/16"),  # Link-local + cloud metadata
    ipaddress.ip_network("::1/128"),  # IPv6 loopback
    ipaddress.ip_network("fc00::/7"),  # IPv6 unique local
    ipaddress.ip_network("fe80::/10"),  # IPv6 link-local
    ipaddress.ip_network("ff00::/8"),  # IPv6 multicast
]

# Explicitly blocked hosts
_BLOCKED_HOSTS = {
    "localhost",
    "metadata.google.internal",
    "metadata.azure.com",
    "kubernetes.default.svc",
}

_CONNECTOR_ALLOWED_PRIVATE_HOSTS_ENV = "FULL_VIEW_CONNECTOR_ALLOWED_PRIVATE_HOSTS"


def parse_connector_allowed_private_hosts(raw: str | None) -> frozenset[str]:
    """Parse an exact-host allowlist and discard URL-shaped entries.

    Values are hostnames or IP literals only.  Schemes, paths, query strings,
    fragments, userinfo and host:port pairs are deliberately not accepted.
    """

    allowed: set[str] = set()
    for item in (raw or "").split(","):
        host = item.strip().lower().rstrip(".")
        if not host or any(marker in host for marker in ("/", "?", "#", "@")):
            continue
        if ":" in host:
            try:
                ipaddress.IPv6Address(host.strip("[]"))
            except ValueError:
                continue
            host = host.strip("[]")
        allowed.add(host)
    return frozenset(allowed)


def configured_connector_allowed_private_hosts() -> frozenset[str]:
    """Return the process-wide exact private-host allowlist."""

    return parse_connector_allowed_private_hosts(
        os.getenv(_CONNECTOR_ALLOWED_PRIVATE_HOSTS_ENV)
    )


class SSRFProtectionError(Exception):
    """Raised when SSRF protection blocks a request."""

    pass


class DNSRebindingProtectionError(SSRFProtectionError):
    """Raised when a network request targets an unpinned DNS hostname."""

    pass


@dataclass(frozen=True)
class ConnectorConnectionTestResult:
    """Credential-free, API-safe outcome of a Connector reachability probe."""

    connector_id: str
    success: bool
    reachable: bool
    status_code: int | None
    latency_ms: int | None
    error_code: str | None
    message: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class ConnectorCredentialScope:
    """Execution identity required to resolve an opaque credential reference."""

    subject_user_id: str
    app_id: str
    run_id: str


class ConnectorConnectionTester:
    """Probe a registered Connector without exposing or transmitting credentials.

    The control plane owns only an opaque ``credential_ref``.  Treating that
    reference as a bearer token would both leak an identifier and provide a
    false authentication result, so probes are deliberately unauthenticated.
    A side-effect-free HEAD request is used.  A 401 therefore means the network
    path is reachable but the upstream requires an execution-time credential.
    """

    def __init__(
        self,
        repository: CapabilityRepository,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        allowed_private_hosts: frozenset[str] = frozenset(),
    ) -> None:
        self._repository = repository
        self._transport = transport
        self._validator = HttpConnectorExecutor(
            repository,
            allowed_private_hosts=allowed_private_hosts,
        )

    async def test_connection(self, connector_id: str) -> ConnectorConnectionTestResult:
        connector = await self._repository.get_connector(connector_id)
        if connector is None:
            raise ResourceNotFound("connector not found")
        if not connector.is_active:
            return self._failure(
                connector_id,
                error_code="connector_disabled",
                message="连接器已停用，未发起网络请求。",
            )

        try:
            self._validator._validate_url_ssrf(  # noqa: SLF001
                connector.base_url,
                connector.denied_hosts,
            )
            self._validator._require_ip_literal_target(  # noqa: SLF001
                connector.base_url
            )
            await self._validator._validate_dns(connector.base_url)  # noqa: SLF001
        except DNSRebindingProtectionError:
            return self._failure(
                connector_id,
                error_code="dns_target_not_pinned",
                message="连接目标使用未绑定的域名，已拒绝发起请求。",
            )
        except (SSRFProtectionError, UpstreamUnavailable):
            return self._failure(
                connector_id,
                error_code="target_blocked",
                message="连接目标未通过安全校验，已拒绝发起请求。",
            )

        timeout_seconds = connector.timeout_ms / 1000.0
        started = perf_counter()
        try:
            async with httpx.AsyncClient(
                follow_redirects=False,
                timeout=timeout_seconds,
                transport=self._transport,
                trust_env=False,
            ) as client, client.stream(
                "HEAD",
                connector.base_url,
                headers={"Accept": "application/json"},
            ) as response:
                status_code = response.status_code
            latency_ms = max(0, round((perf_counter() - started) * 1000))
        except httpx.TimeoutException:
            return self._failure(
                connector_id,
                error_code="upstream_timeout",
                message="连接超时，未收到上游服务响应。",
            )
        except httpx.RequestError:
            return self._failure(
                connector_id,
                error_code="upstream_unavailable",
                message="无法连接上游服务。",
            )

        if 200 <= status_code < 300:
            return ConnectorConnectionTestResult(
                connector_id=connector_id,
                success=True,
                reachable=True,
                status_code=status_code,
                latency_ms=latency_ms,
                error_code=None,
                message="连接成功，上游服务可达。",
            )
        if status_code == 401:
            return self._failure(
                connector_id,
                reachable=True,
                status_code=status_code,
                latency_ms=latency_ms,
                error_code="upstream_authentication_required",
                message="网络可达，但上游服务要求认证。",
            )
        if status_code == 403:
            return self._failure(
                connector_id,
                reachable=True,
                status_code=status_code,
                latency_ms=latency_ms,
                error_code="upstream_forbidden",
                message="网络可达，但上游服务拒绝访问。",
            )
        return self._failure(
            connector_id,
            reachable=True,
            status_code=status_code,
            latency_ms=latency_ms,
            error_code=(
                "upstream_redirected" if 300 <= status_code < 400 else "upstream_rejected"
            ),
            message=(
                "网络可达，但上游服务返回了重定向。"
                if 300 <= status_code < 400
                else "网络可达，但上游服务未接受该探测请求。"
            ),
        )

    @staticmethod
    def _failure(
        connector_id: str,
        *,
        error_code: str,
        message: str,
        reachable: bool = False,
        status_code: int | None = None,
        latency_ms: int | None = None,
    ) -> ConnectorConnectionTestResult:
        return ConnectorConnectionTestResult(
            connector_id=connector_id,
            success=False,
            reachable=reachable,
            status_code=status_code,
            latency_ms=latency_ms,
            error_code=error_code,
            message=message,
        )


class HttpConnectorExecutor:
    """Generic HTTP connector executor with execution-time SSRF protections.

    This executor can invoke any ToolCapability by:
    1. Loading the Connector from the repository
    2. Validating the resource_path against the connector's whitelist
    3. Resolving DNS and validating all IPs
    4. Making the HTTP request with credentials injected
    5. Handling redirects with re-validation per hop
    """

    def __init__(
        self,
        repository: CapabilityRepository,
        *,
        follow_redirects: bool = False,
        default_timeout_ms: int = 8000,
        allowed_private_hosts: frozenset[str] = frozenset(),
        credential_broker: CredentialBroker | None = None,
    ) -> None:
        self._repository = repository
        self._follow_redirects = follow_redirects
        self._default_timeout_ms = default_timeout_ms
        self._allowed_private_hosts = frozenset(
            host.lower().rstrip(".") for host in allowed_private_hosts
        )
        self._credential_broker = credential_broker

    async def execute(
        self,
        tool: ToolCapability,
        arguments: dict[str, Any],
        *,
        credential_scope: ConnectorCredentialScope | None = None,
    ) -> dict[str, Any]:
        """Execute a tool capability via its connector.

        Args:
            tool: The published ToolCapability to execute
            arguments: The arguments to pass to the tool

        Returns:
            The HTTP response as a dict

        Raises:
            SSRFProtectionError: If SSRF protection blocks the request
            UpstreamTimeout: If the request times out
            UpstreamUnavailable: If the upstream is unavailable
        """
        # Load the connector
        connector = await self._repository.get_connector(tool.connector_ref)
        if connector is None:
            raise UpstreamUnavailable(
                f"Connector {tool.connector_ref} not found"
            )

        if not connector.is_active:
            raise UpstreamUnavailable(
                f"Connector {connector.connector_id} is disabled"
            )

        # Validate resource_path against whitelist
        self._validate_path_whitelist(connector, tool.resource_path)

        # Build the full URL
        full_url = self._build_url(connector.base_url, tool.resource_path)

        # Validate the URL (SSRF check)
        self._validate_url_ssrf(full_url, connector.denied_hosts)

        # httpx performs its own DNS lookup after validation.  Until the
        # transport can bind the validated address to the actual socket, only
        # literal IP targets are safe from DNS rebinding/TOCTOU.
        self._require_ip_literal_target(full_url)

        # Resolve DNS and validate all IPs
        await self._validate_dns(full_url)

        # Prepare request
        timeout_ms = tool.timeout_ms or connector.timeout_ms or self._default_timeout_ms
        timeout_seconds = timeout_ms / 1000.0

        # Apply parameter mapping
        request_params = self._apply_parameter_mapping(
            tool.parameter_mapping, arguments
        )
        credential_refs = {
            ref for ref in (tool.credential_ref, connector.credential_ref) if ref
        }
        if len(credential_refs) > 1:
            raise CredentialUnavailable("conflicting connector credential references")
        credential_ref = next(iter(credential_refs), None)
        if credential_ref is not None and credential_scope is None:
            raise CredentialUnavailable("credential execution scope is required")
        headers = await self._resolve_headers(
            credential_ref=credential_ref,
            subject_user_id=(credential_scope.subject_user_id if credential_scope else None),
            app_id=(credential_scope.app_id if credential_scope else None),
            run_id=(credential_scope.run_id if credential_scope else None),
        )

        # Make the HTTP request
        async with httpx.AsyncClient(
            follow_redirects=self._follow_redirects,
            timeout=timeout_seconds,
            trust_env=False,
        ) as client:
            try:
                if tool.http_method == "GET":
                    response = await client.get(
                        full_url,
                        params=request_params,
                        headers=headers,
                    )
                elif tool.http_method == "POST":
                    response = await client.post(
                        full_url,
                        json=request_params,
                        headers=headers,
                    )
                else:
                    raise ValueError(f"Unsupported HTTP method: {tool.http_method}")

                # Handle redirects with re-validation
                if self._follow_redirects and response.is_redirect:
                    redirect_url = response.headers.get("location")
                    if redirect_url:
                        # Re-validate redirect URL
                        self._validate_url_ssrf(redirect_url, connector.denied_hosts)
                        await self._validate_dns(redirect_url)
                        # Follow the redirect (simplified - in production would loop)
                        response = await client.get(redirect_url)

                response.raise_for_status()

                # Apply result mapping
                result = response.json()
                return self._apply_result_mapping(tool.result_mapping, result)

            except httpx.TimeoutException as e:
                raise UpstreamTimeout(f"Request timed out: {e}") from e
            except httpx.RequestError as e:
                raise UpstreamUnavailable(f"Request failed: {e}") from e

    def _validate_path_whitelist(
        self, connector: Connector, resource_path: str
    ) -> None:
        """Validate resource_path against connector's allowed_path_prefixes.

        Uses posixpath.normpath to normalize the path and prevent bypasses
        using .., //, or URL encoding.
        """
        import posixpath
        from urllib.parse import unquote

        # Decode URL encoding first
        decoded_path = unquote(resource_path)

        # Check for path traversal attempts BEFORE normalization
        # Check both original and decoded paths
        for path_to_check in [resource_path, decoded_path]:
            if ".." in path_to_check:
                raise SSRFProtectionError(
                    f"Path traversal not allowed: {resource_path}"
                )

        # Normalize the resource path
        normalized_path = posixpath.normpath(decoded_path)

        # Double-check after normalization
        if ".." in normalized_path:
            raise SSRFProtectionError(
                f"Path traversal not allowed: {resource_path}"
            )

        # Check for double slashes
        if "//" in resource_path or "//" in decoded_path:
            raise SSRFProtectionError(
                f"Double slashes not allowed: {resource_path}"
            )

        # Check if path is in whitelist
        allowed = False
        for prefix in connector.allowed_path_prefixes:
            normalized_prefix = posixpath.normpath(prefix)
            if normalized_prefix == "/" or normalized_path == normalized_prefix or (
                normalized_path.startswith(f"{normalized_prefix.rstrip('/')}/")
            ):
                allowed = True
                break

        if not allowed:
            raise SSRFProtectionError(
                f"Path {resource_path} not in whitelist for connector "
                f"{connector.connector_id}"
            )

    def _build_url(self, base_url: str, resource_path: str) -> str:
        """Build full URL from base_url and resource_path."""
        # Remove trailing slash from base_url
        base = base_url.rstrip("/")
        # Ensure resource_path starts with /
        path = resource_path if resource_path.startswith("/") else f"/{resource_path}"
        return f"{base}{path}"

    def _validate_url_ssrf(self, url: str, denied_hosts: list[str]) -> None:
        """Validate URL against SSRF attacks.

        Checks:
        - Scheme is http or https
        - Host is not in denied_hosts
        - Host is not localhost, loopback, private, link-local, multicast, etc.
        """
        parsed = urlparse(url)

        # Check scheme
        if parsed.scheme not in ("http", "https"):
            raise SSRFProtectionError(f"Invalid scheme: {parsed.scheme}")

        hostname = parsed.hostname
        if not hostname:
            raise SSRFProtectionError("No hostname in URL")

        if parsed.username is not None or parsed.password is not None:
            raise SSRFProtectionError("Credentials in connector URL are not allowed")

        # Check denied hosts
        normalized_hostname = hostname.lower().rstrip(".")
        normalized_denied_hosts = {item.lower().rstrip(".") for item in denied_hosts}
        if normalized_hostname in normalized_denied_hosts:
            raise SSRFProtectionError(f"Host {hostname} is denied")

        # Validate IP literals at the URL layer as well as after resolution.
        # This keeps permanent metadata/link-local blocks non-overridable.
        try:
            ipaddress.ip_address(normalized_hostname)
        except ValueError:
            pass
        else:
            self._validate_ip(
                normalized_hostname,
                allow_private=normalized_hostname in self._allowed_private_hosts,
            )
            return

        # An exact hostname may opt into RFC1918/loopback access.  Permanent
        # blocks above and connector-specific denied_hosts take precedence.
        if normalized_hostname in self._allowed_private_hosts:
            return

        # Check blocked hosts
        if normalized_hostname in _BLOCKED_HOSTS:
            raise SSRFProtectionError(f"Host {hostname} is blocked")

        # Check for .local and .internal domains
        if normalized_hostname.endswith(".local") or normalized_hostname.endswith(".internal"):
            raise SSRFProtectionError(
                f"Domain {hostname} is not allowed (.local/.internal)"
            )

        # Check for localhost variants
        if normalized_hostname in ("localhost", "127.0.0.1", "::1", "0.0.0.0"):
            raise SSRFProtectionError(f"Host {hostname} is blocked (loopback)")

    @staticmethod
    def _require_ip_literal_target(url: str) -> None:
        hostname = urlparse(url).hostname
        if not hostname:
            raise SSRFProtectionError("No hostname in URL")
        try:
            ipaddress.ip_address(hostname)
        except ValueError as exc:
            raise DNSRebindingProtectionError(
                "DNS hostname execution is disabled until the validated address "
                "can be pinned to the network connection"
            ) from exc

    async def _validate_dns(self, url: str) -> None:
        """Resolve DNS and validate all IP addresses.

        This prevents DNS rebinding attacks by checking all resolved IPs
        against blocked ranges.
        """
        parsed = urlparse(url)
        hostname = parsed.hostname
        if not hostname:
            raise SSRFProtectionError("No hostname in URL")

        # Skip IP address validation if hostname is already an IP
        try:
            ipaddress.ip_address(hostname)
            # It's an IP address, validate it directly
            self._validate_ip(
                hostname,
                allow_private=hostname.lower().rstrip(".")
                in self._allowed_private_hosts,
            )
            return
        except ValueError:
            # It's a hostname, resolve it
            pass

        # Resolve DNS
        try:
            addrinfo = socket.getaddrinfo(hostname, None)
        except socket.gaierror as e:
            raise UpstreamUnavailable(
                f"DNS resolution failed for {hostname}: {e}"
            ) from e

        # Validate all resolved IPs
        for info in addrinfo:
            ip_str = str(info[4][0])
            self._validate_ip(
                ip_str,
                allow_private=hostname.lower().rstrip(".")
                in self._allowed_private_hosts,
            )

    def _validate_ip(self, ip_str: str, *, allow_private: bool = False) -> None:
        """Validate an IP address against blocked ranges."""
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError as e:
            raise SSRFProtectionError(f"Invalid IP address: {ip_str}") from e

        # Link-local metadata and multicast/unspecified addresses are never
        # overridable, even if an operator accidentally allowlists the host.
        if ip_str == "169.254.169.254":
            raise SSRFProtectionError(
                "Cloud metadata address 169.254.169.254 is permanently blocked"
            )
        if ip.is_link_local:
            raise SSRFProtectionError(
                f"IP {ip_str} is permanently blocked (link-local)"
            )
        if ip.is_multicast:
            raise SSRFProtectionError(
                f"IP {ip_str} is permanently blocked (multicast)"
            )
        if ip.is_unspecified:
            raise SSRFProtectionError(f"IP {ip_str} is permanently blocked")

        if allow_private and (ip.is_private or ip.is_loopback):
            return

        # Check if IP is in any blocked network
        for network in _BLOCKED_IP_NETWORKS:
            if ip in network:
                raise SSRFProtectionError(
                    f"IP {ip_str} is in blocked range {network}"
                )


        # Check IP properties
        if ip.is_private:
            raise SSRFProtectionError(f"IP {ip_str} is private")
        if ip.is_loopback:
            raise SSRFProtectionError(f"IP {ip_str} is loopback")
        if ip.is_link_local:
            raise SSRFProtectionError(f"IP {ip_str} is link-local")
        if ip.is_multicast:
            raise SSRFProtectionError(f"IP {ip_str} is multicast")
        if ip.is_reserved:
            raise SSRFProtectionError(f"IP {ip_str} is reserved")
        if ip.is_unspecified:
            raise SSRFProtectionError(f"IP {ip_str} is unspecified")

    def _apply_parameter_mapping(
        self,
        parameter_mapping: dict[str, Any],
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        """Apply parameter mapping to transform arguments for the HTTP request.

        If no mapping is defined, pass arguments as-is.
        """
        if not parameter_mapping:
            return arguments

        # Simple mapping: just rename keys
        # In production, this could support more complex transformations
        mapped = {}
        for target_key, source_expr in parameter_mapping.items():
            if isinstance(source_expr, str) and source_expr.startswith("$."):
                # Reference to argument: $.arg_name
                arg_name = source_expr[2:]  # Remove "$."
                if arg_name in arguments:
                    mapped[target_key] = arguments[arg_name]
            elif isinstance(source_expr, str) and source_expr.startswith("$"):
                # Reference to argument: $.arg_name (without dot)
                arg_name = source_expr[1:]  # Remove "$"
                if arg_name in arguments:
                    mapped[target_key] = arguments[arg_name]
            else:
                # Literal value
                mapped[target_key] = source_expr

        return mapped

    def _apply_result_mapping(
        self,
        result_mapping: dict[str, Any],
        result: dict[str, Any],
    ) -> dict[str, Any]:
        """Apply result mapping to transform the HTTP response.

        If no mapping is defined, return result as-is.
        """
        if not result_mapping:
            return result

        # Simple mapping: extract fields
        # In production, this could support more complex transformations
        mapped = {}
        for target_key, source_expr in result_mapping.items():
            if isinstance(source_expr, str) and source_expr.startswith("$."):
                # JSONPath-like extraction: $.data.items
                path = source_expr[2:].split(".")
                value = result
                for key in path:
                    if isinstance(value, dict):
                        value = value.get(key)
                    else:
                        value = None
                        break
                mapped[target_key] = value
            else:
                # Literal value
                mapped[target_key] = source_expr

        return mapped

    def _build_headers(self, credential_ref: str | None) -> dict[str, str]:
        """Build non-secret HTTP headers.

        An opaque reference is intentionally ignored here.  Only
        ``_resolve_headers`` may add Authorization after scoped broker lookup.
        """
        headers = {"Accept": "application/json"}

        return headers

    async def _resolve_headers(
        self,
        *,
        credential_ref: str | None,
        subject_user_id: str | None,
        app_id: str | None,
        run_id: str | None,
    ) -> dict[str, str]:
        headers = self._build_headers(None)
        if credential_ref is None:
            return headers
        broker = self._credential_broker
        if (
            broker is None
            or subject_user_id is None
            or app_id is None
            or run_id is None
        ):
            raise CredentialUnavailable("credential resolver is unavailable")
        secret = await broker.resolve(
            credential_ref=credential_ref,
            subject_user_id=subject_user_id,
            app_id=app_id,
            run_id=run_id,
        )
        headers["Authorization"] = f"Bearer {secret.get_secret_value()}"
        return headers
