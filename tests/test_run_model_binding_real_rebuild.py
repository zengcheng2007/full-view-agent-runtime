"""Real-rebuild tests for Run -> model config binding.

Proves that the binding + immutable snapshot survive a process restart:
a fresh repository instance (built from the same DB) loads the same
(config_id, config_version) + snapshot as the original, so the old Run
keeps its original config even when the runtime default has moved on.

These tests require a real PostgreSQL instance (set
``FULL_VIEW_DATABASE_URL``). They are skipped in no-DB CI.
"""

from __future__ import annotations

import os

import psycopg
import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from full_view_agent.application.model_config_repository import (
    ModelConfigSnapshot,
    PostgresRunModelBindingRepository,
)
from full_view_agent.domain.capability import ModelConfig
from full_view_agent.infrastructure.capability_repository import (
    PostgresModelConfigRepository,
)

pytestmark = pytest.mark.db

DATABASE_URL = os.getenv("FULL_VIEW_TEST_DATABASE_URL", "")
CREDENTIAL_KEY_B64 = os.getenv("FULL_VIEW_TEST_CREDENTIAL_KEY", "")


def _get_credential_key() -> bytes:
    import base64

    return base64.b64decode(CREDENTIAL_KEY_B64, altchars=b"-_", validate=True)


requires_postgres = pytest.mark.skipif(
    not DATABASE_URL,
    reason="FULL_VIEW_TEST_DATABASE_URL required for real-rebuild test",
)


@pytest.fixture()
async def clean_binding(pg_schema):
    """Clean up the test run's binding + snapshot rows before / after.

    Uses the ``pg_schema`` fixture so cleanup runs against the
    throwaway schema (not the shared catalog).
    """
    run_id = "run-real-rebuild-1"
    config_id = "mconf-real-rebuild"
    schema = pg_schema["schema"]
    dsn = pg_schema["dsn"]

    async def _cleanup() -> None:
        async with await psycopg.AsyncConnection.connect(dsn) as conn:
            await conn.execute(
                f'DELETE FROM "{schema}".run_model_bindings WHERE run_id = %s',
                (run_id,),
            )
            await conn.execute(
                f'DELETE FROM "{schema}".run_model_config_snapshots'
                f" WHERE config_id = %s",
                (config_id,),
            )
            await conn.execute(
                f'DELETE FROM "{schema}".model_configs WHERE config_id = %s',
                (config_id,),
            )

    await _cleanup()
    yield pg_schema
    await _cleanup()


@requires_postgres
@pytest.mark.asyncio
async def test_real_rebuild_repository_survives_process_restart(
    clean_binding: dict
) -> None:
    """Simulate a process restart by rebuilding the repository from the DB.

    Phase 1: bind run-1 to config v1 using repository A.
    Phase 2: "restart" — build a brand new repository B from the same DB.
    Phase 3: load the binding via B — must return the same (config_id, version).
    Phase 4: load the snapshot via B — must return the same metadata + key material.
    """
    encryption_key = _get_credential_key()

    # ── Phase 1: bind via repository A ────────────────────────────────
    repo_a = PostgresRunModelBindingRepository(
        dsn=clean_binding["dsn"], schema=clean_binding["schema"]
    )
    # Encrypt a real key to put in the snapshot.
    cipher = AESGCM(encryption_key)
    nonce = os.urandom(12)
    ciphertext = cipher.encrypt(
        nonce, b"super-secret-v1", b"mconf-real-rebuild"
    )
    snapshot_v1 = ModelConfigSnapshot(
        config_id="mconf-real-rebuild",
        config_version=1,
        name="real-rebuild-v1",
        api_base_url="https://v1.example/v1",
        model_name="model-v1",
        protocol="openai_compatible",
        timeout_seconds=60,
        max_output_tokens=32000,
        max_retries=1,
        api_key_ciphertext=ciphertext,
        api_key_nonce=nonce,
    )
    await repo_a.store_binding("run-real-rebuild-1", snapshot_v1)

    # ── Phase 2: "restart" — fresh repository instance ────────────────
    repo_b = PostgresRunModelBindingRepository(
        dsn=clean_binding["dsn"], schema=clean_binding["schema"]
    )

    # ── Phase 3: load binding via repo B ──────────────────────────────
    binding = await repo_b.load_binding("run-real-rebuild-1")
    assert binding is not None
    assert binding.config_id == "mconf-real-rebuild"
    assert binding.config_version == 1

    # ── Phase 4: load snapshot via repo B ─────────────────────────────
    loaded = await repo_b.load_snapshot(
        config_id=binding.config_id, config_version=binding.config_version
    )
    assert loaded is not None
    assert loaded.name == "real-rebuild-v1"
    assert loaded.api_base_url == "https://v1.example/v1"
    assert loaded.model_name == "model-v1"
    # Decrypt the captured ciphertext — should yield the original key.
    decrypted = cipher.decrypt(
        loaded.api_key_nonce,
        loaded.api_key_ciphertext,
        loaded.config_id.encode("utf-8"),
    )
    assert decrypted == b"super-secret-v1"


@requires_postgres
@pytest.mark.asyncio
async def test_real_rebuild_config_overwrite_does_not_affect_bound_run(
    clean_binding: dict
) -> None:
    """When the same config_id is overwritten with v2, old Runs still read v1.

    This is the "immutable versions or fail closed" guarantee: the
    snapshot row for v1 is not touched by the v2 overwrite.
    """
    encryption_key = _get_credential_key()
    cipher = AESGCM(encryption_key)

    # Insert the v1 config row + binding.
    dsn = clean_binding["dsn"]
    schema = clean_binding["schema"]
    config_repo = PostgresModelConfigRepository(dsn=dsn, schema=schema)
    binding_repo = PostgresRunModelBindingRepository(dsn=dsn, schema=schema)

    config_v1 = ModelConfig(
        config_id="mconf-real-rebuild",
        name="real-rebuild-v1",
        api_base_url="https://v1.example/v1",
        model_name="model-v1",
        protocol="openai_compatible",
        timeout_seconds=60,
        max_output_tokens=32000,
        max_retries=1,
        is_enabled=True,
    )
    await config_repo.save(config_v1)
    # Store v1 key.
    nonce_v1 = os.urandom(12)
    ciphertext_v1 = cipher.encrypt(
        nonce_v1, b"super-secret-v1", b"mconf-real-rebuild"
    )
    snapshot_v1 = ModelConfigSnapshot(
        config_id="mconf-real-rebuild",
        config_version=1,
        name="real-rebuild-v1",
        api_base_url="https://v1.example/v1",
        model_name="model-v1",
        protocol="openai_compatible",
        timeout_seconds=60,
        max_output_tokens=32000,
        max_retries=1,
        api_key_ciphertext=ciphertext_v1,
        api_key_nonce=nonce_v1,
    )
    await binding_repo.store_binding("run-real-rebuild-1", snapshot_v1)

    # Now overwrite config with v2 (same config_id, higher version).
    config_v2 = config_v1.model_copy(
        update={
            "version": 2,
            "name": "real-rebuild-v2",
            "model_name": "model-v2",
            "api_base_url": "https://v2.example/v1",
        }
    )
    await config_repo.save(config_v2)
    # Rotate the key on the source row.
    nonce_v2 = os.urandom(12)
    ciphertext_v2 = cipher.encrypt(
        nonce_v2, b"super-secret-v2", b"mconf-real-rebuild"
    )
    async with await psycopg.AsyncConnection.connect(clean_binding["dsn"]) as conn:
        schema = clean_binding["schema"]
        await conn.execute(
            f'UPDATE "{schema}".model_configs'
            f" SET api_key_ciphertext = %s, api_key_nonce = %s"
            f" WHERE config_id = %s",
            (ciphertext_v2, nonce_v2, "mconf-real-rebuild"),
        )

    # "Restart" — fresh binding repository.
    binding_repo_b = PostgresRunModelBindingRepository(
        dsn=clean_binding["dsn"], schema=clean_binding["schema"]
    )
    binding = await binding_repo_b.load_binding("run-real-rebuild-1")
    assert binding is not None
    assert binding.config_version == 1  # still bound to v1

    loaded = await binding_repo_b.load_snapshot(
        config_id=binding.config_id, config_version=binding.config_version
    )
    assert loaded is not None
    # The snapshot still carries v1 metadata + v1 ciphertext.
    assert loaded.name == "real-rebuild-v1"
    assert loaded.model_name == "model-v1"
    decrypted = cipher.decrypt(
        loaded.api_key_nonce,
        loaded.api_key_ciphertext,
        loaded.config_id.encode("utf-8"),
    )
    assert decrypted == b"super-secret-v1"  # v1 key, not v2


@requires_postgres
@pytest.mark.asyncio
async def test_real_rebuild_missing_snapshot_fails_closed(
    clean_binding: dict
) -> None:
    """If a binding points to a missing snapshot, load_snapshot returns None.

    The caller (RunBoundModelPlannerFactory) raises rather than
    silently substitute a newer config.
    """
    binding_repo = PostgresRunModelBindingRepository(
        dsn=clean_binding["dsn"], schema=clean_binding["schema"]
    )

    # Manually insert a binding with no matching snapshot.
    async with await psycopg.AsyncConnection.connect(clean_binding["dsn"]) as conn:
        schema = clean_binding["schema"]
        await conn.execute(
            f'INSERT INTO "{schema}".run_model_bindings'
            f" (run_id, config_id, config_version)"
            f" VALUES (%s, %s, %s)"
            f" ON CONFLICT (run_id) DO NOTHING",
            ("run-real-rebuild-1", "mconf-nonexistent", 99),
        )

    binding = await binding_repo.load_binding("run-real-rebuild-1")
    assert binding is not None
    snapshot = await binding_repo.load_snapshot(
        config_id=binding.config_id, config_version=binding.config_version
    )
    # The snapshot is missing — the caller must fail closed.
    assert snapshot is None
