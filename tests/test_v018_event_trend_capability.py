"""V018 PostgreSQL registry coverage for the event monthly trend shape."""

from __future__ import annotations

from pathlib import Path

import psycopg
import pytest

from full_view_agent.infrastructure.postgres_persistence import PostgresAgentPersistence


def _migration_text() -> str:
    return (
        Path(__file__).parents[1]
        / "scripts/migrations/V018_event_trend_capability.sql"
    ).read_text(encoding="utf-8")


def test_v018_updates_event_capability_contract_idempotently() -> None:
    migration = _migration_text()

    assert "V018" in migration
    assert "governance.query_event_metrics" in migration
    assert "event_count" in migration
    assert '"month"' in migration
    assert '"time_range"' in migration
    assert "schema://data/event-trend-table/1.0.0" in migration
    assert "SELECT 18" in migration


def test_v018_is_loaded_by_in_process_automatic_migrations() -> None:
    persistence = PostgresAgentPersistence(
        dsn="postgresql://unused:unused@127.0.0.1:1/unused",
        schema="fva_test_migration_inventory",
    )

    assert any(
        "V018" in migration and "event-trend-table" in migration
        for migration in persistence._p2_migration_statements()
    )


@pytest.mark.db
@pytest.mark.asyncio
async def test_v018_real_pg_exposes_event_trend_input_and_schema(
    pg_schema: dict[str, str],
) -> None:
    async with await psycopg.AsyncConnection.connect(pg_schema["dsn"]) as connection:
        cursor = await connection.execute(
            f'SELECT input_schema, data_schema_ref FROM "{pg_schema["schema"]}".'
            "capability_tools WHERE capability_id = %s AND version = '1.0.0'",
            ("governance.query_event_metrics",),
        )
        row = await cursor.fetchone()

    assert row is not None
    assert row[0]["properties"]["query"]["properties"]["time_range"]
    assert row[1] == "schema://data/event-finish-rate-table/1.0.0"
