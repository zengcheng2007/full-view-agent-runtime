"""Unit tests for RunCapabilitySnapshotStore semantics.

These tests exercise the in-memory store (no DB required) and verify
the invariants the Postgres store must also satisfy:

* ``store_if_absent`` is first-write-wins: the second caller sees the
  first caller's snapshot as the winner.
* Empty capability sets are distinguishable from "no snapshot yet" via
  the sentinel row: ``load`` returns a snapshot with empty
  ``tool_versions`` (not None) for an intentionally-empty snapshot.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from full_view_agent.application.run_capability_snapshot_store import (
    InMemoryRunCapabilitySnapshotStore,
    PersistedRunCapabilitySnapshot,
)


@pytest.fixture()
def store() -> InMemoryRunCapabilitySnapshotStore:
    return InMemoryRunCapabilitySnapshotStore()


def _snapshot(run_id: str, **versions: str) -> PersistedRunCapabilitySnapshot:
    return PersistedRunCapabilitySnapshot(
        run_id=run_id,
        tool_versions=dict(versions),
        captured_at=datetime(2026, 8, 8, 12, 0, 0, tzinfo=UTC),
    )


@pytest.mark.asyncio
async def test_store_if_absent_returns_first_writer_as_winner(
    store: InMemoryRunCapabilitySnapshotStore,
) -> None:
    first = _snapshot("run-1", gov_tool="1.0.0")
    second = _snapshot("run-1", gov_tool="2.0.0")  # different version

    winner_a = await store.store_if_absent(first)
    winner_b = await store.store_if_absent(second)

    # Both calls return the same snapshot — the first writer's.
    assert winner_a is first
    assert winner_b is first
    assert winner_b.tool_versions == {"gov_tool": "1.0.0"}


@pytest.mark.asyncio
async def test_empty_snapshot_is_distinguishable_from_no_snapshot(
    store: InMemoryRunCapabilitySnapshotStore,
) -> None:
    # Before any store call, ``load`` returns None.
    assert await store.load("run-empty") is None

    # Store an intentionally-empty snapshot.
    empty = _snapshot("run-empty")  # tool_versions == {}
    winner = await store.store_if_absent(empty)
    assert winner is empty

    # After storing the empty snapshot, ``load`` returns a snapshot
    # with empty ``tool_versions`` — NOT None. This lets the rebuild
    # path distinguish "deliberately no capabilities" from "not yet
    # snapshotted".
    loaded = await store.load("run-empty")
    assert loaded is not None
    assert loaded.tool_versions == {}
    assert loaded.run_id == "run-empty"
    assert loaded.captured_at == empty.captured_at


@pytest.mark.asyncio
async def test_store_preserves_skill_and_workflow_versions(
    store: InMemoryRunCapabilitySnapshotStore,
) -> None:
    snapshot = PersistedRunCapabilitySnapshot(
        run_id="run-runtime-definitions",
        tool_versions={"tool.a": "1.0.0"},
        skill_versions={"skill.a": "2.0.0"},
        workflow_versions={"workflow.a": "3.0.0"},
        captured_at=datetime.now(UTC),
    )

    await store.store_if_absent(snapshot)
    loaded = await store.load(snapshot.run_id)

    assert loaded is not None
    assert loaded.skill_versions == {"skill.a": "2.0.0"}
    assert loaded.workflow_versions == {"workflow.a": "3.0.0"}


@pytest.mark.asyncio
async def test_delete_removes_both_empty_and_populated_snapshots(
    store: InMemoryRunCapabilitySnapshotStore,
) -> None:
    # Populate then delete.
    await store.store_if_absent(_snapshot("run-populated", gov_tool="1.0.0"))
    assert await store.load("run-populated") is not None
    await store.delete("run-populated")
    assert await store.load("run-populated") is None

    # Empty then delete.
    await store.store_if_absent(_snapshot("run-empty"))
    assert await store.load("run-empty") is not None
    await store.delete("run-empty")
    assert await store.load("run-empty") is None

    # Delete on a run_id that was never stored is a no-op.
    await store.delete("run-never-stored")


@pytest.mark.asyncio
async def test_concurrent_wins_are_consistent_across_loads(
    store: InMemoryRunCapabilitySnapshotStore,
) -> None:
    """Simulate two concurrent writers: both should converge on the same
    winner when they call ``store_if_absent`` and ``load`` afterwards.
    """
    first = _snapshot("run-c", tool_a="1.0.0")
    second = _snapshot("run-c", tool_a="9.9.9", tool_b="1.0.0")

    winner_a = await store.store_if_absent(first)
    winner_b = await store.store_if_absent(second)
    load_after = await store.load("run-c")

    assert winner_a.tool_versions == {"tool_a": "1.0.0"}
    assert winner_b is winner_a  # same reference — first writer won
    assert load_after is not None
    assert load_after.tool_versions == {"tool_a": "1.0.0"}
