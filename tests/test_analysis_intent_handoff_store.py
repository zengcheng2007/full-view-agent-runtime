from __future__ import annotations

import asyncio
import os
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from pydantic import ValidationError

from full_view_agent.application.analysis_intent_handoff import (
    AnalysisIntentHandoff,
    AnalysisIntentHandoffConflict,
    HandoffClarificationOption,
)
from full_view_agent.application.errors import ResourceNotFound, RunStateConflict
from full_view_agent.domain.analysis_intent import AnalysisIntentV1
from full_view_agent.infrastructure.analysis_intent_handoff_store import (
    InMemoryAnalysisIntentHandoffStore,
    PostgresAnalysisIntentHandoffStore,
)


def intent(*goals: str) -> AnalysisIntentV1:
    return AnalysisIntentV1.model_validate(
        {
            "goals": list(goals),
            "scope": {"kind": "named_area", "area_query": "西湖区"},
        }
    )


def capture_args(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "tenant_id": "tenant-a",
        "user_id": "user-a",
        "session_id": "session-a",
        "run_id": "run-a",
        "intent": intent("population"),
    }
    values.update(overrides)
    return values


def option() -> HandoffClarificationOption:
    return HandoffClarificationOption(
        option_id="opt-opaque-a",
        area_code="330106",
        area_name="西湖区",
    )


def postgres_dsn() -> str:
    dsn = os.getenv("FULL_VIEW_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("FULL_VIEW_TEST_DATABASE_URL is not configured")
    return dsn


def test_v008_migration_is_portable_and_complete() -> None:
    migration = (
        Path(__file__).parents[1]
        / "scripts/migrations/V008_analysis_intent_handoffs.sql"
    ).read_text(encoding="utf-8")
    for column in (
        "handoff_id TEXT NOT NULL UNIQUE",
        "tenant_id TEXT NOT NULL",
        "user_id TEXT NOT NULL",
        "session_id TEXT NOT NULL",
        "run_id TEXT PRIMARY KEY",
        "intent_json TEXT NOT NULL",
        "intent_fingerprint TEXT NOT NULL",
        "clarification_json TEXT NOT NULL",
        "selected_area_code TEXT",
        "plan_id TEXT",
        "request_id TEXT",
        "failure_code TEXT",
        "report_result_id TEXT",
        "version BIGINT NOT NULL",
    ):
        assert column in migration
    assert "JSONB" not in migration.upper()
    assert "BEGIN;" in migration and "COMMIT;" in migration
    assert "SELECT 8" in migration


async def test_memory_capture_is_concurrently_idempotent() -> None:
    store = InMemoryAnalysisIntentHandoffStore()

    first, second = await asyncio.gather(
        store.capture(**capture_args()),  # type: ignore[arg-type]
        store.capture(**capture_args()),  # type: ignore[arg-type]
    )

    assert first == second
    assert first.status == "captured"
    assert first.version == 1
    assert first.intent_fingerprint.startswith("sha256:")


async def test_memory_capture_rejects_different_intent_and_hides_owner() -> None:
    store = InMemoryAnalysisIntentHandoffStore()
    await store.capture(**capture_args())  # type: ignore[arg-type]

    with pytest.raises(AnalysisIntentHandoffConflict) as exc_info:
        await store.capture(
            **capture_args(intent=intent("housing"))  # type: ignore[arg-type]
        )
    assert exc_info.value.code == "HANDOFF_CONFLICT"
    with pytest.raises(ResourceNotFound):
        await store.get_for_run(
            tenant_id="tenant-b",
            user_id="user-a",
            run_id="run-a",
        )


async def test_memory_lifecycle_is_cas_guarded_and_replayable() -> None:
    store = InMemoryAnalysisIntentHandoffStore()
    captured = await store.capture(**capture_args())  # type: ignore[arg-type]
    waiting = await store.advance(
        tenant_id="tenant-a",
        user_id="user-a",
        run_id="run-a",
        expected_version=captured.version,
        status="waiting_clarification",
        clarification_options=(option(),),
    )
    compiling = await store.advance(
        tenant_id="tenant-a",
        user_id="user-a",
        run_id="run-a",
        expected_version=waiting.version,
        status="compiling",
        clarification_options=waiting.clarification_options,
        selected_area_code="330106",
    )
    compiled = await store.advance(
        tenant_id="tenant-a",
        user_id="user-a",
        run_id="run-a",
        expected_version=compiling.version,
        status="compiled",
        clarification_options=compiling.clarification_options,
        selected_area_code="330106",
        plan_id="plan-a",
        request_id="request-a",
    )
    executing, replayed = await asyncio.gather(
        *[
            store.advance(
                tenant_id="tenant-a",
                user_id="user-a",
                run_id="run-a",
                expected_version=compiled.version,
                status="executing",
                clarification_options=compiled.clarification_options,
                selected_area_code="330106",
                plan_id="plan-a",
                request_id="request-a",
            )
            for _ in range(2)
        ]
    )
    assert executing == replayed
    completed = await store.advance(
        tenant_id="tenant-a",
        user_id="user-a",
        run_id="run-a",
        expected_version=executing.version,
        status="completed",
        clarification_options=executing.clarification_options,
        selected_area_code="330106",
        plan_id="plan-a",
        request_id="request-a",
        report_result_id="result-report-a",
    )

    assert completed.status == "completed"
    assert completed.version == 6
    assert completed.report_result_id == "result-report-a"
    with pytest.raises(RunStateConflict):
        await store.advance(
            tenant_id="tenant-a",
            user_id="user-a",
            run_id="run-a",
            expected_version=completed.version,
            status="failed",
            failure_code="late_failure",
        )


async def test_memory_cannot_replace_clarification_or_plan_authority() -> None:
    store = InMemoryAnalysisIntentHandoffStore()
    captured = await store.capture(**capture_args())  # type: ignore[arg-type]
    waiting = await store.advance(
        tenant_id="tenant-a",
        user_id="user-a",
        run_id="run-a",
        expected_version=captured.version,
        status="waiting_clarification",
        clarification_options=(option(),),
    )
    with pytest.raises(RunStateConflict):
        await store.advance(
            tenant_id="tenant-a",
            user_id="user-a",
            run_id="run-a",
            expected_version=waiting.version,
            status="compiling",
            clarification_options=(
                HandoffClarificationOption(
                    option_id="opt-b",
                    area_code="330108",
                    area_name="滨江区",
                ),
            ),
            selected_area_code="330108",
        )


async def test_memory_rejects_inconsistent_transition_as_run_conflict() -> None:
    store = InMemoryAnalysisIntentHandoffStore()
    captured = await store.capture(**capture_args())  # type: ignore[arg-type]

    with pytest.raises(RunStateConflict):
        await store.advance(
            tenant_id="tenant-a",
            user_id="user-a",
            run_id="run-a",
            expected_version=captured.version,
            status="compiled",
            plan_id="plan-without-request",
        )


def test_handoff_contract_rejects_untrusted_or_inconsistent_state() -> None:
    base = {
        "handoff_id": "ahf-a",
        "tenant_id": "tenant-a",
        "user_id": "user-a",
        "session_id": "session-a",
        "run_id": "run-a",
        "intent": intent("population"),
        "intent_fingerprint": "sha256:" + "a" * 64,
    }
    invalid_payloads = (
        {**base, "status": "compiled", "plan_id": "plan-a"},
        {**base, "status": "waiting_clarification"},
        {**base, "selected_area_code": "330106"},
        {**base, "status": "failed"},
        {**base, "status": "completed", "plan_id": "p", "request_id": "r"},
    )
    for payload in invalid_payloads:
        with pytest.raises(ValidationError):
            AnalysisIntentHandoff.model_validate(payload)


async def test_postgres_store_lifecycle_and_owner_isolation() -> None:
    schema = f"test_handoff_{uuid4().hex[:10]}"
    store = PostgresAnalysisIntentHandoffStore(dsn=postgres_dsn(), schema=schema)
    try:
        captured = await store.capture(**capture_args())  # type: ignore[arg-type]
        replayed = await store.capture(**capture_args())  # type: ignore[arg-type]
        assert captured == replayed
        compiling = await store.advance(
            tenant_id="tenant-a",
            user_id="user-a",
            run_id="run-a",
            expected_version=captured.version,
            status="compiling",
        )
        compiled = await store.advance(
            tenant_id="tenant-a",
            user_id="user-a",
            run_id="run-a",
            expected_version=compiling.version,
            status="compiled",
            plan_id="plan-a",
            request_id="request-a",
        )
        assert compiled.version == 3
        assert (
            await store.get_for_run(
                tenant_id="tenant-a", user_id="user-a", run_id="run-a"
            )
            == compiled
        )
        with pytest.raises(ResourceNotFound):
            await store.get_for_run(
                tenant_id="tenant-b", user_id="user-a", run_id="run-a"
            )
    finally:
        await store.drop_schema()


async def test_postgres_corrupted_payload_fails_closed() -> None:
    schema = f"test_handoff_{uuid4().hex[:10]}"
    dsn = postgres_dsn()
    store = PostgresAnalysisIntentHandoffStore(dsn=dsn, schema=schema)
    try:
        await store.capture(**capture_args())  # type: ignore[arg-type]
        async with await psycopg.AsyncConnection.connect(dsn) as connection:
            await connection.execute(
                f'UPDATE "{schema}".analysis_intent_handoffs '
                "SET intent_json = %s WHERE run_id = %s",
                ('{"goals":["not-a-goal"]}', "run-a"),
            )
        with pytest.raises(AnalysisIntentHandoffConflict) as exc_info:
            await store.get_for_run(
                tenant_id="tenant-a", user_id="user-a", run_id="run-a"
            )
        assert exc_info.value.code == "HANDOFF_STORE_INVALID"
    finally:
        await store.drop_schema()
