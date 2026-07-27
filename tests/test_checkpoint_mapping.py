import os
from datetime import UTC, datetime
from uuid import uuid4

import psycopg
import pytest

from full_view_agent.application.checkpoint_mapping import (
    CHECKPOINT_NAMESPACE,
    CheckpointThreadMapping,
    build_checkpoint_thread_id,
)
from full_view_agent.application.errors import ResourceNotFound, RunStateConflict
from full_view_agent.infrastructure.checkpoint_mapping_store import (
    InMemoryCheckpointMappingStore,
    PostgresCheckpointMappingStore,
)


def postgres_test_dsn() -> str:
    dsn = os.getenv("FULL_VIEW_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("FULL_VIEW_TEST_DATABASE_URL is not configured")
    return dsn


def test_checkpoint_thread_id_is_deterministic_per_run_and_bounded() -> None:
    thread_id = build_checkpoint_thread_id("run_abc123")

    assert thread_id == "fva:run:run_abc123"
    assert len(thread_id) < 255
    assert build_checkpoint_thread_id("run_abc123") == thread_id
    assert build_checkpoint_thread_id("run_other") != thread_id


def test_checkpoint_mapping_keeps_product_and_framework_identifiers_explicit() -> None:
    now = datetime.now(UTC)

    mapping = CheckpointThreadMapping(
        run_id="run_abc123",
        session_id="ses_abc123",
        owner_user_id="user-1",
        thread_id=build_checkpoint_thread_id("run_abc123"),
        checkpoint_ns=CHECKPOINT_NAMESPACE,
        orchestrator="langgraph",
        checkpoint_id=None,
        version=1,
        created_at=now,
        updated_at=now,
    )

    assert mapping.run_id == "run_abc123"
    assert mapping.session_id == "ses_abc123"
    assert mapping.thread_id == "fva:run:run_abc123"
    assert mapping.checkpoint_ns == ""
    assert mapping.checkpoint_id is None
    assert mapping.version == 1


@pytest.mark.asyncio
async def test_mapping_store_create_is_idempotent_and_user_scoped() -> None:
    store = InMemoryCheckpointMappingStore()

    first = await store.ensure_mapping(
        user_id="user-1",
        run_id="run-1",
        session_id="ses-1",
    )
    replay = await store.ensure_mapping(
        user_id="user-1",
        run_id="run-1",
        session_id="ses-1",
    )

    assert replay == first
    assert first.thread_id == "fva:run:run-1"
    with pytest.raises(ResourceNotFound):
        await store.get_mapping(user_id="other-user", run_id="run-1")


@pytest.mark.asyncio
async def test_mapping_store_records_checkpoint_with_optimistic_version() -> None:
    store = InMemoryCheckpointMappingStore()
    created = await store.ensure_mapping(
        user_id="user-1",
        run_id="run-1",
        session_id="ses-1",
    )

    updated = await store.record_checkpoint(
        user_id="user-1",
        run_id="run-1",
        checkpoint_id="checkpoint-1",
        expected_version=created.version,
    )

    assert updated.checkpoint_id == "checkpoint-1"
    assert updated.version == 2
    replay = await store.record_checkpoint(
        user_id="user-1",
        run_id="run-1",
        checkpoint_id="checkpoint-1",
        expected_version=created.version,
    )
    assert replay == updated
    with pytest.raises(RunStateConflict):
        await store.record_checkpoint(
            user_id="user-1",
            run_id="run-1",
            checkpoint_id="checkpoint-stale",
            expected_version=created.version,
        )


@pytest.mark.asyncio
async def test_postgres_mapping_survives_restart_and_keeps_user_scope() -> None:
    schema = f"fva_checkpoint_test_{uuid4().hex[:12]}"
    store = PostgresCheckpointMappingStore(
        dsn=postgres_test_dsn(),
        schema=schema,
    )
    await store.initialize()
    try:
        created = await store.ensure_mapping(
            user_id="user-1",
            run_id="run-1",
            session_id="ses-1",
        )
        updated = await store.record_checkpoint(
            user_id="user-1",
            run_id="run-1",
            checkpoint_id="checkpoint-1",
            expected_version=created.version,
        )

        restarted = PostgresCheckpointMappingStore(
            dsn=postgres_test_dsn(),
            schema=schema,
        )
        recovered = await restarted.get_mapping(
            user_id="user-1",
            run_id="run-1",
        )

        assert recovered == updated
        replay = await restarted.record_checkpoint(
            user_id="user-1",
            run_id="run-1",
            checkpoint_id="checkpoint-1",
            expected_version=created.version,
        )
        assert replay == updated
        with pytest.raises(ResourceNotFound):
            await restarted.get_mapping(
                user_id="other-user",
                run_id="run-1",
            )
        with pytest.raises(RunStateConflict):
            await restarted.record_checkpoint(
                user_id="user-1",
                run_id="run-1",
                checkpoint_id="checkpoint-stale",
                expected_version=created.version,
            )
    finally:
        async with await psycopg.AsyncConnection.connect(
            postgres_test_dsn(),
        ) as connection:
            await connection.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
