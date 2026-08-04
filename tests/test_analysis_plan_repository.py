"""Trusted server-side persistence contracts for ``AnalysisPlan``."""

from __future__ import annotations

import inspect
import json
import os
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest

from full_view_agent.application.analysis_plan_repository import (
    AnalysisPlanRepository,
    AnalysisPlanStoreRejected,
    analysis_plan_namespace,
)
from full_view_agent.application.analysis_planner import AnalysisPlanner
from full_view_agent.domain.analysis_plan import AnalysisPlan
from full_view_agent.infrastructure.analysis_plan_repository import (
    InMemoryAnalysisPlanRepository,
    PostgresAnalysisPlanRepository,
)
from full_view_agent.semantic.catalog import SemanticCatalog

from .test_analysis_planner import FULL_AUTH, area_request


def _plan(*, request_id: str = "request-plan-store"):
    return AnalysisPlanner(SemanticCatalog.default()).plan(
        area_request("population", request_id=request_id),
        authorization=FULL_AUTH,
    )


def _postgres_test_dsn() -> str:
    dsn = os.getenv("FULL_VIEW_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("FULL_VIEW_TEST_DATABASE_URL is not configured")
    return dsn


def test_repository_port_exposes_save_and_get_with_full_namespace() -> None:
    save = inspect.signature(AnalysisPlanRepository.save).parameters
    get = inspect.signature(AnalysisPlanRepository.get).parameters
    latest = inspect.signature(AnalysisPlanRepository.get_latest_for_run).parameters

    assert {"tenant_id", "user_id", "run_id", "plan"} <= set(save)
    assert {"tenant_id", "user_id", "run_id", "plan_id"} <= set(get)
    assert {"tenant_id", "user_id", "run_id"} <= set(latest)


def test_namespace_is_deterministic_and_uses_every_identity_component() -> None:
    base = analysis_plan_namespace(
        tenant_id="tenant-a", user_id="user-a", run_id="run-a", plan_id="plan-a"
    )
    assert base == analysis_plan_namespace(
        tenant_id="tenant-a", user_id="user-a", run_id="run-a", plan_id="plan-a"
    )
    for field in ("tenant_id", "user_id", "run_id", "plan_id"):
        values = {
            "tenant_id": "tenant-a",
            "user_id": "user-a",
            "run_id": "run-a",
            "plan_id": "plan-a",
        }
        values[field] += "-other"
        assert analysis_plan_namespace(**values) != base


def test_forward_migration_uses_portable_text_storage() -> None:
    migration_path = (
        Path(__file__).parents[1] / "scripts/migrations/V003_analysis_plans.sql"
    )
    migration = migration_path.read_text(encoding="utf-8")

    assert "namespace TEXT PRIMARY KEY" in migration
    assert "request_id TEXT NOT NULL" in migration
    assert "plan_json TEXT NOT NULL" in migration
    assert "catalog_version TEXT NOT NULL" in migration
    assert "catalog_fingerprint TEXT NOT NULL" in migration
    assert "JSONB" not in migration.upper()


@pytest.mark.asyncio
async def test_in_memory_save_is_idempotent_and_returns_revalidated_copy() -> None:
    repository = InMemoryAnalysisPlanRepository()
    plan = _plan()

    first = await repository.save(
        tenant_id="tenant-a", user_id="user-a", run_id="run-a", plan=plan
    )
    second = await repository.save(
        tenant_id="tenant-a", user_id="user-a", run_id="run-a", plan=plan
    )
    loaded = await repository.get(
        tenant_id="tenant-a",
        user_id="user-a",
        run_id="run-a",
        plan_id=plan.plan_id,
    )

    assert first == second == loaded == plan
    assert loaded is not plan


@pytest.mark.asyncio
async def test_in_memory_discovers_latest_plan_only_inside_owner_run() -> None:
    repository = InMemoryAnalysisPlanRepository()
    first = _plan(request_id="request-plan-store-first")
    second = _plan(request_id="request-plan-store-second")
    await repository.save(
        tenant_id="tenant-a", user_id="user-a", run_id="run-a", plan=first
    )
    await repository.save(
        tenant_id="tenant-a", user_id="user-a", run_id="run-a", plan=second
    )

    assert await repository.get_latest_for_run(
        tenant_id="tenant-a", user_id="user-a", run_id="run-a"
    ) == second
    assert await repository.get_latest_for_run(
        tenant_id="tenant-a", user_id="user-other", run_id="run-a"
    ) is None


@pytest.mark.asyncio
async def test_in_memory_rejects_plan_whose_id_does_not_match_content() -> None:
    repository = InMemoryAnalysisPlanRepository()
    invalid = _plan().model_copy(update={"request_id": "request-tampered"})

    with pytest.raises(AnalysisPlanStoreRejected) as exc_info:
        await repository.save(
            tenant_id="tenant-a", user_id="user-a", run_id="run-a", plan=invalid
        )

    assert exc_info.value.code == "PLAN_ID_MISMATCH"


@pytest.mark.asyncio
async def test_in_memory_rejects_unvalidated_constructed_plan() -> None:
    repository = InMemoryAnalysisPlanRepository()
    invalid = AnalysisPlan.model_construct(plan_id="sha256:" + "0" * 64)

    with pytest.raises(AnalysisPlanStoreRejected) as exc_info:
        await repository.save(
            tenant_id="tenant-a", user_id="user-a", run_id="run-a", plan=invalid
        )

    assert exc_info.value.code == "PLAN_CONTRACT_INVALID"


@pytest.mark.asyncio
async def test_in_memory_never_overwrites_conflicting_record() -> None:
    repository = InMemoryAnalysisPlanRepository()
    plan = _plan()
    await repository.save(
        tenant_id="tenant-a", user_id="user-a", run_id="run-a", plan=plan
    )
    namespace = analysis_plan_namespace(
        tenant_id="tenant-a",
        user_id="user-a",
        run_id="run-a",
        plan_id=plan.plan_id,
    )
    original = repository._records[namespace]
    repository._records[namespace] = replace(original, plan_json='{"different":true}')

    with pytest.raises(AnalysisPlanStoreRejected) as exc_info:
        await repository.save(
            tenant_id="tenant-a", user_id="user-a", run_id="run-a", plan=plan
        )

    assert exc_info.value.code == "PLAN_CONFLICT"
    assert repository._records[namespace].plan_json == '{"different":true}'


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["tenant_id", "user_id", "run_id"])
async def test_in_memory_does_not_read_across_namespace(field: str) -> None:
    repository = InMemoryAnalysisPlanRepository()
    plan = _plan()
    owner = {"tenant_id": "tenant-a", "user_id": "user-a", "run_id": "run-a"}
    await repository.save(**owner, plan=plan)
    other = dict(owner)
    other[field] += "-other"

    assert await repository.get(**other, plan_id=plan.plan_id) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        (lambda record: replace(record, plan_json="not-json"), "PLAN_JSON_INVALID"),
        (
            lambda record: replace(
                record,
                plan_json=json.dumps(
                    {**json.loads(record.plan_json), "plan_id": "sha256:" + "0" * 64}
                ),
            ),
            "PLAN_ID_MISMATCH",
        ),
        (
            lambda record: replace(record, request_id="request-other"),
            "PLAN_REQUEST_MISMATCH",
        ),
    ],
)
async def test_in_memory_corrupt_records_fail_closed(mutation, code: str) -> None:
    repository = InMemoryAnalysisPlanRepository()
    plan = _plan()
    await repository.save(
        tenant_id="tenant-a", user_id="user-a", run_id="run-a", plan=plan
    )
    namespace = analysis_plan_namespace(
        tenant_id="tenant-a", user_id="user-a", run_id="run-a", plan_id=plan.plan_id
    )
    repository._records[namespace] = mutation(repository._records[namespace])

    with pytest.raises(AnalysisPlanStoreRejected) as exc_info:
        await repository.get(
            tenant_id="tenant-a",
            user_id="user-a",
            run_id="run-a",
            plan_id=plan.plan_id,
        )

    assert exc_info.value.code == code


@pytest.mark.asyncio
async def test_postgres_repository_is_idempotent_isolated_and_survives_restart() -> None:
    schema = f"fva_plan_{uuid4().hex[:12]}"
    dsn = _postgres_test_dsn()
    first = PostgresAnalysisPlanRepository(dsn=dsn, schema=schema)
    await first.initialize()
    try:
        plan = _plan()
        await first.save(
            tenant_id="tenant-a", user_id="user-a", run_id="run-a", plan=plan
        )
        assert await first.save(
            tenant_id="tenant-a", user_id="user-a", run_id="run-a", plan=plan
        ) == plan
        restarted = PostgresAnalysisPlanRepository(dsn=dsn, schema=schema)
        assert await restarted.get(
            tenant_id="tenant-a",
            user_id="user-a",
            run_id="run-a",
            plan_id=plan.plan_id,
        ) == plan
        assert await restarted.get(
            tenant_id="tenant-a",
            user_id="user-other",
            run_id="run-a",
            plan_id=plan.plan_id,
        ) is None
        assert await restarted.get_latest_for_run(
            tenant_id="tenant-a", user_id="user-a", run_id="run-a"
        ) == plan
    finally:
        await first.drop_schema()


@pytest.mark.asyncio
async def test_postgres_corrupt_json_fails_closed() -> None:
    schema = f"fva_plan_{uuid4().hex[:12]}"
    dsn = _postgres_test_dsn()
    repository = PostgresAnalysisPlanRepository(dsn=dsn, schema=schema)
    await repository.initialize()
    try:
        plan = _plan()
        await repository.save(
            tenant_id="tenant-a", user_id="user-a", run_id="run-a", plan=plan
        )
        async with await psycopg.AsyncConnection.connect(dsn) as connection:
            await connection.execute(
                f'UPDATE "{schema}".analysis_plans SET plan_json = %s',
                ("not-json",),
            )
        with pytest.raises(AnalysisPlanStoreRejected) as exc_info:
            await repository.get(
                tenant_id="tenant-a",
                user_id="user-a",
                run_id="run-a",
                plan_id=plan.plan_id,
            )
        assert exc_info.value.code == "PLAN_JSON_INVALID"
        with pytest.raises(AnalysisPlanStoreRejected) as conflict_info:
            await repository.save(
                tenant_id="tenant-a", user_id="user-a", run_id="run-a", plan=plan
            )
        assert conflict_info.value.code == "PLAN_CONFLICT"
    finally:
        await repository.drop_schema()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("column", "value", "code"),
    [
        ("plan_id", "sha256:" + "0" * 64, "PLAN_ID_MISMATCH"),
        ("request_id", "request-other", "PLAN_REQUEST_MISMATCH"),
        ("catalog_version", "catalog-other", "PLAN_CATALOG_MISMATCH"),
    ],
)
async def test_postgres_corrupt_authority_columns_fail_closed(
    column: str, value: str, code: str
) -> None:
    schema = f"fva_plan_{uuid4().hex[:12]}"
    dsn = _postgres_test_dsn()
    repository = PostgresAnalysisPlanRepository(dsn=dsn, schema=schema)
    await repository.initialize()
    try:
        plan = _plan()
        await repository.save(
            tenant_id="tenant-a", user_id="user-a", run_id="run-a", plan=plan
        )
        assert column in {"plan_id", "request_id", "catalog_version"}
        async with await psycopg.AsyncConnection.connect(dsn) as connection:
            await connection.execute(
                f'UPDATE "{schema}".analysis_plans SET {column} = %s',
                (value,),
            )
        with pytest.raises(AnalysisPlanStoreRejected) as exc_info:
            await repository.get(
                tenant_id="tenant-a",
                user_id="user-a",
                run_id="run-a",
                plan_id=plan.plan_id,
            )
        assert exc_info.value.code == code
    finally:
        await repository.drop_schema()
