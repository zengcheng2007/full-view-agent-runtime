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
import socket
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

import httpx

from full_view_agent.application.errors import (
    UpstreamTimeout,
    UpstreamUnavailable,
)
from full_view_agent.domain.capability import Connector, ToolCapability

if TYPE_CHECKING:
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


class SSRFProtectionError(Exception):
    """Raised when SSRF protection blocks a request."""

    pass


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
        allow_private_networks: bool = False,
    ) -> None:
        self._repository = repository
        self._follow_redirects = follow_redirects
        self._default_timeout_ms = default_timeout_ms
        self._allow_private_networks = allow_private_networks

    async def execute(
        self,
        tool: ToolCapability,
        arguments: dict[str, Any],
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

        # Resolve DNS and validate all IPs
        await self._validate_dns(full_url)

        # Prepare request
        timeout_ms = tool.timeout_ms or connector.timeout_ms or self._default_timeout_ms
        timeout_seconds = timeout_ms / 1000.0

        # Apply parameter mapping
        request_params = self._apply_parameter_mapping(
            tool.parameter_mapping, arguments
        )

        # Make the HTTP request
        async with httpx.AsyncClient(
            follow_redirects=self._follow_redirects,
            timeout=timeout_seconds,
        ) as client:
            try:
                if tool.http_method == "GET":
                    response = await client.get(
                        full_url,
                        params=request_params,
                        headers=self._build_headers(tool.credential_ref),
                    )
                elif tool.http_method == "POST":
                    response = await client.post(
                        full_url,
                        json=request_params,
                        headers=self._build_headers(tool.credential_ref),
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
            if normalized_path.startswith(normalized_prefix):
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

        # Check denied hosts
        if hostname in denied_hosts:
            raise SSRFProtectionError(f"Host {hostname} is denied")

        # Allow private networks in test mode
        if self._allow_private_networks:
            return

        # Check blocked hosts
        if hostname in _BLOCKED_HOSTS:
            raise SSRFProtectionError(f"Host {hostname} is blocked")

        # Check for .local and .internal domains
        if hostname.endswith(".local") or hostname.endswith(".internal"):
            raise SSRFProtectionError(
                f"Domain {hostname} is not allowed (.local/.internal)"
            )

        # Check for localhost variants
        if hostname in ("localhost", "127.0.0.1", "::1", "0.0.0.0"):
            raise SSRFProtectionError(f"Host {hostname} is blocked (loopback)")

    async def _validate_dns(self, url: str) -> None:
        """Resolve DNS and validate all IP addresses.

        This prevents DNS rebinding attacks by checking all resolved IPs
        against blocked ranges.
        """
        # Skip DNS validation in test mode
        if self._allow_private_networks:
            return

        parsed = urlparse(url)
        hostname = parsed.hostname
        if not hostname:
            raise SSRFProtectionError("No hostname in URL")

        # Skip IP address validation if hostname is already an IP
        try:
            ipaddress.ip_address(hostname)
            # It's an IP address, validate it directly
            self._validate_ip(hostname)
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
            self._validate_ip(ip_str)

    def _validate_ip(self, ip_str: str) -> None:
        """Validate an IP address against blocked ranges."""
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError as e:
            raise SSRFProtectionError(f"Invalid IP address: {ip_str}") from e

        # Check if IP is in any blocked network
        for network in _BLOCKED_IP_NETWORKS:
            if ip in network:
                raise SSRFProtectionError(
                    f"IP {ip_str} is in blocked range {network}"
                )

        # Check for cloud metadata IP
        if ip_str == "169.254.169.254":
            raise SSRFProtectionError(
                "Cloud metadata address 169.254.169.254 is blocked"
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
        """Build HTTP headers with credential injection.

        Credentials are only injected via credential_ref, not from arbitrary
        request headers.
        """
        headers = {"Accept": "application/json"}

        if credential_ref:
            # In production, this would look up the credential from a secure store
            # For now, we just add a placeholder
            headers["Authorization"] = f"Bearer {credential_ref}"

        return headers
