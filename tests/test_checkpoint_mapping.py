import os
from datetime import UTC, datetime
from pathlib import Path
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
    analysis_thread = build_checkpoint_thread_id(
        "run_abc123", graph_kind="analysis"
    )
    assert analysis_thread == "fva:analysis:run:run_abc123"
    assert analysis_thread != thread_id


def test_v009_migration_preserves_agent_identity_and_adds_composite_key() -> None:
    migration = (
        Path(__file__).parents[1]
        / "scripts/migrations/V009_checkpoint_graph_kind.sql"
    ).read_text(encoding="utf-8")

    assert "graph_kind TEXT NOT NULL DEFAULT 'agent'" in migration
    assert "PRIMARY KEY (run_id, graph_kind)" in migration
    assert "graph_kind IN ('agent', 'analysis')" in migration
    assert "ALTER COLUMN graph_kind DROP DEFAULT" in migration
    assert "SELECT 9" in migration
    assert "JSONB" not in migration.upper()


def test_checkpoint_mapping_keeps_product_and_framework_identifiers_explicit() -> None:
    now = datetime.now(UTC)

    mapping = CheckpointThreadMapping(
        run_id="run_abc123",
        graph_kind="agent",
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
    assert mapping.graph_kind == "agent"
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
async def test_mapping_store_isolates_agent_and_analysis_for_same_run() -> None:
    store = InMemoryCheckpointMappingStore()

    agent = await store.ensure_mapping(
        user_id="user-1",
        run_id="run-1",
        session_id="ses-1",
        graph_kind="agent",
    )
    analysis = await store.ensure_mapping(
        user_id="tenant-user-hash",
        run_id="run-1",
        session_id="ses-1",
        graph_kind="analysis",
    )

    assert agent.graph_kind == "agent"
    assert analysis.graph_kind == "analysis"
    assert agent.thread_id == "fva:run:run-1"
    assert analysis.thread_id == "fva:analysis:run:run-1"
    assert agent.thread_id != analysis.thread_id
    assert (
        await store.get_mapping(
            user_id="tenant-user-hash",
            run_id="run-1",
            graph_kind="analysis",
        )
        == analysis
    )


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
        analysis = await store.ensure_mapping(
            user_id="tenant-user-hash",
            run_id="run-1",
            session_id="ses-1",
            graph_kind="analysis",
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
        assert analysis.thread_id == "fva:analysis:run:run-1"
        assert (
            await restarted.get_mapping(
                user_id="tenant-user-hash",
                run_id="run-1",
                graph_kind="analysis",
            )
            == analysis
        )
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


@pytest.mark.asyncio
async def test_v009_upgrades_legacy_mapping_without_changing_agent_thread() -> None:
    schema = f"fva_checkpoint_upgrade_{uuid4().hex[:10]}"
    dsn = postgres_test_dsn()
    legacy_thread = "fva:run:run-legacy"
    now = datetime.now(UTC)
    try:
        async with await psycopg.AsyncConnection.connect(dsn) as connection:
            await connection.execute(f'CREATE SCHEMA "{schema}"')
            await connection.execute(
                f'CREATE TABLE "{schema}".schema_version (version BIGINT PRIMARY KEY)'
            )
            await connection.execute(
                f'CREATE TABLE "{schema}".orchestration_checkpoint_mappings ('
                "run_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, "
                "owner_user_id TEXT NOT NULL, thread_id VARCHAR(255) NOT NULL UNIQUE, "
                "checkpoint_ns TEXT NOT NULL, orchestrator TEXT NOT NULL, "
                "checkpoint_id TEXT, version BIGINT NOT NULL, "
                "created_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL)"
            )
            await connection.execute(
                f'INSERT INTO "{schema}".orchestration_checkpoint_mappings '
                "VALUES (%s, %s, %s, %s, '', 'langgraph', NULL, 1, %s, %s)",
                ("run-legacy", "ses-1", "user-1", legacy_thread, now, now),
            )
        migration = (
            Path(__file__).parents[1]
            / "scripts/migrations/V009_checkpoint_graph_kind.sql"
        ).read_text(encoding="utf-8")
        async with await psycopg.AsyncConnection.connect(dsn) as connection:
            await connection.execute(migration.replace("full_view_agent", schema))

        store = PostgresCheckpointMappingStore(dsn=dsn, schema=schema)
        existing = await store.get_mapping(
            user_id="user-1",
            run_id="run-legacy",
            graph_kind="agent",
        )
        analysis = await store.ensure_mapping(
            user_id="analysis-owner",
            run_id="run-legacy",
            session_id="ses-1",
            graph_kind="analysis",
        )
        assert existing.thread_id == legacy_thread
        assert analysis.thread_id == "fva:analysis:run:run-legacy"
    finally:
        async with await psycopg.AsyncConnection.connect(dsn) as connection:
            await connection.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
