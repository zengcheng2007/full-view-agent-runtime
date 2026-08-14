"""Unit tests for RunModelBindingRepository semantics.

Exercises the in-memory repository to verify invariants the Postgres
implementation must also satisfy:

* ``store_binding`` is first-write-wins: a second caller for the same
  ``run_id`` receives the first caller's binding as the winner.
* The snapshot row is recorded alongside the binding so that subsequent
  ``load_snapshot`` calls return the same snapshot the winner stored.
* Loading a non-existent binding returns None; loading a non-existent
  snapshot returns None.
"""

from __future__ import annotations

import pytest

from full_view_agent.application.model_config_repository import (
    InMemoryRunModelBindingRepository,
    ModelConfigSnapshot,
)


def _snapshot(
    config_id: str, config_version: int
) -> ModelConfigSnapshot:
    return ModelConfigSnapshot(
        config_id=config_id,
        config_version=config_version,
        name=f"conf-{config_id}",
        api_base_url="https://example/v1",
        model_name="model-x",
        protocol="openai_compatible",
        timeout_seconds=60,
        max_output_tokens=32000,
        max_retries=1,
        api_key_ciphertext=b"fake-ciphertext",
        api_key_nonce=b"fake-nonce",
    )


@pytest.fixture()
def repo() -> InMemoryRunModelBindingRepository:
    return InMemoryRunModelBindingRepository()


@pytest.mark.asyncio
async def test_store_binding_returns_first_writer_as_winner(
    repo: InMemoryRunModelBindingRepository,
) -> None:
    first_snapshot = _snapshot("config-A", 1)
    second_snapshot = _snapshot("config-B", 2)  # different config

    winner_a = await repo.store_binding("run-1", first_snapshot)
    winner_b = await repo.store_binding("run-1", second_snapshot)

    # Both calls return the first writer's binding.
    assert winner_a.run_id == "run-1"
    assert winner_a.config_id == "config-A"
    assert winner_a.config_version == 1
    assert winner_b is winner_a  # same object — first writer won


@pytest.mark.asyncio
async def test_load_binding_and_snapshot_round_trip(
    repo: InMemoryRunModelBindingRepository,
) -> None:
    snapshot = _snapshot("config-X", 7)
    winner = await repo.store_binding("run-2", snapshot)

    loaded_binding = await repo.load_binding("run-2")
    assert loaded_binding is not None
    assert loaded_binding.run_id == "run-2"
    assert loaded_binding.config_id == winner.config_id
    assert loaded_binding.config_version == winner.config_version

    loaded_snapshot = await repo.load_snapshot(
        config_id=winner.config_id,
        config_version=winner.config_version,
    )
    assert loaded_snapshot is not None
    assert loaded_snapshot.config_id == "config-X"
    assert loaded_snapshot.config_version == 7
    # Ciphertext/nonce preserved — the plaintext key is never written.
    assert loaded_snapshot.api_key_ciphertext == b"fake-ciphertext"
    assert loaded_snapshot.api_key_nonce == b"fake-nonce"


@pytest.mark.asyncio
async def test_load_nonexistent_returns_none(
    repo: InMemoryRunModelBindingRepository,
) -> None:
    assert await repo.load_binding("run-missing") is None
    assert (
        await repo.load_snapshot(config_id="config-missing", config_version=1)
        is None
    )


@pytest.mark.asyncio
async def test_snapshot_is_idempotent_across_multiple_bindings(
    repo: InMemoryRunModelBindingRepository,
) -> None:
    """If two different Runs bind to the same (config_id, version),
    the snapshot row is only inserted once but both bindings reference
    it. Verify both bindings see the same snapshot.
    """
    snapshot_v1 = _snapshot("config-shared", 1)
    winner_run1 = await repo.store_binding("run-a", snapshot_v1)
    winner_run2 = await repo.store_binding("run-b", snapshot_v1)

    snap_a = await repo.load_snapshot(
        config_id=winner_run1.config_id,
        config_version=winner_run1.config_version,
    )
    snap_b = await repo.load_snapshot(
        config_id=winner_run2.config_id,
        config_version=winner_run2.config_version,
    )
    assert snap_a is not None
    assert snap_b is not None
    assert snap_a.config_version == snap_b.config_version == 1
