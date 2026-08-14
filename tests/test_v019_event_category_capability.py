from pathlib import Path

import psycopg
import pytest

from full_view_agent.infrastructure.postgres_persistence import PostgresAgentPersistence


def _migration_text() -> str:
    return (
        Path(__file__).parents[1]
        / "scripts/migrations/V019_event_category_capability.sql"
    ).read_text(encoding="utf-8")


def test_v019_updates_event_category_contract_idempotently() -> None:
    migration = _migration_text()
    assert "V019" in migration
    assert "event_category" in migration
    assert "event-category-table" in migration
    assert "eventtype_code1" in migration
    assert "SELECT 19" in migration


def test_v019_is_loaded_by_automatic_migrations() -> None:
    persistence = PostgresAgentPersistence(
        dsn="postgresql://unused:unused@127.0.0.1:1/unused",
        schema="fva_test_migration_inventory",
    )
    assert any("V019" in item for item in persistence._p2_migration_statements())


@pytest.mark.db
@pytest.mark.asyncio
async def test_v019_real_pg_exposes_event_category_contract(
    pg_schema: dict[str, str],
) -> None:
    async with await psycopg.AsyncConnection.connect(pg_schema["dsn"]) as connection:
        cursor = await connection.execute(
            f'SELECT input_schema, output_schema FROM "{pg_schema["schema"]}".'
            "capability_tools WHERE capability_id = %s AND version = '1.0.0'",
            ("governance.query_event_metrics",),
        )
        row = await cursor.fetchone()
    assert row is not None
    assert "event_category" in str(row[0])
    assert "event-category-table" in str(row[1])
