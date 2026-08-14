"""Directed tests that verify missing-table errors are NOT silenced.

The production code MUST raise on missing ``run_capability_snapshots``
/ ``capability_tools`` tables — silently treating "table missing" as
"no data" would let an un-migrated production deployment appear to
work while actually violating the Run-pinned-capability semantic.

These tests use the isolated test database and create a schema that
deliberately has no V010/V012 tables, then assert that the production
repositories raise ``UndefinedTable`` rather than returning empty.
"""

from __future__ import annotations

import os

import psycopg
import pytest

from full_view_agent.application.run_capability_snapshot_store import (
    PersistedRunCapabilitySnapshot,
    PostgresRunCapabilitySnapshotStore,
)
from full_view_agent.infrastructure.capability_repository import (
    PostgresCapabilityRepository,
)

pytestmark = pytest.mark.db

DATABASE_URL = os.getenv("FULL_VIEW_TEST_DATABASE_URL", "")

requires_postgres = pytest.mark.skipif(
    not DATABASE_URL,
    reason="FULL_VIEW_TEST_DATABASE_URL required",
)


@pytest.fixture()
async def empty_schema():
    """Create a throwaway schema with no capability/snapshot tables."""
    import uuid

    schema_name = f"fva_empty_{uuid.uuid4().hex[:12]}"
    async with await psycopg.AsyncConnection.connect(DATABASE_URL) as conn:
        await conn.execute(f'CREATE SCHEMA "{schema_name}"')
    try:
        yield schema_name
    finally:
        try:
            async with await psycopg.AsyncConnection.connect(DATABASE_URL) as conn:
                await conn.execute(f'DROP SCHEMA "{schema_name}" CASCADE')
        except Exception:
            pass


@requires_postgres
@pytest.mark.asyncio
async def test_capability_repository_raises_when_tools_table_missing(
    empty_schema: str,
) -> None:
    """PostgresCapabilityRepository.list_capabilities must raise when
    the capability_tools table does not exist — never silently return
    an empty list (that would mask a migration failure).
    """
    from psycopg.errors import UndefinedTable

    repo = PostgresCapabilityRepository(dsn=DATABASE_URL, schema=empty_schema)
    with pytest.raises(UndefinedTable):
        await repo.list_capabilities(capability_type="tool")


@requires_postgres
@pytest.mark.asyncio
async def test_capability_snapshot_store_load_raises_when_table_missing(
    empty_schema: str,
) -> None:
    """PostgresRunCapabilitySnapshotStore.load must raise when the
    run_capability_snapshots table does not exist.
    """
    from psycopg.errors import UndefinedTable

    store = PostgresRunCapabilitySnapshotStore(
        dsn=DATABASE_URL, schema=empty_schema
    )
    with pytest.raises(UndefinedTable):
        await store.load("run-x")


@requires_postgres
@pytest.mark.asyncio
async def test_capability_snapshot_store_delete_raises_when_table_missing(
    empty_schema: str,
) -> None:
    """PostgresRunCapabilitySnapshotStore.delete must raise when the
    run_capability_snapshots table does not exist.
    """
    from psycopg.errors import UndefinedTable

    store = PostgresRunCapabilitySnapshotStore(
        dsn=DATABASE_URL, schema=empty_schema
    )
    with pytest.raises(UndefinedTable):
        await store.delete("run-x")


@requires_postgres
@pytest.mark.asyncio
async def test_capability_snapshot_store_store_raises_when_table_missing(
    empty_schema: str,
) -> None:
    """PostgresRunCapabilitySnapshotStore.store_if_absent must raise when
    the run_capability_snapshots table does not exist.
    """
    from datetime import UTC, datetime

    from psycopg.errors import UndefinedTable

    store = PostgresRunCapabilitySnapshotStore(
        dsn=DATABASE_URL, schema=empty_schema
    )
    snapshot = PersistedRunCapabilitySnapshot(
        run_id="run-x",
        tool_versions={"governance.t1": "1.0.0"},
        captured_at=datetime.now(UTC),
    )
    with pytest.raises(UndefinedTable):
        await store.store_if_absent(snapshot)
