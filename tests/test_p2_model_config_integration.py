"""P2 Integration test: Model config controls new Runs (G2).

Tests that model config from database is used when creating ModelProvider at startup.
"""

from __future__ import annotations

import asyncio

import pytest

from full_view_agent.application.errors import RunStateConflict
from full_view_agent.application.model_config_service import (
    EncryptedModelConfigKeyStore,
    InMemoryModelConfigKeyStore,
    ModelConfigService,
)
from full_view_agent.domain.capability import ModelConfig
from full_view_agent.infrastructure.capability_repository import (
    InMemoryModelConfigRepository,
)


@pytest.fixture
def event_loop():
    """Create an event loop for async tests."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


class TestModelConfigIntegration:
    """Test G2: Model config controls new Runs."""

    @pytest.mark.asyncio
    async def test_resolve_for_runtime_with_enabled_config(self):
        """Test that resolve_for_runtime returns enabled config."""
        from pydantic import SecretStr

        repo = InMemoryModelConfigRepository()
        key_store = InMemoryModelConfigKeyStore()
        service = ModelConfigService(repository=repo, key_store=key_store)

        # Create and enable a config
        config = ModelConfig(
            config_id="test.config",
            name="Test Config",
            api_base_url="https://api.example.com/v1",
            model_name="gpt-4",
            protocol="openai_compatible",
            timeout_seconds=60,
            max_output_tokens=32000,
            max_retries=2,
            is_enabled=True,
        )
        await repo.save(config)
        await key_store.store_key(config_id="test.config", api_key=SecretStr("test-api-key-12345"))

        # Resolve for runtime
        resolved = await service.resolve_for_runtime()

        assert resolved is not None
        assert resolved.config_id == "test.config"
        assert resolved.model_name == "gpt-4"
        assert resolved.api_base_url == "https://api.example.com/v1"
        assert resolved.api_key_secret == "test-api-key-12345"
        assert resolved.timeout_seconds == 60
        assert resolved.is_enabled is True

    @pytest.mark.asyncio
    async def test_resolve_for_runtime_no_enabled_config(self):
        """Test that resolve_for_runtime returns None when no config is enabled."""
        repo = InMemoryModelConfigRepository()
        key_store = InMemoryModelConfigKeyStore()
        service = ModelConfigService(repository=repo, key_store=key_store)

        # Create a disabled config
        config = ModelConfig(
            config_id="test.config",
            name="Test Config",
            api_base_url="https://api.example.com/v1",
            model_name="gpt-4",
            protocol="openai_compatible",
            is_enabled=False,
        )
        await repo.save(config)

        # Resolve for runtime
        resolved = await service.resolve_for_runtime()

        assert resolved is None

    @pytest.mark.asyncio
    async def test_resolve_for_runtime_multiple_configs(self):
        """Legacy runtime refuses to choose arbitrarily from the public pool."""
        from pydantic import SecretStr

        repo = InMemoryModelConfigRepository()
        key_store = InMemoryModelConfigKeyStore()
        service = ModelConfigService(repository=repo, key_store=key_store)

        # Create multiple configs
        config1 = ModelConfig(
            config_id="config.1",
            name="Config 1",
            api_base_url="https://api1.example.com/v1",
            model_name="gpt-4",
            protocol="openai_compatible",
            is_enabled=False,
        )
        config2 = ModelConfig(
            config_id="config.2",
            name="Config 2",
            api_base_url="https://api2.example.com/v1",
            model_name="gpt-3.5-turbo",
            protocol="openai_compatible",
            is_enabled=True,
        )
        config3 = ModelConfig(
            config_id="config.3",
            name="Config 3",
            api_base_url="https://api3.example.com/v1",
            model_name="claude-3",
            protocol="openai_compatible",
            is_enabled=True,
        )

        await repo.save(config1)
        await repo.save(config2)
        await repo.save(config3)
        await key_store.store_key(config_id="config.2", api_key=SecretStr("key-2"))
        await key_store.store_key(config_id="config.3", api_key=SecretStr("key-3"))

        with pytest.raises(RunStateConflict, match="default model"):
            await service.resolve_for_runtime()

    @pytest.mark.asyncio
    async def test_encrypted_key_store_persistence(self):
        """Test that encrypted key store persists keys."""
        from pydantic import SecretStr

        # Use a 32-byte key for AES-256
        encryption_key = b"0123456789abcdef0123456789abcdef"
        key_store = EncryptedModelConfigKeyStore(encryption_key=encryption_key)

        # Store a key
        await key_store.store_key(config_id="test.config", api_key=SecretStr("my-secret-api-key"))

        # Retrieve the key
        retrieved = await key_store.resolve_key(config_id="test.config")

        assert retrieved.get_secret_value() == "my-secret-api-key"

    @pytest.mark.asyncio
    async def test_model_config_service_enable_disable(self):
        """Test enabling and disabling model configs."""
        from pydantic import SecretStr

        repo = InMemoryModelConfigRepository()
        key_store = InMemoryModelConfigKeyStore()
        service = ModelConfigService(repository=repo, key_store=key_store)

        # Create a config
        config = ModelConfig(
            config_id="test.config",
            name="Test Config",
            api_base_url="https://api.example.com/v1",
            model_name="gpt-4",
            protocol="openai_compatible",
            is_enabled=False,
        )
        await repo.save(config)
        await key_store.store_key(
            config_id="test.config", api_key=SecretStr("test-key")
        )

        # Initially disabled
        resolved = await service.resolve_for_runtime()
        assert resolved is None

        # Enable it (keyword-only args, no changed_by parameter)
        await service.enable_config(config_id="test.config")

        # Now should be resolved
        resolved = await service.resolve_for_runtime()
        assert resolved is not None
        assert resolved.config_id == "test.config"
        assert resolved.api_key_secret == "test-key"

        # Disable it
        await service.disable_config(config_id="test.config")

        # Should be disabled again
        resolved = await service.resolve_for_runtime()
        assert resolved is None
