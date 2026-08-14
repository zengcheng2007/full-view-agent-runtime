from pathlib import Path

from full_view_agent.infrastructure.postgres_persistence import (
    PostgresAgentPersistence,
)

TOOL_ID = "governance.query_governance_power_metrics"


def test_v025_publishes_and_binds_aggregate_governance_power() -> None:
    migration = (
        Path(__file__).parents[1]
        / "scripts/migrations/V025_seed_governance_power.sql"
    ).read_text(encoding="utf-8")

    assert TOOL_ID in migration
    assert "'/api/getGovernancePower'" in migration
    assert "'full_information_view'" in migration
    assert "'published'" in migration
    assert "ON CONFLICT (capability_id, version) DO NOTHING" in migration
    assert "SELECT 25" in migration
    assert "getGovernancePowerByGridCode" not in migration


def test_v025_is_loaded_by_automatic_migrations() -> None:
    persistence = PostgresAgentPersistence(
        dsn="postgresql://unused:unused@127.0.0.1:1/unused",
        schema="fva_test_migration_inventory",
    )

    migrations = persistence._p2_migration_statements()

    assert any("V025" in migration and TOOL_ID in migration for migration in migrations)
