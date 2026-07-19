"""Integration test for cleanup_expired against real PostgreSQL."""

import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from full_view_agent.infrastructure.postgres_persistence import PostgresAgentPersistence
from scripts.cleanup_expired import cleanup_expired

pg_dsn = os.environ.get("FULL_VIEW_TEST_DATABASE_URL", "")

pytestmark = pytest.mark.skipif(
    not pg_dsn,
    reason="FULL_VIEW_TEST_DATABASE_URL not set",
)


@pytest.mark.asyncio
async def test_cleanup_deletes_receipts_via_commands_and_evidence_via_results() -> None:
    """Create a full run lifecycle, then verify cleanup_expired deletes all related rows."""
    schema = f"fva_cleanup_{uuid4().hex[:12]}"
    store = PostgresAgentPersistence(dsn=pg_dsn, schema=schema)
    await store.initialize()
    try:
        import psycopg

        async with await psycopg.AsyncConnection.connect(pg_dsn) as conn:
            prefix = f'"{schema}".'

            run_id = "run-cleanup-01"
            result_id = "res-cleanup-01"
            evidence_id = "evd-cleanup-01"
            command_id = "cmd-cleanup-01"
            past = (datetime.now(UTC) - timedelta(days=60)).isoformat()

            await conn.execute(
                f"INSERT INTO {prefix}sessions "
                "(session_id, owner_user_id, data_json) VALUES (%s, %s, %s)",
                ("ses-cleanup", "user-cleanup", "{}"),
            )
            await conn.execute(
                f"INSERT INTO {prefix}runs (run_id, session_id, data_json) "
                "VALUES (%s, %s, %s)",
                (
                    run_id,
                    "ses-cleanup",
                    f'{{"status":"completed","outcome":"success",'
                    f'"completed_at":"{past}"}}',
                ),
            )
            await conn.execute(
                f"INSERT INTO {prefix}results (result_id, run_id, data_json) "
                "VALUES (%s, %s, %s)",
                (result_id, run_id, "{}"),
            )
            await conn.execute(
                f"INSERT INTO {prefix}evidence "
                "(evidence_id, result_id, data_json) VALUES (%s, %s, %s)",
                (evidence_id, result_id, "{}"),
            )
            await conn.execute(
                f"INSERT INTO {prefix}frontend_commands "
                "(command_id, run_id, data_json) VALUES (%s, %s, %s)",
                (command_id, run_id, "{}"),
            )
            await conn.execute(
                f"INSERT INTO {prefix}frontend_command_receipts "
                "(command_id, client_instance_id, data_json) VALUES (%s, %s, %s)",
                (command_id, "cli-01", "{}"),
            )
            await conn.execute(
                f"INSERT INTO {prefix}messages "
                "(message_id, session_id, run_id, created_at, data_json) "
                "VALUES (%s, %s, %s, %s, %s)",
                ("msg-cleanup", "ses-cleanup", run_id, past, "{}"),
            )
            await conn.commit()

        # Run the actual cleanup function (not duplicated SQL)
        counts = await cleanup_expired(pg_dsn, schema=schema, retention_days=30)
        assert counts["terminal_runs"] == 1

        # Verify all related rows are deleted
        async with await psycopg.AsyncConnection.connect(pg_dsn) as conn:
            prefix = f'"{schema}".'
            for table in (
                "frontend_command_receipts",
                "frontend_commands",
                "evidence",
                "results",
                "messages",
                "runs",
            ):
                count = await conn.execute(
                    f"SELECT COUNT(*) FROM {prefix}{table}"
                )
                assert (await count.fetchone())[0] == 0, f"{table} should be empty"
    finally:
        await store.drop_schema()
