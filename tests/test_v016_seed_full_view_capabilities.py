"""V016 production-registry seed coverage for the first two D-line tools."""

from __future__ import annotations

from pathlib import Path

import psycopg
import pytest

from full_view_agent.application.run_capability_snapshot import (
    RunCapabilitySnapshotService,
)
from full_view_agent.application.tool_registry import ToolRegistry
from full_view_agent.infrastructure.application_registry import (
    PostgresApplicationRegistry,
)
from full_view_agent.infrastructure.capability_repository import (
    PostgresCapabilityRepository,
)
from full_view_agent.infrastructure.postgres_persistence import (
    PostgresAgentPersistence,
)

SEEDED_TOOL_IDS = {
    "governance.get_governance_overview",
    "governance.query_enterprise_metrics",
}


def test_v016_seeds_tools_and_full_view_bindings_without_rewriting_history() -> None:
    migration_path = (
        Path(__file__).parents[1]
        / "scripts/migrations/V016_seed_full_view_capabilities.sql"
    )
    migration = migration_path.read_text(encoding="utf-8")

    assert "V016" in migration
    assert "governance.get_governance_overview" in migration
    assert "governance.query_enterprise_metrics" in migration
    assert "'full_information_view'" in migration
    assert "'published'" in migration
    assert "ON CONFLICT (capability_id, version) DO NOTHING" in migration
    assert (
        "ON CONFLICT (app_id, capability_id, capability_version) DO NOTHING"
        in migration
    )
    assert "SELECT 16" in migration


def test_v016_is_loaded_by_in_process_automatic_migrations() -> None:
    persistence = PostgresAgentPersistence(
        dsn="postgresql://unused:unused@127.0.0.1:1/unused",
        schema="fva_test_migration_inventory",
    )

    migrations = persistence._p2_migration_statements()

    assert any(
        "V016" in migration
        and SEEDED_TOOL_IDS.issubset(
            {
                tool_id
                for tool_id in SEEDED_TOOL_IDS
                if tool_id in migration
            }
        )
        for migration in migrations
    )


@pytest.mark.db
@pytest.mark.asyncio
async def test_v016_auto_migration_and_restart_keep_tools_in_full_view_snapshot(
    pg_schema: dict[str, str],
) -> None:
    """Exercise auto migration, application binding and restart on real PG."""

    dsn = pg_schema["dsn"]
    schema = pg_schema["schema"]

    # Force the isolated schema back to its pre-V016 state. The only path that
    # may restore these rows below is the runtime's automatic migration list.
    async with await psycopg.AsyncConnection.connect(dsn) as connection:
        await connection.execute(
            f'DELETE FROM "{schema}".application_capability_bindings '
            "WHERE capability_id = ANY(%s)",
            (list(SEEDED_TOOL_IDS),),
        )
        await connection.execute(
            f'DELETE FROM "{schema}".capability_tools '
            "WHERE capability_id = ANY(%s)",
            (list(SEEDED_TOOL_IDS),),
        )

    first_runtime = PostgresAgentPersistence(dsn=dsn, schema=schema)
    await first_runtime.initialize()

    async with await psycopg.AsyncConnection.connect(dsn) as connection:
        tool_cursor = await connection.execute(
            f'SELECT capability_id, status FROM "{schema}".capability_tools '
            "WHERE capability_id = ANY(%s)",
            (list(SEEDED_TOOL_IDS),),
        )
        assert dict(await tool_cursor.fetchall()) == {
            tool_id: "published" for tool_id in SEEDED_TOOL_IDS
        }

        binding_cursor = await connection.execute(
            f'SELECT capability_id, enabled FROM "{schema}".'
            "application_capability_bindings "
            "WHERE app_id = 'full_information_view' AND capability_id = ANY(%s)",
            (list(SEEDED_TOOL_IDS),),
        )
        assert dict(await binding_cursor.fetchall()) == {
            tool_id: True for tool_id in SEEDED_TOOL_IDS
        }
        await connection.execute(
            f'UPDATE "{schema}".capability_tools SET description = %s '
            "WHERE capability_id = %s AND version = '1.0.0'",
            (
                "administrator-owned description",
                "governance.get_governance_overview",
            ),
        )

    # A new persistence/repository/registry graph is the process-restart
    # boundary. Replaying the migration must be idempotent and the rebuilt
    # application-scoped snapshot must still expose both tools.
    restarted_runtime = PostgresAgentPersistence(dsn=dsn, schema=schema)
    await restarted_runtime.initialize()
    async with await psycopg.AsyncConnection.connect(dsn) as connection:
        description_cursor = await connection.execute(
            f'SELECT description FROM "{schema}".capability_tools '
            "WHERE capability_id = %s AND version = '1.0.0'",
            ("governance.get_governance_overview",),
        )
        assert (await description_cursor.fetchone()) == (
            "administrator-owned description",
        )
    restarted_snapshot_service = RunCapabilitySnapshotService(
        repository=PostgresCapabilityRepository(dsn=dsn, schema=schema),
        application_registry=PostgresApplicationRegistry(dsn=dsn, schema=schema),
    )
    snapshot = await restarted_snapshot_service.create_snapshot_for_run(
        run_id="run-v016-after-restart",
        base_registry=ToolRegistry.default(),
        app_id="full_information_view",
    )

    assert SEEDED_TOOL_IDS.issubset(snapshot.tool_versions)
    assert all(snapshot.tool_versions[tool_id] == "1.0.0" for tool_id in SEEDED_TOOL_IDS)
