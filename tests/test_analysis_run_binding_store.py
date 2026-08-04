"""Persistent ownership and resume binding for Analysis Graph runs."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from full_view_agent.application.analysis_run_binding import (
    AnalysisRunBinding,
    AnalysisRunBindingConflict,
)
from full_view_agent.application.errors import ResourceNotFound, RunStateConflict
from full_view_agent.infrastructure.analysis_run_binding_store import (
    InMemoryAnalysisRunBindingStore,
    PostgresAnalysisRunBindingStore,
)

FINGERPRINT_A = "sha256:" + "a" * 64
FINGERPRINT_B = "sha256:" + "b" * 64


def _binding_args(**overrides: str) -> dict[str, str]:
    values = {
        "tenant_id": "tenant-a",
        "user_id": "user-a",
        "session_id": "session-a",
        "run_id": "run-a",
        "plan_id": "plan-a",
        "request_id": "request-a",
        "invocation_fingerprint": FINGERPRINT_A,
    }
    values.update(overrides)
    return values


def _postgres_test_dsn() -> str:
    dsn = os.getenv("FULL_VIEW_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("FULL_VIEW_TEST_DATABASE_URL is not configured")
    return dsn


def test_forward_migration_is_portable_and_contains_authority_columns() -> None:
    migration = (
        Path(__file__).parents[1]
        / "scripts/migrations/V004_analysis_run_bindings.sql"
    ).read_text(encoding="utf-8")

    for column in (
        "tenant_id TEXT NOT NULL",
        "user_id TEXT NOT NULL",
        "session_id TEXT NOT NULL",
        "run_id TEXT PRIMARY KEY",
        "plan_id TEXT NOT NULL",
        "request_id TEXT NOT NULL",
        "invocation_fingerprint TEXT NOT NULL",
        "status TEXT NOT NULL",
        "report_result_id TEXT",
        "version BIGINT NOT NULL",
    ):
        assert column in migration
    assert "JSONB" not in migration.upper()
    assert "BEGIN;" in migration
    assert "COMMIT;" in migration
    assert "schema_version (version)" in migration
    assert "SELECT 4" in migration
    assert "CHECK" in migration


@pytest.mark.asyncio
async def test_memory_ensure_is_concurrently_idempotent() -> None:
    store = InMemoryAnalysisRunBindingStore()

    first, second = await asyncio.gather(
        store.ensure_binding(**_binding_args()),
        store.ensure_binding(**_binding_args()),
    )

    assert first == second
    assert first.status == "pending"
    assert first.report_result_id is None
    assert first.version == 1


@pytest.mark.asyncio
async def test_memory_rejects_conflicting_invocation_fingerprint() -> None:
    store = InMemoryAnalysisRunBindingStore()
    await store.ensure_binding(**_binding_args())

    with pytest.raises(AnalysisRunBindingConflict) as exc_info:
        await store.ensure_binding(
            **_binding_args(invocation_fingerprint=FINGERPRINT_B)
        )

    assert exc_info.value.code == "BINDING_CONFLICT"


@pytest.mark.asyncio
async def test_memory_hides_binding_from_another_tenant() -> None:
    store = InMemoryAnalysisRunBindingStore()
    await store.ensure_binding(**_binding_args())

    with pytest.raises(ResourceNotFound):
        await store.get_binding(
            tenant_id="tenant-b",
            user_id="user-a",
            run_id="run-a",
            invocation_fingerprint=FINGERPRINT_A,
        )
    with pytest.raises(ResourceNotFound):
        await store.ensure_binding(**_binding_args(tenant_id="tenant-b"))


@pytest.mark.asyncio
async def test_memory_update_is_versioned_and_idempotent() -> None:
    store = InMemoryAnalysisRunBindingStore()
    created = await store.ensure_binding(**_binding_args())

    running = await store.update_binding(
        tenant_id="tenant-a",
        user_id="user-a",
        run_id="run-a",
        invocation_fingerprint=FINGERPRINT_A,
        expected_version=created.version,
        status="running",
        report_result_id=None,
    )
    completed = await store.update_binding(
        tenant_id="tenant-a",
        user_id="user-a",
        run_id="run-a",
        invocation_fingerprint=FINGERPRINT_A,
        expected_version=running.version,
        status="completed",
        report_result_id="result-report-a",
    )
    replayed = await store.update_binding(
        tenant_id="tenant-a",
        user_id="user-a",
        run_id="run-a",
        invocation_fingerprint=FINGERPRINT_A,
        expected_version=running.version,
        status="completed",
        report_result_id="result-report-a",
    )

    assert completed == replayed
    assert completed.version == 3
    assert completed.report_result_id == "result-report-a"
    with pytest.raises(RunStateConflict):
        await store.update_binding(
            tenant_id="tenant-a",
            user_id="user-a",
            run_id="run-a",
            invocation_fingerprint=FINGERPRINT_A,
            expected_version=created.version,
            status="failed",
            report_result_id=None,
        )


@pytest.mark.asyncio
async def test_memory_ensure_replays_binding_after_status_changes() -> None:
    store = InMemoryAnalysisRunBindingStore()
    created = await store.ensure_binding(**_binding_args())
    updated = await store.update_binding(
        tenant_id="tenant-a",
        user_id="user-a",
        run_id="run-a",
        invocation_fingerprint=FINGERPRINT_A,
        expected_version=created.version,
        status="running",
        report_result_id=None,
    )

    assert await store.ensure_binding(**_binding_args()) == updated


@pytest.mark.asyncio
async def test_memory_get_rejects_wrong_invocation_fingerprint() -> None:
    store = InMemoryAnalysisRunBindingStore()
    await store.ensure_binding(**_binding_args())

    with pytest.raises(AnalysisRunBindingConflict) as exc_info:
        await store.get_binding(
            tenant_id="tenant-a",
            user_id="user-a",
            run_id="run-a",
            invocation_fingerprint=FINGERPRINT_B,
        )

    assert exc_info.value.code == "BINDING_FINGERPRINT_MISMATCH"


@pytest.mark.parametrize(
    ("status", "report_result_id"),
    [
        ("completed", None),
        ("partial", None),
        ("pending", "result-report-a"),
        ("running", "result-report-a"),
        ("waiting_input", "result-report-a"),
        ("failed", "result-report-a"),
        ("cancelled", "result-report-a"),
    ],
)
def test_binding_contract_rejects_inconsistent_report_reference(
    status: str, report_result_id: str | None
) -> None:
    with pytest.raises(ValidationError):
        AnalysisRunBinding.model_validate(
            {
                **_binding_args(),
                "status": status,
                "report_result_id": report_result_id,
            }
        )


@pytest.mark.asyncio
async def test_memory_rejects_terminal_revival_and_status_skips() -> None:
    store = InMemoryAnalysisRunBindingStore()
    pending = await store.ensure_binding(**_binding_args())
    with pytest.raises(RunStateConflict):
        await store.update_binding(
            tenant_id="tenant-a",
            user_id="user-a",
            run_id="run-a",
            invocation_fingerprint=FINGERPRINT_A,
            expected_version=pending.version,
            status="completed",
            report_result_id="result-report-a",
        )
    running = await store.update_binding(
        tenant_id="tenant-a",
        user_id="user-a",
        run_id="run-a",
        invocation_fingerprint=FINGERPRINT_A,
        expected_version=pending.version,
        status="running",
        report_result_id=None,
    )
    completed = await store.update_binding(
        tenant_id="tenant-a",
        user_id="user-a",
        run_id="run-a",
        invocation_fingerprint=FINGERPRINT_A,
        expected_version=running.version,
        status="completed",
        report_result_id="result-report-a",
    )
    assert await store.update_binding(
        tenant_id="tenant-a",
        user_id="user-a",
        run_id="run-a",
        invocation_fingerprint=FINGERPRINT_A,
        expected_version=running.version,
        status="completed",
        report_result_id="result-report-a",
    ) == completed
    with pytest.raises(RunStateConflict):
        await store.update_binding(
            tenant_id="tenant-a",
            user_id="user-a",
            run_id="run-a",
            invocation_fingerprint=FINGERPRINT_A,
            expected_version=completed.version,
            status="running",
            report_result_id=None,
        )


@pytest.mark.asyncio
async def test_postgres_rejects_terminal_revival_and_invalid_report_pair() -> None:
    schema = f"fva_binding_{uuid4().hex[:12]}"
    store = PostgresAnalysisRunBindingStore(dsn=_postgres_test_dsn(), schema=schema)
    await store.initialize()
    try:
        pending = await store.ensure_binding(**_binding_args())
        with pytest.raises(RunStateConflict):
            await store.update_binding(
                tenant_id="tenant-a",
                user_id="user-a",
                run_id="run-a",
                invocation_fingerprint=FINGERPRINT_A,
                expected_version=pending.version,
                status="completed",
                report_result_id=None,
            )
        running = await store.update_binding(
            tenant_id="tenant-a",
            user_id="user-a",
            run_id="run-a",
            invocation_fingerprint=FINGERPRINT_A,
            expected_version=pending.version,
            status="running",
            report_result_id=None,
        )
        completed = await store.update_binding(
            tenant_id="tenant-a",
            user_id="user-a",
            run_id="run-a",
            invocation_fingerprint=FINGERPRINT_A,
            expected_version=running.version,
            status="partial",
            report_result_id="result-report-a",
        )
        with pytest.raises(RunStateConflict):
            await store.update_binding(
                tenant_id="tenant-a",
                user_id="user-a",
                run_id="run-a",
                invocation_fingerprint=FINGERPRINT_A,
                expected_version=completed.version,
                status="cancelled",
                report_result_id=None,
            )
    finally:
        await store.drop_schema()


@pytest.mark.asyncio
async def test_postgres_is_concurrently_idempotent_isolated_and_survives_restart() -> None:
    schema = f"fva_binding_{uuid4().hex[:12]}"
    dsn = _postgres_test_dsn()
    first_store = PostgresAnalysisRunBindingStore(dsn=dsn, schema=schema)
    await first_store.initialize()
    try:
        first, second = await asyncio.gather(
            first_store.ensure_binding(**_binding_args()),
            first_store.ensure_binding(**_binding_args()),
        )
        assert first == second

        restarted = PostgresAnalysisRunBindingStore(dsn=dsn, schema=schema)
        loaded = await restarted.get_binding(
            tenant_id="tenant-a",
            user_id="user-a",
            run_id="run-a",
            invocation_fingerprint=FINGERPRINT_A,
        )
        assert loaded == first

        with pytest.raises(ResourceNotFound):
            await restarted.get_binding(
                tenant_id="tenant-b",
                user_id="user-a",
                run_id="run-a",
                invocation_fingerprint=FINGERPRINT_A,
            )
        with pytest.raises(AnalysisRunBindingConflict):
            await restarted.ensure_binding(
                **_binding_args(invocation_fingerprint=FINGERPRINT_B)
            )
    finally:
        await first_store.drop_schema()


@pytest.mark.asyncio
async def test_postgres_versioned_update_survives_restart() -> None:
    schema = f"fva_binding_{uuid4().hex[:12]}"
    dsn = _postgres_test_dsn()
    store = PostgresAnalysisRunBindingStore(dsn=dsn, schema=schema)
    await store.initialize()
    try:
        created = await store.ensure_binding(**_binding_args())
        running = await store.update_binding(
            tenant_id="tenant-a",
            user_id="user-a",
            run_id="run-a",
            invocation_fingerprint=FINGERPRINT_A,
            expected_version=created.version,
            status="running",
            report_result_id=None,
        )
        updated = await store.update_binding(
            tenant_id="tenant-a",
            user_id="user-a",
            run_id="run-a",
            invocation_fingerprint=FINGERPRINT_A,
            expected_version=running.version,
            status="partial",
            report_result_id="result-report-a",
        )
        restarted = PostgresAnalysisRunBindingStore(dsn=dsn, schema=schema)
        assert await restarted.get_binding(
            tenant_id="tenant-a",
            user_id="user-a",
            run_id="run-a",
            invocation_fingerprint=FINGERPRINT_A,
        ) == updated
    finally:
        await store.drop_schema()
