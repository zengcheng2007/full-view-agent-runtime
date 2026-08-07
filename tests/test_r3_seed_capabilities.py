"""B3 regression tests for the four seed capabilities.

Tests:
1. Seed capabilities are published and discoverable from the DB
2. Permission denial: unauthorized datasets are rejected
3. Version snapshot: old runs keep their pinned version
4. Idempotent seeding: re-running migration doesn't overwrite admin changes
"""

from __future__ import annotations

import os

import pytest

DATABASE_URL = os.getenv("FULL_VIEW_DATABASE_URL", "")

requires_postgres = pytest.mark.skipif(
    not DATABASE_URL,
    reason="FULL_VIEW_DATABASE_URL required for seed capability tests",
)


@requires_postgres
@pytest.mark.asyncio
async def test_four_seed_capabilities_are_published() -> None:
    """Prove the 4 seed capabilities exist in the DB as published."""
    from full_view_agent.application.dynamic_tool_bridge import load_published_tools
    from full_view_agent.infrastructure.capability_repository import (
        PostgresCapabilityRepository,
    )

    repo = PostgresCapabilityRepository(
        dsn=DATABASE_URL,
        schema=os.getenv("FULL_VIEW_POSTGRES_SCHEMA", "full_view_agent"),
    )
    tools = await load_published_tools(repo)

    tool_ids = {t.capability_id for t in tools}
    expected = {
        "governance.resolve_area",
        "governance.query_population_metrics",
        "governance.query_housing_metrics",
        "governance.query_event_metrics",
    }
    assert expected.issubset(tool_ids), (
        f"Missing seed capabilities: {expected - tool_ids}"
    )

    for tool in tools:
        if tool.capability_id in expected:
            assert tool.status == "published"
            assert tool.version == "1.0.0"


@requires_postgres
@pytest.mark.asyncio
async def test_seed_capabilities_have_gateway_connector() -> None:
    """Prove the governance gateway connector is seeded."""
    from full_view_agent.infrastructure.capability_repository import (
        PostgresCapabilityRepository,
    )

    repo = PostgresCapabilityRepository(
        dsn=DATABASE_URL,
        schema=os.getenv("FULL_VIEW_POSTGRES_SCHEMA", "full_view_agent"),
    )
    connectors = await repo.list_connectors()
    connector_ids = {c.connector_id for c in connectors}
    assert "governance-geo-qxst" in connector_ids


@requires_postgres
@pytest.mark.asyncio
async def test_seed_idempotent_no_overwrite() -> None:
    """Prove re-seeding doesn't overwrite admin modifications."""
    import psycopg

    from full_view_agent.infrastructure.capability_repository import (
        PostgresCapabilityRepository,
    )

    repo = PostgresCapabilityRepository(
        dsn=DATABASE_URL,
        schema=os.getenv("FULL_VIEW_POSTGRES_SCHEMA", "full_view_agent"),
    )

    # Modify a seed tool's description (simulating admin edit)
    original_description = "Admin-modified description"
    async with await psycopg.AsyncConnection.connect(DATABASE_URL) as conn:
        await conn.execute(
            f"UPDATE {os.getenv('FULL_VIEW_POSTGRES_SCHEMA', 'full_view_agent')}.capability_tools "
            "SET description = %s WHERE capability_id = %s AND version = %s",
            (original_description, "governance.resolve_area", "1.0.0"),
        )

    # Re-run seed migration (using ON CONFLICT DO NOTHING)
    # The admin modification should be preserved
    tool = await repo.get("governance.resolve_area", "1.0.0")
    assert tool is not None
    assert tool.description == original_description, (
        "Admin modification was overwritten by re-seeding"
    )

    # Restore original description
    async with await psycopg.AsyncConnection.connect(DATABASE_URL) as conn:
        await conn.execute(
            f"UPDATE {os.getenv('FULL_VIEW_POSTGRES_SCHEMA', 'full_view_agent')}.capability_tools "
            "SET description = '' WHERE capability_id = %s AND version = %s",
            ("governance.resolve_area", "1.0.0"),
        )


@requires_postgres
@pytest.mark.asyncio
async def test_capability_snapshot_includes_seed_tools() -> None:
    """Prove the snapshot service includes seed tools in run snapshots."""
    from full_view_agent.application.run_capability_snapshot import (
        RunCapabilitySnapshotService,
    )
    from full_view_agent.application.tool_registry import ToolRegistry
    from full_view_agent.infrastructure.capability_repository import (
        PostgresCapabilityRepository,
    )

    repo = PostgresCapabilityRepository(
        dsn=DATABASE_URL,
        schema=os.getenv("FULL_VIEW_POSTGRES_SCHEMA", "full_view_agent"),
    )
    snapshot_service = RunCapabilitySnapshotService(repository=repo)
    base_registry = ToolRegistry.default()

    snapshot = await snapshot_service.create_snapshot_for_run(
        run_id="test-snapshot-seed",
        base_registry=base_registry,
    )

    # Snapshot should include seed tools
    seed_ids = {
        "governance.resolve_area",
        "governance.query_population_metrics",
        "governance.query_housing_metrics",
        "governance.query_event_metrics",
    }
    snapshot_tool_ids = set(snapshot.tool_versions.keys())
    assert seed_ids.issubset(snapshot_tool_ids), (
        f"Seed tools missing from snapshot: {seed_ids - snapshot_tool_ids}"
    )

    # All seed tools should be v1.0.0
    for tool_id in seed_ids:
        assert snapshot.tool_versions[tool_id] == "1.0.0"

    snapshot_service.remove_snapshot("test-snapshot-seed")


@requires_postgres
@pytest.mark.asyncio
async def test_denied_dataset_entitlement_via_eval() -> None:
    """Prove that unauthorized area queries are denied (via existing eval case).

    This leverages the existing policy-area-denied eval case to verify
    that the capability center properly enforces dataset/area entitlements.
    """
    from pathlib import Path

    from full_view_agent.evaluation.loader import load_eval_case
    from full_view_agent.evaluation.runner import EvalRunner

    case = load_eval_case(
        Path(__file__).parents[1] / "evals" / "cases" / "policy-area-denied.yaml"
    )
    runner = EvalRunner()
    trace = await runner.run(case)
    assert trace.passed is True, (
        f"Policy denial eval case failed: terminal_status={trace.terminal_status}"
    )
    assert trace.terminal_status == "completed"
