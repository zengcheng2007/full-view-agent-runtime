#!/usr/bin/env python
"""R2G3 Verification: Prove API key restart persistence.

This script demonstrates that:
1. API keys are stored in PostgreSQL (not in-memory)
2. Destroying and recreating the key store still retrieves the key
3. Keys survive process restarts
"""

import asyncio
import inspect
import os
import sys

from pydantic import SecretStr


def _print(msg: str) -> None:
    """Print with encoding-safe fallback for Windows GBK consoles."""
    try:
        print(msg)
    except UnicodeEncodeError:
        print(msg.encode("ascii", "replace").decode("ascii"))


async def verify_restart_persistence() -> bool:
    """Verify that API keys persist across key store recreation."""
    _print("=" * 70)
    _print("R2G3 Verification: API Key Restart Persistence")
    _print("=" * 70)

    # Check if we have database URL
    database_url = os.getenv("FULL_VIEW_DATABASE_URL")
    credential_key_str = os.getenv("FULL_VIEW_CREDENTIAL_KEY")

    if not database_url:
        _print("[FAIL] FULL_VIEW_DATABASE_URL not set")
        _print("       This verification REQUIRES a real PostgreSQL instance.")
        _print("       Architecture-only checks are not accepted.")
        return False

    if not credential_key_str:
        _print("[WARN] FULL_VIEW_CREDENTIAL_KEY not set")
        _print("       Using 32-byte test key for verification")
        credential_key = b"0123456789abcdef0123456789abcdef"
    else:
        import base64

        credential_key = base64.b64decode(
            credential_key_str, altchars=b"-_", validate=True
        )

    _print("\n1. Creating first key store instance...")
    from full_view_agent.infrastructure.postgres_model_config_key_store import (
        PostgresModelConfigKeyStore,
    )

    key_store_1 = PostgresModelConfigKeyStore(
        dsn=database_url,
        schema="full_view_agent",
        encryption_key=credential_key,
    )

    # First, ensure a model config exists
    from full_view_agent.infrastructure.capability_repository import (
        PostgresModelConfigRepository,
    )

    repo = PostgresModelConfigRepository(
        dsn=database_url,
        schema="full_view_agent",
    )

    # Create a test config
    from full_view_agent.domain.capability import ModelConfig

    test_config = ModelConfig(
        config_id="test-r2g3-persistence",
        name="R2G3 Test Config",
        api_base_url="https://api.example.com",
        model_name="test-model",
        protocol="openai_compatible",
        is_enabled=False,
        created_by="test",
    )

    try:
        await repo.save(test_config)
        _print("   [OK] Test config created")
    except Exception as e:
        _print(f"   [WARN] Config may already exist: {e}")

    # Store a test API key
    test_api_key = SecretStr("test-api-key-r2g3-verification-12345")
    _print("\n2. Storing API key in first key store...")
    await key_store_1.store_key(
        config_id="test-r2g3-persistence",
        api_key=test_api_key,
    )
    _print("   [OK] API key stored in PostgreSQL")

    # Retrieve from first instance
    retrieved_key_1 = await key_store_1.resolve_key(config_id="test-r2g3-persistence")
    assert retrieved_key_1.get_secret_value() == test_api_key.get_secret_value()
    _print("   [OK] Key retrieved from first instance")

    # Simulate process restart by creating a new key store instance
    _print("\n3. Simulating process restart (creating new key store instance)...")
    key_store_2 = PostgresModelConfigKeyStore(
        dsn=database_url,
        schema="full_view_agent",
        encryption_key=credential_key,
    )

    # Retrieve from second instance (simulates after restart)
    _print("4. Retrieving API key from second key store instance...")
    retrieved_key_2 = await key_store_2.resolve_key(config_id="test-r2g3-persistence")

    # Verify the key is the same
    assert retrieved_key_2.get_secret_value() == test_api_key.get_secret_value()
    _print("   [OK] Key retrieved from second instance (after simulated restart)")
    _print("   [OK] Keys match: persistence verified")

    # Clean up
    _print("\n5. Cleaning up...")
    await key_store_2.delete_key(config_id="test-r2g3-persistence")
    await repo.delete("test-r2g3-persistence")
    _print("   [OK] Test data cleaned up")

    _print("\n" + "=" * 70)
    _print("[PASS] R2G3 VERIFIED: API keys persist across restarts")
    _print("=" * 70)
    _print("\nKey Points:")
    _print("  - PostgresModelConfigKeyStore stores encrypted keys in PostgreSQL")
    _print("  - Keys survive process restarts (not in-memory)")
    _print("  - Destroying/recreating key store still retrieves keys")
    _print("  - Uses AES-GCM encryption with config_id as AAD")

    return True


async def verify_architecture() -> bool:
    """Verify the architecture is correct even without database."""
    _print("\n1. Verifying PostgresModelConfigKeyStore architecture...")

    from full_view_agent.infrastructure.postgres_model_config_key_store import (
        PostgresModelConfigKeyStore,
    )

    # Verify the class exists and has the right methods
    assert hasattr(PostgresModelConfigKeyStore, "store_key")
    assert hasattr(PostgresModelConfigKeyStore, "resolve_key")
    assert hasattr(PostgresModelConfigKeyStore, "delete_key")
    _print("   [OK] PostgresModelConfigKeyStore has required methods")

    # Verify it's wired in app.py
    from full_view_agent.api.app import RuntimeContainer

    source = inspect.getsource(RuntimeContainer.__post_init__)
    assert "PostgresModelConfigKeyStore" in source
    _print("   [OK] PostgresModelConfigKeyStore is wired in RuntimeContainer")

    _print("\n" + "=" * 70)
    _print("[INFO] R2G3 ARCHITECTURE VERIFIED (database not tested)")
    _print("=" * 70)
    _print("\nTo fully verify restart persistence, set FULL_VIEW_DATABASE_URL")
    _print("and run this script again with a real PostgreSQL instance.")

    return False  # Architecture-only is not a pass for R2G3


async def main() -> int:
    """Run R2G3 verification."""
    try:
        database_url = os.getenv("FULL_VIEW_DATABASE_URL")
        if database_url:
            result = await verify_restart_persistence()
        else:
            _print("[INFO] No database URL; running architecture check only (not a pass)")
            result = await verify_architecture()
        _print("\nR2G3_STATUS=VERIFIED" if result else "\nR2G3_STATUS=NOT_VERIFIED")
        return 0 if result else 1
    except Exception as e:
        _print(f"\n[FAIL] R2G3 verification failed: {e}")
        import traceback

        traceback.print_exc()
        return 1


if __name__ == "__main__":
    # psycopg async requires SelectorEventLoop on Windows
    if sys.platform == "win32":
        import asyncio

        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
