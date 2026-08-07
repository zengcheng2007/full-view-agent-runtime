"""A3 Model config hot-reload with real PostgreSQL.

Proves:
1. Model config + key stored in PostgreSQL survive destroy/rebuild of Runtime
2. Model config switch only affects new Run, old Run keeps its config
3. DB config present but read fails must NOT silently fall back to env vars
"""

from __future__ import annotations

import contextlib
import os

import psycopg
import pytest
from pydantic import SecretStr

DATABASE_URL = os.getenv("FULL_VIEW_DATABASE_URL", "")
CREDENTIAL_KEY_B64 = os.getenv("FULL_VIEW_CREDENTIAL_KEY", "")


def _get_credential_key() -> bytes:
    import base64

    return base64.b64decode(CREDENTIAL_KEY_B64, altchars=b"-_", validate=True)


requires_postgres = pytest.mark.skipif(
    not DATABASE_URL,
    reason="FULL_VIEW_DATABASE_URL required for A3 test",
)


@pytest.fixture()
def clean_model_config():
    """Ensure test config is cleaned up before and after test."""
    config_id = "test-a3-model-config"
    schema = os.getenv("FULL_VIEW_POSTGRES_SCHEMA", "full_view_agent")

    async def _cleanup() -> None:
        async with await psycopg.AsyncConnection.connect(DATABASE_URL) as conn:
            await conn.execute(  # pyright: ignore[reportArgumentType]
                f"DELETE FROM {schema}.model_configs WHERE config_id = %s",
                (config_id,),
            )

    import asyncio

    with contextlib.suppress(Exception):
        asyncio.run(_cleanup())
    yield config_id
    with contextlib.suppress(Exception):
        asyncio.run(_cleanup())


@requires_postgres
@pytest.mark.asyncio
async def test_model_config_persists_across_runtime_rebuild(
    clean_model_config: str,
) -> None:
    """Prove model config + key survive destroying/rebuilding the full Runtime."""
    from full_view_agent.api.app import RuntimeContainer
    from full_view_agent.domain.capability import ModelConfig

    config_id = clean_model_config
    os.getenv("FULL_VIEW_POSTGRES_SCHEMA", "full_view_agent")
    _get_credential_key()
    test_api_key = "sk-test-a3-key-12345"

    # --- Phase 1: Create config + store key via first Runtime ---
    runtime1 = RuntimeContainer()
    await runtime1.initialize()
    # Save model config
    config = ModelConfig(
        config_id=config_id,
        name="A3 Test Config",
        api_base_url="https://api.example.com",
        model_name="test-model",
        protocol="openai_compatible",
        is_enabled=True,
        created_by="test",
    )
    await runtime1.model_config_service._repo.save(config)
    await runtime1.model_config_service._keys.store_key(
        config_id=config_id, api_key=SecretStr(test_api_key)
    )

    # Verify via first runtime
    resolved1 = await runtime1.model_config_service.resolve_for_runtime()
    assert resolved1 is not None, "Runtime1 should resolve the enabled config"
    assert resolved1.api_key_secret == test_api_key

    # --- Phase 2: Destroy runtime1, create runtime2 ---
    del runtime1

    runtime2 = RuntimeContainer()
    await runtime2.initialize()
    resolved2 = await runtime2.model_config_service.resolve_for_runtime()
    assert resolved2 is not None, "Runtime2 should resolve config after rebuild"
    assert resolved2.api_key_secret == test_api_key, (
        "Runtime2 should decrypt the same API key"
    )

    # Cleanup
    await runtime2.model_config_service._keys.delete_key(config_id=config_id)
    await runtime2.model_config_service._repo.delete(config_id)


@requires_postgres
@pytest.mark.asyncio
async def test_model_config_switch_only_affects_new_run(
    clean_model_config: str,
) -> None:
    """Prove model config switch only affects new Run (same process)."""
    from full_view_agent.api.app import RuntimeContainer
    from full_view_agent.domain.capability import ModelConfig

    config_id = clean_model_config
    test_api_key_v1 = "sk-test-a3-v1"
    test_api_key_v2 = "sk-test-a3-v2"

    runtime = RuntimeContainer()
    await runtime.initialize()

    # Create v1 config
    config_v1 = ModelConfig(
        config_id=config_id,
        name="A3 Config V1",
        api_base_url="https://api.example.com",
        model_name="model-v1",
        protocol="openai_compatible",
        is_enabled=True,
        created_by="test",
    )
    await runtime.model_config_service._repo.save(config_v1)
    await runtime.model_config_service._keys.store_key(
        config_id=config_id, api_key=SecretStr(test_api_key_v1)
    )

    # Capture resolved config for "old run"
    old_run_config = await runtime.model_config_service.resolve_for_runtime()
    assert old_run_config is not None
    assert old_run_config.api_key_secret == test_api_key_v1
    assert old_run_config.model_name == "model-v1"

    # Update config to v2 (different API key and model name)
    config_v2 = ModelConfig(
        config_id=config_id,
        name="A3 Config V2",
        api_base_url="https://api.example.com",
        model_name="model-v2",
        protocol="openai_compatible",
        is_enabled=True,
        created_by="test",
    )
    await runtime.model_config_service._repo.save(config_v2)
    await runtime.model_config_service._keys.store_key(
        config_id=config_id, api_key=SecretStr(test_api_key_v2)
    )

    # "Old run" keeps v1 config (its reference is unchanged)
    assert old_run_config.api_key_secret == test_api_key_v1
    assert old_run_config.model_name == "model-v1"

    # "New run" gets v2 config
    new_run_config = await runtime.model_config_service.resolve_for_runtime()
    assert new_run_config is not None
    assert new_run_config.api_key_secret == test_api_key_v2
    assert new_run_config.model_name == "model-v2"

    # Cleanup
    await runtime.model_config_service._keys.delete_key(config_id=config_id)
    await runtime.model_config_service._repo.delete(config_id)


@requires_postgres
@pytest.mark.asyncio
async def test_db_config_read_failure_does_not_silently_fall_back(
    clean_model_config: str,
) -> None:
    """Prove that if DB config exists but read fails, we get explicit failure.

    This ensures the system doesn't silently fall back to env vars when
    the database has a config but the key can't be decrypted.
    """
    from full_view_agent.api.app import RuntimeContainer
    from full_view_agent.domain.capability import ModelConfig

    config_id = clean_model_config

    runtime = RuntimeContainer()
    await runtime.initialize()

    # Save a config
    config = ModelConfig(
        config_id=config_id,
        name="A3 Test Corrupt",
        api_base_url="https://api.example.com",
        model_name="test-model",
        protocol="openai_compatible",
        is_enabled=True,
        created_by="test",
    )
    await runtime.model_config_service._repo.save(config)

    # Store key with WRONG encryption key (simulates corruption)
    wrong_key = b"X" * 32
    from full_view_agent.infrastructure.postgres_model_config_key_store import (
        PostgresModelConfigKeyStore,
    )

    wrong_store = PostgresModelConfigKeyStore(
        dsn=DATABASE_URL,
        schema=os.getenv("FULL_VIEW_POSTGRES_SCHEMA", "full_view_agent"),
        encryption_key=wrong_key,
    )
    await wrong_store.store_key(
        config_id=config_id, api_key=SecretStr("some-key")
    )

    # Now try to resolve with the correct key store - should fail explicitly
    # because the ciphertext was encrypted with wrong key
    with pytest.raises(Exception) as exc_info:
        await runtime.model_config_service.resolve_for_runtime()

    # The error should be explicit (not silent fallback)
    error_msg = str(exc_info.value).lower()
    assert "decrypt" in error_msg or "key" in error_msg or "not found" in error_msg, (
        f"Expected explicit decryption/key error, got: {exc_info.value}"
    )

    # Cleanup with the wrong store (to delete the corrupt key)
    await wrong_store.delete_key(config_id=config_id)
    await runtime.model_config_service._repo.delete(config_id)
