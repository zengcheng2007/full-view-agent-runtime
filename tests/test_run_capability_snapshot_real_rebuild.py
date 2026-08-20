"""Real-rebuild tests for Run -> capability snapshot.

Proves that a Run's pinned tool versions survive a process restart:
a fresh ``PostgresRunCapabilitySnapshotStore`` (built from the same DB)
loads the same (run_id, tool_id -> version) map, and
``RunCapabilitySnapshotService._rebuild_from_persisted`` reconstructs a
registry carrying exactly those versions.

These tests require a real PostgreSQL instance (set
``FULL_VIEW_DATABASE_URL``). They are skipped in no-DB CI.
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime

import psycopg
import pytest

from full_view_agent.application.run_capability_snapshot import (
    RunCapabilitySnapshotService,
)
from full_view_agent.application.run_capability_snapshot_store import (
    PersistedRunCapabilitySnapshot,
    PostgresRunCapabilitySnapshotStore,
)
from full_view_agent.application.tool_registry import ToolRegistry
from full_view_agent.domain.agent_definition import AgentExecutionPolicy
from full_view_agent.domain.capability import ToolCapability
from full_view_agent.infrastructure.capability_repository import (
    InMemoryCapabilityRepository,
)

pytestmark = pytest.mark.db

DATABASE_URL = os.getenv("FULL_VIEW_TEST_DATABASE_URL", "")

requires_postgres = pytest.mark.skipif(
    not DATABASE_URL,
    reason="FULL_VIEW_TEST_DATABASE_URL required for real-rebuild test",
)


@pytest.fixture()
async def clean_cap_snapshot(pg_schema):
    """Clean up the test run's capability snapshot rows before / after.

    Uses the ``pg_schema`` fixture so cleanup runs against the
    throwaway schema (not the shared catalog).
    """
    run_id = "run-cap-rebuild-1"
    schema = pg_schema["schema"]
    dsn = pg_schema["dsn"]

    async def _cleanup() -> None:
        async with await psycopg.AsyncConnection.connect(dsn) as conn:
            await conn.execute(
                f'DELETE FROM "{schema}".run_capability_snapshots'
                f" WHERE run_id = %s",
                (run_id,),
            )

    await _cleanup()
    yield pg_schema
    await _cleanup()


def _tool(capability_id: str, version: str) -> ToolCapability:
    """Build a minimal ToolCapability for seeding."""
    return ToolCapability(
        capability_id=capability_id,
        name=f"tool-{capability_id}",
        domain="governance",
        owner="test",
        version=version,
        status="published",
        guidance="Test guidance for tool",
        risk_level="low",
        required_permissions=[],
        dataset_ids=[],
        description="test tool",
        connector_ref="test-connector",
        http_method="GET",
        resource_path="/test",
        input_schema={},
        output_schema={},
        parameter_mapping={},
        result_mapping={},
        result_kind="table",
        data_schema_ref="",
        timeout_ms=8000,
        max_attempts=2,
        max_result_rows=100,
        cache_enabled=True,
        cache_ttl_seconds=60,
    )


@requires_postgres
@pytest.mark.asyncio
async def test_postgres_roundtrip_preserves_prompt_policy_and_contract_integrity(
    clean_cap_snapshot,
) -> None:
    schema = clean_cap_snapshot["schema"]
    dsn = clean_cap_snapshot["dsn"]
    store = PostgresRunCapabilitySnapshotStore(dsn=dsn, schema=schema)
    expected = PersistedRunCapabilitySnapshot(
        run_id="run-cap-rebuild-1",
        tool_versions={"tool.population": "1.1.0"},
        captured_at=datetime(2026, 8, 15, tzinfo=UTC),
        application_prompt_versions={"application.policy": "3.0.0"},
        agent_prompt_versions={"agent.instruction": "2.0.0"},
        application_prompt_fingerprints={
            "application.policy": "sha256:application"
        },
        agent_prompt_fingerprints={"agent.instruction": "sha256:agent"},
        application_id="full_information_view",
        application_scoped=True,
        agent_scoped=True,
        execution_policy=AgentExecutionPolicy(
            max_model_turns=4,
            max_tool_calls=6,
            max_elapsed_seconds=75,
        ),
        tool_contract_fingerprints={
            "tool.population": "sha256:semantic-contract"
        },
    )

    await store.store_if_absent(expected)
    loaded = await store.load(expected.run_id)

    assert loaded == expected


@requires_postgres
@pytest.mark.asyncio
async def test_capability_snapshot_round_trip_via_db(
    clean_cap_snapshot: dict,
) -> None:
    """Persist a snapshot via store A, read it back via fresh store B.

    Phase 1: seed the capability repository with two published tools (v1).
    Phase 2: build a RunCapabilitySnapshotService with store A; create a
             snapshot for run-1. The service persists the (tool_id, v1)
             triples to the DB.
    Phase 3: "restart" — build a fresh service + store B with the same
             DB but an empty in-memory cache.
    Phase 4: ask the new service for run-1's snapshot. It should consult
             the DB store and rebuild the registry from the persisted
             versions — yielding the same tool versions as the original.
    """
    dsn = clean_cap_snapshot["dsn"]
    schema = clean_cap_snapshot["schema"]

    # Phase 1: seed the capability repo with v1 tools.
    cap_repo = InMemoryCapabilityRepository()
    await cap_repo.save_tool(_tool("governance.t1", "1.0.0"))
    await cap_repo.save_tool(_tool("governance.t2", "1.0.0"))

    # Phase 2: first service + store A.
    store_a = PostgresRunCapabilitySnapshotStore(dsn=dsn, schema=schema)
    service_a = RunCapabilitySnapshotService(
        repository=cap_repo, store=store_a
    )
    base_registry = ToolRegistry.default()
    snap_a = await service_a.create_snapshot_for_run(
        run_id="run-cap-rebuild-1", base_registry=base_registry
    )
    assert snap_a.tool_versions == {
        "governance.t1": "1.0.0",
        "governance.t2": "1.0.0",
    }

    # Phase 3: "restart" — fresh service + store B, empty cache.
    store_b = PostgresRunCapabilitySnapshotStore(dsn=dsn, schema=schema)
    service_b = RunCapabilitySnapshotService(
        repository=cap_repo, store=store_b
    )

    # Phase 4: load via the fresh service. It must consult store B and
    # rebuild the registry from the persisted versions.
    snap_b = await service_b.create_snapshot_for_run(
        run_id="run-cap-rebuild-1", base_registry=base_registry
    )
    # The rebuilt snapshot must carry the same versions.
    assert snap_b.tool_versions == snap_a.tool_versions
    # And the captured_at must match the original (not a new timestamp).
    assert snap_b.created_at == snap_a.created_at


@requires_postgres
@pytest.mark.asyncio
async def test_capability_snapshot_rebuild_fails_closed_when_tool_gone(
    clean_cap_snapshot: dict,
) -> None:
    """If a pinned tool version is no longer in the repository, rebuild
    raises instead of silently substituting the live capability set.

    This is the "fail closed" half of the Run-pinned semantic for
    capabilities: if we can't rebuild exactly what the Run pinned, we
    must NOT silently give it whatever's currently published — that
    would silently violate the hot-publish guarantee.
    """
    dsn = clean_cap_snapshot["dsn"]
    schema = clean_cap_snapshot["schema"]

    cap_repo = InMemoryCapabilityRepository()
    await cap_repo.save_tool(_tool("governance.t1", "1.0.0"))

    store = PostgresRunCapabilitySnapshotStore(dsn=dsn, schema=schema)
    service = RunCapabilitySnapshotService(repository=cap_repo, store=store)
    base_registry = ToolRegistry.default()
    await service.create_snapshot_for_run(
        run_id="run-cap-rebuild-1", base_registry=base_registry
    )

    # Now "delete" the v1 tool and publish v2 under the same capability_id.
    cap_repo_v2 = InMemoryCapabilityRepository()
    await cap_repo_v2.save_tool(_tool("governance.t1", "2.0.0"))

    # Fresh service, new repo, same DB store.
    store_b = PostgresRunCapabilitySnapshotStore(dsn=dsn, schema=schema)
    service_b = RunCapabilitySnapshotService(
        repository=cap_repo_v2, store=store_b
    )
    # The persisted snapshot references v1, but the new repo only has v2.
    # The service must raise rather than silently returning v2.
    with pytest.raises(RuntimeError, match="pinned tool versions"):
        await service_b.create_snapshot_for_run(
            run_id="run-cap-rebuild-1", base_registry=base_registry
        )


@requires_postgres
@pytest.mark.asyncio
async def test_postgres_snapshot_store_has_atomic_first_writer(clean_cap_snapshot: dict) -> None:
    """Concurrent disjoint writers must never produce a mixed Run snapshot."""

    dsn = clean_cap_snapshot["dsn"]
    schema = clean_cap_snapshot["schema"]
    store_a = PostgresRunCapabilitySnapshotStore(dsn=dsn, schema=schema)
    store_b = PostgresRunCapabilitySnapshotStore(dsn=dsn, schema=schema)
    captured_at = datetime(2026, 8, 11, 13, 0, tzinfo=UTC)
    first = PersistedRunCapabilitySnapshot(
        run_id="run-cap-rebuild-1",
        tool_versions={"tool.a": "1.0.0"},
        captured_at=captured_at,
    )
    second = PersistedRunCapabilitySnapshot(
        run_id="run-cap-rebuild-1",
        tool_versions={"tool.b": "2.0.0"},
        captured_at=captured_at,
    )

    winner_a, winner_b = await asyncio.gather(
        store_a.store_if_absent(first),
        store_b.store_if_absent(second),
    )
    loaded = await store_a.load("run-cap-rebuild-1")

    assert winner_a == winner_b
    assert loaded in (first, second)
