"""Crash-safe, at-most-once persistence for Analysis Graph steps."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql

from full_view_agent.application.analysis_observation_validator import (
    AgentStoreAnalysisObservationValidator,
)
from full_view_agent.application.analysis_step_ledger import (
    AnalysisStepLedgerConflict,
    analysis_step_tool_call_id,
)
from full_view_agent.application.errors import ResourceNotFound, RunStateConflict
from full_view_agent.application.tool_observation_service import ToolObservationService
from full_view_agent.application.tool_registry import ToolRegistry
from full_view_agent.infrastructure.analysis_step_ledger_store import (
    InMemoryAnalysisStepLedgerStore,
    PostgresAnalysisStepLedgerStore,
)
from full_view_agent.infrastructure.event_broker import InMemoryEventBroker
from full_view_agent.infrastructure.memory_store import InMemoryAgentStore

from .test_tool_observation_service import _action, _running_run, _tool_result

FINGERPRINT_A = "sha256:" + "a" * 64
FINGERPRINT_B = "sha256:" + "b" * 64


class _AcceptingObservationValidator:
    async def validate(self, **_kwargs) -> None:
        return None


def _reserve_args(**overrides: str) -> dict[str, str]:
    values = {
        "tenant_id": "tenant-a",
        "user_id": "user-a",
        "run_id": "run-a",
        "plan_id": "plan-a",
        "request_id": "request-a",
        "step_id": "step-population",
        "tool_call_id": analysis_step_tool_call_id(
            tenant_id="tenant-a",
            run_id="run-a",
            plan_id="plan-a",
            step_id="step-population",
        ),
        "invocation_fingerprint": FINGERPRINT_A,
    }
    values.update(overrides)
    if "tool_call_id" not in overrides:
        values["tool_call_id"] = analysis_step_tool_call_id(
            tenant_id=values["tenant_id"],
            run_id=values["run_id"],
            plan_id=values["plan_id"],
            step_id=values["step_id"],
        )
    return values


def _postgres_test_dsn() -> str:
    dsn = os.getenv("FULL_VIEW_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("FULL_VIEW_TEST_DATABASE_URL is not configured")
    return dsn


def test_v005_migration_is_portable_and_has_step_authority_columns() -> None:
    migration = (
        Path(__file__).parents[1]
        / "scripts/migrations/V005_analysis_step_ledger.sql"
    ).read_text(encoding="utf-8")

    for column in (
        "tenant_id TEXT NOT NULL",
        "user_id TEXT NOT NULL",
        "run_id TEXT NOT NULL",
        "plan_id TEXT NOT NULL",
        "request_id TEXT NOT NULL",
        "step_id TEXT NOT NULL",
        "tool_call_id TEXT NOT NULL",
        "invocation_fingerprint TEXT NOT NULL",
        "status TEXT NOT NULL",
        "result_id TEXT",
        "evidence_ids TEXT NOT NULL",
        "version BIGINT NOT NULL",
    ):
        assert column in migration
    assert "PRIMARY KEY (run_id, step_id)" in migration
    assert "JSONB" not in migration.upper()
    assert migration.strip().startswith("-- Migration V005")
    assert "BEGIN;" in migration
    assert "WHERE version = 5" in migration
    assert "result_status" not in migration
    assert migration.strip().endswith("COMMIT;")


def test_v006_migration_upgrades_outcomes_without_rewriting_v005() -> None:
    migration = (
        Path(__file__).parents[1]
        / "scripts/migrations/V006_analysis_step_outcomes.sql"
    ).read_text(encoding="utf-8")

    assert "ADD COLUMN IF NOT EXISTS result_status TEXT" in migration
    assert "ADD COLUMN IF NOT EXISTS reason_code TEXT" in migration
    assert "waiting_reauth" in migration
    assert "synthetic" in migration
    assert "WHERE version = 6" in migration
    assert migration.strip().endswith("COMMIT;")


def test_tool_call_identity_is_scoped_to_tenant_and_run() -> None:
    base = analysis_step_tool_call_id(
        tenant_id="tenant-a",
        run_id="run-a",
        plan_id="plan-a",
        step_id="step-population",
    )
    assert base != analysis_step_tool_call_id(
        tenant_id="tenant-b",
        run_id="run-a",
        plan_id="plan-a",
        step_id="step-population",
    )
    assert base != analysis_step_tool_call_id(
        tenant_id="tenant-a",
        run_id="run-b",
        plan_id="plan-a",
        step_id="step-population",
    )


@pytest.mark.asyncio
async def test_memory_rejects_unverified_persisted_references() -> None:
    store = InMemoryAnalysisStepLedgerStore()
    reserved = await store.reserve_step(**_reserve_args())
    executing = await store.transition_step(
        **_transition_identity(),
        expected_version=reserved.version,
        status="executing",
        result_id=None,
        evidence_ids=(),
    )
    with pytest.raises(AnalysisStepLedgerConflict) as exc_info:
        await store.transition_step(
            **_transition_identity(),
            expected_version=executing.version,
            status="persisted",
            result_status="success",
            reason_code="STEP_TEST",
            result_id="does-not-exist",
            evidence_ids=("does-not-exist",),
        )
    assert exc_info.value.code == "STEP_OBSERVATION_UNVERIFIED"


@pytest.mark.asyncio
async def test_agent_store_validator_accepts_only_the_durable_observation() -> None:
    agent_store = InMemoryAgentStore()
    _service, run = await _running_run(agent_store)
    tool_call_id = analysis_step_tool_call_id(
        tenant_id="tenant-a",
        run_id=run.run_id,
        plan_id="plan-a",
        step_id="step-population",
    )
    observation = await ToolObservationService(
        store=agent_store,
        events=InMemoryEventBroker(),
        registry=ToolRegistry.default(),
        evidence_source_system="test-source",
    ).persist(
        user_id="user-01",
        run=run,
        action=_action(),
        tool_result=_tool_result().model_copy(update={"tool_call_id": tool_call_id}),
    )
    ledger = InMemoryAnalysisStepLedgerStore(
        observation_validator=AgentStoreAnalysisObservationValidator(agent_store)
    )
    reserved = await ledger.reserve_step(
        tenant_id="tenant-a",
        user_id="user-01",
        run_id=run.run_id,
        plan_id="plan-a",
        request_id="request-a",
        step_id="step-population",
        tool_call_id=tool_call_id,
        invocation_fingerprint=FINGERPRINT_A,
    )
    executing = await ledger.transition_step(
        tenant_id="tenant-a",
        user_id="user-01",
        run_id=run.run_id,
        step_id="step-population",
        invocation_fingerprint=FINGERPRINT_A,
        expected_version=reserved.version,
        status="executing",
        result_id=None,
        evidence_ids=(),
    )

    observed = await ledger.transition_step(
        tenant_id="tenant-a",
        user_id="user-01",
        run_id=run.run_id,
        step_id="step-population",
        invocation_fingerprint=FINGERPRINT_A,
        expected_version=executing.version,
        status="observed",
        result_status="success",
        reason_code="STEP_TEST",
        result_id=None,
        evidence_ids=(),
    )
    persisted = await ledger.transition_step(
        tenant_id="tenant-a",
        user_id="user-01",
        run_id=run.run_id,
        step_id="step-population",
        invocation_fingerprint=FINGERPRINT_A,
        expected_version=observed.version,
        status="persisted",
        result_status="success",
        reason_code="STEP_TEST",
        result_id=observation.data_result.result_id,
        evidence_ids=(observation.evidence.evidence_id,),
    )

    assert persisted.result_id == observation.data_result.result_id


@pytest.mark.asyncio
async def test_memory_reserve_is_concurrently_idempotent() -> None:
    store = InMemoryAnalysisStepLedgerStore()

    first, second = await asyncio.gather(
        store.reserve_step(**_reserve_args()),
        store.reserve_step(**_reserve_args()),
    )

    assert first == second
    assert first.status == "reserved"
    assert first.result_id is None
    assert first.evidence_ids == ()
    assert first.version == 1


@pytest.mark.asyncio
async def test_memory_rejects_non_deterministic_tool_call_id() -> None:
    store = InMemoryAnalysisStepLedgerStore()

    with pytest.raises(AnalysisStepLedgerConflict) as exc_info:
        await store.reserve_step(**_reserve_args(tool_call_id="sha256:" + "0" * 64))

    assert exc_info.value.code == "STEP_TOOL_CALL_ID_MISMATCH"


@pytest.mark.asyncio
async def test_memory_hides_step_from_another_tenant() -> None:
    store = InMemoryAnalysisStepLedgerStore()
    await store.reserve_step(**_reserve_args())

    with pytest.raises(ResourceNotFound):
        await store.get_step(
            tenant_id="tenant-b",
            user_id="user-a",
            run_id="run-a",
            step_id="step-population",
            invocation_fingerprint=FINGERPRINT_A,
        )
    with pytest.raises(ResourceNotFound):
        await store.reserve_step(**_reserve_args(tenant_id="tenant-b"))


@pytest.mark.asyncio
async def test_memory_wrong_invocation_fingerprint_fails_closed() -> None:
    store = InMemoryAnalysisStepLedgerStore()
    await store.reserve_step(**_reserve_args())

    with pytest.raises(AnalysisStepLedgerConflict) as exc_info:
        await store.get_step(
            tenant_id="tenant-a",
            user_id="user-a",
            run_id="run-a",
            step_id="step-population",
            invocation_fingerprint=FINGERPRINT_B,
        )

    assert exc_info.value.code == "STEP_INVOCATION_MISMATCH"


@pytest.mark.asyncio
async def test_memory_persisted_replay_returns_same_references() -> None:
    store = InMemoryAnalysisStepLedgerStore(
        observation_validator=_AcceptingObservationValidator()
    )
    reserved = await store.reserve_step(**_reserve_args())
    executing = await store.transition_step(
        **_transition_identity(),
        expected_version=reserved.version,
        status="executing",
        result_id=None,
        evidence_ids=(),
    )
    observed = await store.transition_step(
        **_transition_identity(),
        expected_version=executing.version,
        status="observed",
        result_status="success",
        reason_code="STEP_TEST",
        result_id=None,
        evidence_ids=(),
    )
    persisted = await store.transition_step(
        **_transition_identity(),
        expected_version=observed.version,
        status="persisted",
        result_status="success",
        reason_code="STEP_TEST",
        result_id="result-a",
        evidence_ids=("evidence-a", "evidence-b"),
    )
    replayed = await store.transition_step(
        **_transition_identity(),
        expected_version=observed.version,
        status="persisted",
        result_status="success",
        reason_code="STEP_TEST",
        result_id="result-a",
        evidence_ids=("evidence-a", "evidence-b"),
    )

    assert persisted == replayed
    assert persisted.result_id == "result-a"
    assert persisted.evidence_ids == ("evidence-a", "evidence-b")
    assert persisted.version == 4


@pytest.mark.asyncio
async def test_memory_recovery_atomically_marks_executing_indeterminate() -> None:
    store = InMemoryAnalysisStepLedgerStore()
    reserved = await store.reserve_step(**_reserve_args())
    executing = await store.transition_step(
        **_transition_identity(),
        expected_version=reserved.version,
        status="executing",
        result_id=None,
        evidence_ids=(),
    )

    recovered = await store.mark_indeterminate_if_executing(
        **_transition_identity(), expected_version=executing.version
    )

    assert recovered.status == "indeterminate"
    assert recovered.version == 3
    with pytest.raises(AnalysisStepLedgerConflict) as exc_info:
        await store.transition_step(
            **_transition_identity(),
            expected_version=recovered.version,
            status="executing",
            result_id=None,
            evidence_ids=(),
        )
    assert exc_info.value.code == "STEP_TRANSITION_INVALID"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "result_id", "evidence_ids"),
    [
        ("reserved", "result-a", ()),
        ("executing", None, ("evidence-a",)),
        ("persisted", None, ("evidence-a",)),
        ("persisted", "result-a", ()),
        ("failed", "result-a", ("evidence-a",)),
    ],
)
async def test_memory_rejects_illegal_status_result_combinations(
    status: str, result_id: str | None, evidence_ids: tuple[str, ...]
) -> None:
    store = InMemoryAnalysisStepLedgerStore()
    reserved = await store.reserve_step(**_reserve_args())

    with pytest.raises(AnalysisStepLedgerConflict) as exc_info:
        await store.transition_step(
            **_transition_identity(),
            expected_version=reserved.version,
            status=status,  # type: ignore[arg-type]
            result_id=result_id,
            evidence_ids=evidence_ids,
        )

    assert exc_info.value.code in {
        "STEP_STATE_INVALID",
        "STEP_TRANSITION_INVALID",
    }


@pytest.mark.asyncio
async def test_memory_rejects_stale_cas_and_backward_transition() -> None:
    store = InMemoryAnalysisStepLedgerStore()
    reserved = await store.reserve_step(**_reserve_args())
    executing = await store.transition_step(
        **_transition_identity(),
        expected_version=reserved.version,
        status="executing",
        result_id=None,
        evidence_ids=(),
    )

    with pytest.raises(RunStateConflict):
        await store.transition_step(
            **_transition_identity(),
            expected_version=reserved.version,
            status="failed",
            result_id=None,
            evidence_ids=(),
        )
    with pytest.raises(AnalysisStepLedgerConflict) as exc_info:
        await store.transition_step(
            **_transition_identity(),
            expected_version=executing.version,
            status="reserved",
            result_id=None,
            evidence_ids=(),
        )
    assert exc_info.value.code == "STEP_TRANSITION_INVALID"


@pytest.mark.asyncio
async def test_postgres_concurrency_isolation_recovery_and_restart() -> None:
    schema = f"fva_step_{uuid4().hex[:12]}"
    dsn = _postgres_test_dsn()
    store = PostgresAnalysisStepLedgerStore(dsn=dsn, schema=schema)
    await store.initialize()
    try:
        async with await psycopg.AsyncConnection.connect(dsn) as connection:
            version = await (
                await connection.execute(
                    sql.SQL(
                        "SELECT version FROM {}.schema_version WHERE version = 6"
                    ).format(sql.Identifier(schema))
                )
            ).fetchone()
        assert version == (6,)
        first, second = await asyncio.gather(
            store.reserve_step(**_reserve_args()),
            store.reserve_step(**_reserve_args()),
        )
        assert first == second

        with pytest.raises(ResourceNotFound):
            await store.reserve_step(**_reserve_args(tenant_id="tenant-b"))
        with pytest.raises(AnalysisStepLedgerConflict):
            await store.get_step(
                tenant_id="tenant-a",
                user_id="user-a",
                run_id="run-a",
                step_id="step-population",
                invocation_fingerprint=FINGERPRINT_B,
            )

        executing = await store.transition_step(
            **_transition_identity(),
            expected_version=first.version,
            status="executing",
            result_id=None,
            evidence_ids=(),
        )
        restarted = PostgresAnalysisStepLedgerStore(dsn=dsn, schema=schema)
        recovered = await restarted.mark_indeterminate_if_executing(
            **_transition_identity(), expected_version=executing.version
        )
        assert recovered.status == "indeterminate"
        assert await restarted.get_step(
            tenant_id="tenant-a",
            user_id="user-a",
            run_id="run-a",
            step_id="step-population",
            invocation_fingerprint=FINGERPRINT_A,
        ) == recovered
    finally:
        await store.drop_schema()


@pytest.mark.asyncio
async def test_postgres_rejects_unverified_persisted_references() -> None:
    schema = f"fva_step_{uuid4().hex[:12]}"
    store = PostgresAnalysisStepLedgerStore(
        dsn=_postgres_test_dsn(), schema=schema
    )
    await store.initialize()
    try:
        reserved = await store.reserve_step(**_reserve_args())
        executing = await store.transition_step(
            **_transition_identity(),
            expected_version=reserved.version,
            status="executing",
            result_id=None,
            evidence_ids=(),
        )
        with pytest.raises(AnalysisStepLedgerConflict) as exc_info:
            await store.transition_step(
                **_transition_identity(),
                expected_version=executing.version,
                status="persisted",
                result_status="success",
                reason_code="STEP_TEST",
                result_id="does-not-exist",
                evidence_ids=("does-not-exist",),
            )
        assert exc_info.value.code == "STEP_OBSERVATION_UNVERIFIED"
        current = await store.get_step(
            **_transition_identity(),
        )
        assert current.status == "executing"
    finally:
        await store.drop_schema()


@pytest.mark.asyncio
async def test_postgres_persisted_replay_survives_restart() -> None:
    schema = f"fva_step_{uuid4().hex[:12]}"
    dsn = _postgres_test_dsn()
    validator = _AcceptingObservationValidator()
    store = PostgresAnalysisStepLedgerStore(
        dsn=dsn, schema=schema, observation_validator=validator
    )
    await store.initialize()
    try:
        reserved = await store.reserve_step(**_reserve_args())
        executing = await store.transition_step(
            **_transition_identity(),
            expected_version=reserved.version,
            status="executing",
            result_id=None,
            evidence_ids=(),
        )
        observed = await store.transition_step(
            **_transition_identity(),
            expected_version=executing.version,
            status="observed",
            result_status="partial",
            reason_code="STEP_PARTIAL",
            result_id=None,
            evidence_ids=(),
        )
        persisted = await store.transition_step(
            **_transition_identity(),
            expected_version=observed.version,
            status="persisted",
            result_status="partial",
            reason_code="STEP_PARTIAL",
            result_id="result-a",
            evidence_ids=("evidence-a",),
        )
        restarted = PostgresAnalysisStepLedgerStore(
            dsn=dsn, schema=schema, observation_validator=validator
        )
        replayed = await restarted.transition_step(
            **_transition_identity(),
            expected_version=observed.version,
            status="persisted",
            result_status="partial",
            reason_code="STEP_PARTIAL",
            result_id="result-a",
            evidence_ids=("evidence-a",),
        )
        assert replayed == persisted
    finally:
        await store.drop_schema()


def _transition_identity() -> dict[str, str]:
    return {
        "tenant_id": "tenant-a",
        "user_id": "user-a",
        "run_id": "run-a",
        "step_id": "step-population",
        "invocation_fingerprint": FINGERPRINT_A,
    }
