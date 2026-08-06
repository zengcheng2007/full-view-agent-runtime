"""P2 Integration tests: Dynamic tool integration and HTTP connector executor.

Tests G1 (dynamic tool wiring) and G4 (HTTP connector executor with SSRF).
"""

from __future__ import annotations

import asyncio
import socket
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from full_view_agent.application.dynamic_tool_bridge import (
    build_dynamic_tool_registry_entries,
    convert_tool_capability_to_descriptor,
    convert_tool_capability_to_manifest,
    load_published_tools,
)
from full_view_agent.application.tool_registry import ToolRegistry
from full_view_agent.domain.capability import (
    Connector,
    ToolCapability,
)
from full_view_agent.infrastructure.capability_repository import (
    InMemoryCapabilityRepository,
)
from full_view_agent.infrastructure.http_connector_executor import (
    HttpConnectorExecutor,
    SSRFProtectionError,
)


@pytest.fixture
def event_loop():
    """Create an event loop for async tests."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture
def sample_connector():
    """Create a sample connector for testing."""
    return Connector(
        connector_id="test.api",
        name="Test API",
        base_url="https://api.example.com",
        description="Test API connector",
        allowed_path_prefixes=["/api/v1", "/public"],
        denied_hosts=["blocked.example.com"],
        is_active=True,
        credential_ref=None,
        timeout_ms=5000,
    )


@pytest.fixture
def sample_tool():
    """Create a sample tool capability for testing."""
    return ToolCapability(
        capability_id="tool.test_query",
        name="Test Query Tool",
        capability_type="tool",
        domain="governance",
        owner="test-team",
        version="1.0.0",
        status="published",
        risk_level="low",
        required_permissions=["test.read"],
        dataset_ids=["test_dataset"],
        description="A test tool for integration testing",
        connector_ref="test.api",
        http_method="GET",
        resource_path="/api/v1/query",
        input_schema={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
        output_schema={"type": "object"},
        parameter_mapping={"q": "$.query"},
        result_mapping={"data": "$.results"},
        result_kind="table",
        data_schema_ref="schema://test/query/1.0.0",
        timeout_ms=5000,
        max_attempts=2,
        max_result_rows=100,
        cache_enabled=True,
        cache_ttl_seconds=60,
    )


class TestDynamicToolBridge:
    """Test G1: Dynamic tool integration into ToolRegistry."""

    def test_convert_tool_to_manifest(self, sample_tool):
        """Test converting ToolCapability to InternalToolManifest."""
        manifest = convert_tool_capability_to_manifest(sample_tool)

        assert manifest.tool_id == "tool.test_query"
        assert manifest.tool_version == "1.0.0"
        assert manifest.status == "active"
        assert manifest.risk_level == "low"
        assert manifest.adapter_ref.startswith("adapter://dynamic/")
        assert "test.api" in manifest.adapter_ref
        assert len(manifest.result_schemas) == 1
        assert manifest.result_schemas[0].kind == "table"

    def test_convert_tool_to_descriptor(self, sample_tool):
        """Test converting ToolCapability to ModelToolDescriptor."""
        descriptor = convert_tool_capability_to_descriptor(sample_tool)

        assert descriptor.tool_id == "tool.test_query"
        assert descriptor.tool_version == "1.0.0"
        assert descriptor.name == "Test Query Tool"
        assert "test tool" in descriptor.description.lower()

    @pytest.mark.asyncio
    async def test_load_published_tools(self):
        """Test loading only published tools from repository."""
        repo = InMemoryCapabilityRepository()

        # Create tools with different statuses
        published_tool = ToolCapability(
            capability_id="tool.published",
            name="Published Tool",
            capability_type="tool",
            domain="governance",
            owner="test",
            version="1.0.0",
            status="published",
            connector_ref="test.api",
            resource_path="/api/test",
        )
        draft_tool = ToolCapability(
            capability_id="tool.draft",
            name="Draft Tool",
            capability_type="tool",
            domain="governance",
            owner="test",
            version="1.0.0",
            status="draft",
            connector_ref="test.api",
            resource_path="/api/draft",
        )
        testing_tool = ToolCapability(
            capability_id="tool.testing",
            name="Testing Tool",
            capability_type="tool",
            domain="governance",
            owner="test",
            version="1.0.0",
            status="testing",
            connector_ref="test.api",
            resource_path="/api/testing",
        )

        await repo.save_connector(
            Connector(
                connector_id="test.api",
                name="Test",
                base_url="https://api.example.com",
                allowed_path_prefixes=["/api"],
            )
        )
        await repo.save_tool(published_tool)
        await repo.save_tool(draft_tool)
        await repo.save_tool(testing_tool)

        # Load published tools
        published = await load_published_tools(repo)

        assert len(published) == 1
        assert published[0].capability_id == "tool.published"

    def test_build_dynamic_registry_entries(self, sample_tool):
        """Test building registry entries from published tools."""
        manifests, descriptors = build_dynamic_tool_registry_entries([sample_tool])

        assert len(manifests) == 1
        assert len(descriptors) == 1
        assert manifests[0].tool_id == "tool.test_query"
        assert descriptors[0].tool_id == "tool.test_query"

    def test_tool_registry_merge_dynamic(self):
        """Test merging dynamic tools into static registry."""
        # Create static registry
        static_registry = ToolRegistry.default()
        static_tool_count = len(static_registry.list_tool_ids())

        # Create dynamic tool
        dynamic_tool = ToolCapability(
            capability_id="tool.dynamic",
            name="Dynamic Tool",
            capability_type="tool",
            domain="governance",
            owner="test",
            version="1.0.0",
            status="published",
            connector_ref="test.api",
            resource_path="/api/dynamic",
        )

        manifests, descriptors = build_dynamic_tool_registry_entries([dynamic_tool])

        # Merge into registry
        merged_registry = static_registry.merge_dynamic(
            manifests=manifests,
            descriptors=descriptors,
            dynamic_input_schemas={"tool.dynamic": {"type": "object"}},
        )

        # Verify merge
        merged_tool_ids = merged_registry.list_tool_ids()
        assert len(merged_tool_ids) == static_tool_count + 1
        assert "tool.dynamic" in merged_tool_ids

        # Verify static tools still present
        assert "governance.resolve_area" in merged_tool_ids

        # Verify dynamic tool accessible
        manifest = merged_registry.get_manifest("tool.dynamic")
        assert manifest.tool_id == "tool.dynamic"

        descriptor = merged_registry.get_model_descriptor("tool.dynamic")
        assert descriptor.tool_id == "tool.dynamic"

        # Verify input schema
        schema = merged_registry.get_input_schema("tool.dynamic")
        assert schema["type"] == "object"


class TestHttpConnectorExecutor:
    """Test G4: HTTP connector executor with SSRF protections."""

    @pytest.mark.asyncio
    async def test_path_whitelist_validation(self, sample_connector, sample_tool):
        """Test path whitelist validation."""
        repo = MagicMock()
        repo.get_connector = AsyncMock(return_value=sample_connector)

        executor = HttpConnectorExecutor(repo)

        # Valid path
        executor._validate_path_whitelist(sample_connector, "/api/v1/query")

        # Invalid path
        with pytest.raises(SSRFProtectionError, match="not in whitelist"):
            executor._validate_path_whitelist(sample_connector, "/admin/secret")

        # Path traversal attempt
        with pytest.raises(SSRFProtectionError, match="Path traversal"):
            executor._validate_path_whitelist(
                sample_connector, "/api/v1/../../../etc/passwd"
            )

    def test_url_ssrf_validation(self, sample_connector):
        """Test URL SSRF validation."""
        executor = HttpConnectorExecutor(MagicMock())

        # Valid URL
        executor._validate_url_ssrf(
            "https://api.example.com/test", sample_connector.denied_hosts
        )

        # Invalid scheme
        with pytest.raises(SSRFProtectionError, match="Invalid scheme"):
            executor._validate_url_ssrf(
                "ftp://api.example.com/test", sample_connector.denied_hosts
            )

        # Denied host
        with pytest.raises(SSRFProtectionError, match="is denied"):
            executor._validate_url_ssrf(
                "https://blocked.example.com/test", sample_connector.denied_hosts
            )

        # Localhost
        with pytest.raises(SSRFProtectionError, match="blocked"):
            executor._validate_url_ssrf(
                "http://localhost/test", sample_connector.denied_hosts
            )

        # 127.0.0.1
        with pytest.raises(SSRFProtectionError, match="blocked"):
            executor._validate_url_ssrf(
                "http://127.0.0.1/test", sample_connector.denied_hosts
            )

        # ::1
        with pytest.raises(SSRFProtectionError, match="blocked"):
            executor._validate_url_ssrf(
                "http://[::1]/test", sample_connector.denied_hosts
            )

        # .local domain
        with pytest.raises(SSRFProtectionError, match="not allowed"):
            executor._validate_url_ssrf(
                "http://service.local/test", sample_connector.denied_hosts
            )

        # .internal domain
        with pytest.raises(SSRFProtectionError, match="not allowed"):
            executor._validate_url_ssrf(
                "http://service.internal/test", sample_connector.denied_hosts
            )

    def test_ip_validation(self):
        """Test IP address validation against blocked ranges."""
        executor = HttpConnectorExecutor(MagicMock())

        # Valid public IP
        executor._validate_ip("8.8.8.8")

        # Loopback - caught by blocked network check
        with pytest.raises(SSRFProtectionError, match="blocked range"):
            executor._validate_ip("127.0.0.1")

        # Private IPs
        with pytest.raises(SSRFProtectionError, match="blocked range|private"):
            executor._validate_ip("10.0.0.1")

        with pytest.raises(SSRFProtectionError, match="blocked range|private"):
            executor._validate_ip("172.16.0.1")

        with pytest.raises(SSRFProtectionError, match="blocked range|private"):
            executor._validate_ip("192.168.1.1")

        # Link-local
        with pytest.raises(SSRFProtectionError, match="blocked range|link-local"):
            executor._validate_ip("169.254.1.1")

        # Cloud metadata - caught by blocked network check first
        with pytest.raises(SSRFProtectionError, match="blocked range|Cloud metadata"):
            executor._validate_ip("169.254.169.254")

        # Multicast
        with pytest.raises(SSRFProtectionError, match="blocked range|multicast"):
            executor._validate_ip("224.0.0.1")

    @pytest.mark.asyncio
    async def test_dns_validation(self):
        """Test DNS resolution and IP validation."""
        executor = HttpConnectorExecutor(MagicMock())

        # Mock socket.getaddrinfo to return private IP
        with patch("socket.getaddrinfo") as mock_getaddrinfo:
            mock_getaddrinfo.return_value = [
                (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("10.0.0.1", 0))
            ]

            with pytest.raises(SSRFProtectionError, match="blocked range|private"):
                await executor._validate_dns("http://evil.example.com/test")

        # Mock socket.getaddrinfo to return public IP
        with patch("socket.getaddrinfo") as mock_getaddrinfo:
            mock_getaddrinfo.return_value = [
                (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("8.8.8.8", 0))
            ]

            # Should not raise
            await executor._validate_dns("http://safe.example.com/test")

    def test_parameter_mapping(self):
        """Test parameter mapping transformation."""
        executor = HttpConnectorExecutor(MagicMock())

        # No mapping
        result = executor._apply_parameter_mapping({}, {"query": "test"})
        assert result == {"query": "test"}

        # Simple mapping
        mapping = {"q": "$.query", "limit": "10"}
        result = executor._apply_parameter_mapping(
            mapping, {"query": "test", "other": "value"}
        )
        assert result == {"q": "test", "limit": "10"}

    def test_result_mapping(self):
        """Test result mapping transformation."""
        executor = HttpConnectorExecutor(MagicMock())

        # No mapping
        result = executor._apply_result_mapping({}, {"data": [1, 2, 3]})
        assert result == {"data": [1, 2, 3]}

        # JSONPath-like mapping
        mapping = {"items": "$.data.items", "total": "$.data.total"}
        response = {"data": {"items": [1, 2, 3], "total": 3, "other": "ignored"}}
        result = executor._apply_result_mapping(mapping, response)
        assert result == {"items": [1, 2, 3], "total": 3}


class TestSSRFAttackVectors:
    """Comprehensive SSRF attack vector tests."""

    @pytest.mark.asyncio
    async def test_localhost_attack(self):
        """Test localhost attack vectors."""
        executor = HttpConnectorExecutor(MagicMock())

        attack_urls = [
            "http://localhost/admin",
            "http://127.0.0.1/admin",
            "http://[::1]/admin",
            "http://0.0.0.0/admin",
        ]

        for url in attack_urls:
            with pytest.raises(SSRFProtectionError):
                executor._validate_url_ssrf(url, [])

    @pytest.mark.asyncio
    async def test_private_network_attack(self):
        """Test private network attack vectors."""
        executor = HttpConnectorExecutor(MagicMock())

        # Mock DNS to return private IPs
        with patch("socket.getaddrinfo") as mock_getaddrinfo:
            # 10.0.0.0/8
            mock_getaddrinfo.return_value = [
                (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("10.0.0.1", 0))
            ]
            with pytest.raises(SSRFProtectionError):
                await executor._validate_dns("http://evil.example.com")

            # 172.16.0.0/12
            mock_getaddrinfo.return_value = [
                (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("172.16.0.1", 0))
            ]
            with pytest.raises(SSRFProtectionError):
                await executor._validate_dns("http://evil.example.com")

            # 192.168.0.0/16
            mock_getaddrinfo.return_value = [
                (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("192.168.1.1", 0))
            ]
            with pytest.raises(SSRFProtectionError):
                await executor._validate_dns("http://evil.example.com")

    @pytest.mark.asyncio
    async def test_cloud_metadata_attack(self):
        """Test cloud metadata endpoint attack."""
        executor = HttpConnectorExecutor(MagicMock())

        # Direct IP
        with pytest.raises(SSRFProtectionError):
            executor._validate_ip("169.254.169.254")

        # Via DNS
        with patch("socket.getaddrinfo") as mock_getaddrinfo:
            mock_getaddrinfo.return_value = [
                (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("169.254.169.254", 0))
            ]
            with pytest.raises(SSRFProtectionError):
                await executor._validate_dns("http://metadata.google.internal")

    @pytest.mark.asyncio
    async def test_dns_rebinding_attack(self):
        """Test DNS rebinding attack (TOCTOU)."""
        executor = HttpConnectorExecutor(MagicMock())

        # First resolution returns public IP
        with patch("socket.getaddrinfo") as mock_getaddrinfo:
            mock_getaddrinfo.return_value = [
                (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("8.8.8.8", 0))
            ]
            # Should pass
            await executor._validate_dns("http://evil.example.com")

        # Second resolution returns private IP (rebinding)
        with patch("socket.getaddrinfo") as mock_getaddrinfo:
            mock_getaddrinfo.return_value = [
                (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("10.0.0.1", 0))
            ]
            # Should fail
            with pytest.raises(SSRFProtectionError):
                await executor._validate_dns("http://evil.example.com")

    def test_path_traversal_attack(self, sample_connector):
        """Test path traversal attack vectors."""
        executor = HttpConnectorExecutor(MagicMock())

        attack_paths = [
            "/api/v1/../../../etc/passwd",
            "/api/v1/..%2f..%2f..%2fetc%2fpasswd",
            "/api/v1//..//..//etc//passwd",
            "/api/v1/%2e%2e/%2e%2e/etc/passwd",
        ]

        for path in attack_paths:
            with pytest.raises(SSRFProtectionError):
                executor._validate_path_whitelist(sample_connector, path)

    def test_double_slash_bypass(self, sample_connector):
        """Test double slash bypass attempts."""
        executor = HttpConnectorExecutor(MagicMock())

        # These should fail (not in whitelist)
        with pytest.raises(SSRFProtectionError):
            executor._validate_path_whitelist(sample_connector, "//api/v1/query")

        with pytest.raises(SSRFProtectionError):
            executor._validate_path_whitelist(
                sample_connector, "/api//v1//query"
            )
