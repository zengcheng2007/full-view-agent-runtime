"""A3 Model config hot-reload with real PostgreSQL.

Proves:
1. Model config + key stored in PostgreSQL survive destroy/rebuild of Runtime
2. Model config switch only affects new Run, old Run keeps its config
3. DB config present but read fails must NOT silently fall back to env vars
"""

from __future__ import annotations

import os

import pytest
from pydantic import SecretStr

from tests.model_resource_helpers import publish_tested_model

pytestmark = pytest.mark.db
DATABASE_URL = os.getenv("FULL_VIEW_TEST_DATABASE_URL", "")
CREDENTIAL_KEY_B64 = os.getenv("FULL_VIEW_TEST_CREDENTIAL_KEY", "")


def _get_credential_key() -> bytes:
    import base64

    return base64.b64decode(CREDENTIAL_KEY_B64, altchars=b"-_", validate=True)


requires_postgres = pytest.mark.skipif(
    not DATABASE_URL,
    reason="FULL_VIEW_TEST_DATABASE_URL required for A3 test",
)


@requires_postgres
@pytest.mark.asyncio
async def test_model_config_persists_across_runtime_rebuild() -> None:
    """Prove model config + key survive destroying/rebuilding the full Runtime."""
    from full_view_agent.api.app import RuntimeContainer
    os.getenv("FULL_VIEW_POSTGRES_SCHEMA", "full_view_agent")
    _get_credential_key()
    test_api_key = "sk-test-a3-key-12345"

    # --- Phase 1: Create config + store key via first Runtime ---
    runtime1 = RuntimeContainer()
    await runtime1.initialize()
    config = await publish_tested_model(
        runtime1.model_config_service,
        name="A3 Test Config",
        api_base_url="https://api.example.com",
        api_key=test_api_key,
        model_name="test-model",
        legacy_default=True,
    )
    config_id = config.config_id

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
async def test_model_config_switch_only_affects_new_run() -> None:
    """Prove model config switch only affects new Run (same process)."""
    from full_view_agent.api.app import RuntimeContainer
    test_api_key_v1 = "sk-test-a3-v1"
    test_api_key_v2 = "sk-test-a3-v2"

    runtime = RuntimeContainer()
    await runtime.initialize()

    config_v1 = await publish_tested_model(
        runtime.model_config_service,
        name="A3 Config V1",
        api_base_url="https://api.example.com",
        api_key=test_api_key_v1,
        model_name="model-v1",
        legacy_default=True,
    )

    # Capture resolved config for "old run"
    old_run_config = await runtime.model_config_service.resolve_for_runtime()
    assert old_run_config is not None
    assert old_run_config.api_key_secret == test_api_key_v1
    assert old_run_config.model_name == "model-v1"

    # Publish v2 as a separate immutable resource, then explicitly switch the
    # legacy default. Existing Runs retain their already-resolved v1 snapshot.
    config_v2 = await publish_tested_model(
        runtime.model_config_service,
        name="A3 Config V2",
        api_base_url="https://api.example.com",
        api_key=test_api_key_v2,
        model_name="model-v2",
        legacy_default=True,
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
    for config_id in (config_v1.config_id, config_v2.config_id):
        await runtime.model_config_service._keys.delete_key(config_id=config_id)
        await runtime.model_config_service._repo.delete(config_id)


@requires_postgres
@pytest.mark.asyncio
async def test_db_config_read_failure_does_not_silently_fall_back() -> None:
    """Prove that if DB config exists but read fails, we get explicit failure.

    This ensures the system doesn't silently fall back to env vars when
    the database has a config but the key can't be decrypted.
    """
    from full_view_agent.api.app import RuntimeContainer
    runtime = RuntimeContainer()
    await runtime.initialize()

    config = await publish_tested_model(
        runtime.model_config_service,
        name="A3 Test Corrupt",
        api_base_url="https://api.example.com",
        api_key="initial-valid-key",
        model_name="test-model",
        legacy_default=True,
    )
    config_id = config.config_id

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
