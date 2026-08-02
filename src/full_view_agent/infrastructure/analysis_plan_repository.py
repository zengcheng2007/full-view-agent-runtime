# pyright: reportArgumentType=false, reportCallIssue=false

"""In-memory and PostgreSQL authorities for server-created analysis plans."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from datetime import UTC, datetime

import psycopg

from full_view_agent.application.analysis_plan_repository import (
    AnalysisPlanStoreRejected,
    analysis_plan_namespace,
    validate_plan_for_save,
    validate_stored_plan,
)
from full_view_agent.domain.analysis_plan import AnalysisPlan

_SAFE_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]{0,62}$")


@dataclass(frozen=True)
class _StoredAnalysisPlan:
    namespace: str
    tenant_id: str
    user_id: str
    run_id: str
    plan_id: str
    request_id: str
    plan_json: str
    catalog_version: str
    catalog_fingerprint: str
    created_at: datetime


class InMemoryAnalysisPlanRepository:
    """Process-local implementation with production-equivalent integrity rules."""

    def __init__(self) -> None:
        self._records: dict[str, _StoredAnalysisPlan] = {}
        self._lock = asyncio.Lock()

    async def save(
        self,
        *,
        tenant_id: str,
        user_id: str,
        run_id: str,
        plan: AnalysisPlan,
    ) -> AnalysisPlan:
        validated = validate_plan_for_save(plan)
        record = _record_from_plan(
            tenant_id=tenant_id,
            user_id=user_id,
            run_id=run_id,
            plan=validated,
        )
        async with self._lock:
            existing = self._records.get(record.namespace)
            if existing is None:
                self._records[record.namespace] = record
            elif not _same_record_content(existing, record):
                raise AnalysisPlanStoreRejected(
                    "PLAN_CONFLICT", "plan identity already stores different content"
                )
        return _validated_record(record)

    async def get(
        self,
        *,
        tenant_id: str,
        user_id: str,
        run_id: str,
        plan_id: str,
    ) -> AnalysisPlan | None:
        namespace = analysis_plan_namespace(
            tenant_id=tenant_id,
            user_id=user_id,
            run_id=run_id,
            plan_id=plan_id,
        )
        record = self._records.get(namespace)
        if record is None:
            return None
        if (
            record.namespace != namespace
            or record.tenant_id != tenant_id
            or record.user_id != user_id
            or record.run_id != run_id
            or record.plan_id != plan_id
        ):
            raise AnalysisPlanStoreRejected(
                "PLAN_NAMESPACE_MISMATCH", "stored namespace metadata is inconsistent"
            )
        return _validated_record(record)


class PostgresAnalysisPlanRepository:
    """Portable TEXT/standard-column PostgreSQL implementation.

    No PostgreSQL extension or JSON-specific operator is used, preserving a
    straightforward future migration path to Kingbase.
    """

    def __init__(self, *, dsn: str, schema: str = "full_view_agent") -> None:
        if not _SAFE_IDENTIFIER.fullmatch(schema):
            raise ValueError("PostgreSQL schema must be a safe lower-case identifier")
        self._dsn = dsn
        self._schema = schema
        self._init_lock = asyncio.Lock()
        self._initialized = False

    async def initialize(self) -> None:
        await self._ensure_initialized()

    async def drop_schema(self) -> None:
        async with await psycopg.AsyncConnection.connect(self._dsn) as connection:
            await connection.execute(f'DROP SCHEMA IF EXISTS "{self._schema}" CASCADE')
        self._initialized = False

    async def save(
        self,
        *,
        tenant_id: str,
        user_id: str,
        run_id: str,
        plan: AnalysisPlan,
    ) -> AnalysisPlan:
        validated = validate_plan_for_save(plan)
        record = _record_from_plan(
            tenant_id=tenant_id,
            user_id=user_id,
            run_id=run_id,
            plan=validated,
        )
        await self._ensure_initialized()
        async with await psycopg.AsyncConnection.connect(self._dsn) as connection:
            inserted = await (
                await connection.execute(
                    f'INSERT INTO "{self._schema}".analysis_plans '
                    "(namespace, tenant_id, user_id, run_id, plan_id, request_id, plan_json, "
                    "catalog_version, catalog_fingerprint, created_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT DO NOTHING RETURNING namespace",
                    _record_values(record),
                )
            ).fetchone()
            if inserted is None:
                row = await (
                    await connection.execute(
                        f'SELECT namespace, tenant_id, user_id, run_id, plan_id, request_id, '
                        f'plan_json, catalog_version, catalog_fingerprint, created_at '
                        f'FROM "{self._schema}".analysis_plans WHERE namespace = %s FOR UPDATE',
                        (record.namespace,),
                    )
                ).fetchone()
                if row is None or not _same_record_content(_record_from_row(row), record):
                    raise AnalysisPlanStoreRejected(
                        "PLAN_CONFLICT", "plan identity already stores different content"
                    )
        return _validated_record(record)

    async def get(
        self,
        *,
        tenant_id: str,
        user_id: str,
        run_id: str,
        plan_id: str,
    ) -> AnalysisPlan | None:
        namespace = analysis_plan_namespace(
            tenant_id=tenant_id,
            user_id=user_id,
            run_id=run_id,
            plan_id=plan_id,
        )
        await self._ensure_initialized()
        async with await psycopg.AsyncConnection.connect(self._dsn) as connection:
            row = await (
                await connection.execute(
                    f'SELECT namespace, tenant_id, user_id, run_id, plan_id, request_id, '
                    f'plan_json, catalog_version, catalog_fingerprint, created_at '
                    f'FROM "{self._schema}".analysis_plans '
                    "WHERE namespace = %s",
                    (namespace,),
                )
            ).fetchone()
        if row is None:
            return None
        record = _record_from_row(row)
        if (
            record.namespace != namespace
            or record.tenant_id != tenant_id
            or record.user_id != user_id
            or record.run_id != run_id
        ):
            raise AnalysisPlanStoreRejected(
                "PLAN_NAMESPACE_MISMATCH", "stored namespace metadata is inconsistent"
            )
        if record.plan_id != plan_id:
            raise AnalysisPlanStoreRejected(
                "PLAN_ID_MISMATCH", "stored plan id does not match namespace identity"
            )
        return _validated_record(record)

    async def _ensure_initialized(self) -> None:
        if self._initialized:
            return
        async with self._init_lock:
            if self._initialized:
                return
            async with await psycopg.AsyncConnection.connect(self._dsn) as connection:
                await connection.execute(f'CREATE SCHEMA IF NOT EXISTS "{self._schema}"')
                for statement in self._ddl_statements():
                    await connection.execute(statement)
            self._initialized = True

    def _ddl_statements(self) -> tuple[str, ...]:
        prefix = f'"{self._schema}".'
        return (
            f"CREATE TABLE IF NOT EXISTS {prefix}analysis_plans ("
            "namespace TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, user_id TEXT NOT NULL, "
            "run_id TEXT NOT NULL, plan_id TEXT NOT NULL, request_id TEXT NOT NULL, "
            "plan_json TEXT NOT NULL, catalog_version TEXT NOT NULL, "
            "catalog_fingerprint TEXT NOT NULL, created_at TIMESTAMP WITH TIME ZONE NOT NULL)",
            f"CREATE UNIQUE INDEX IF NOT EXISTS idx_fva_analysis_plans_scope "
            f"ON {prefix}analysis_plans(tenant_id, user_id, run_id, plan_id)",
            f"CREATE INDEX IF NOT EXISTS idx_fva_analysis_plans_request "
            f"ON {prefix}analysis_plans(tenant_id, user_id, run_id, request_id)",
        )


def _record_from_plan(
    *, tenant_id: str, user_id: str, run_id: str, plan: AnalysisPlan
) -> _StoredAnalysisPlan:
    return _StoredAnalysisPlan(
        namespace=analysis_plan_namespace(
            tenant_id=tenant_id,
            user_id=user_id,
            run_id=run_id,
            plan_id=plan.plan_id,
        ),
        tenant_id=tenant_id,
        user_id=user_id,
        run_id=run_id,
        plan_id=plan.plan_id,
        request_id=plan.request_id,
        plan_json=plan.model_dump_json(),
        catalog_version=plan.catalog_version,
        catalog_fingerprint=plan.catalog_fingerprint,
        created_at=datetime.now(UTC),
    )


def _record_values(record: _StoredAnalysisPlan) -> tuple[object, ...]:
    return (
        record.namespace,
        record.tenant_id,
        record.user_id,
        record.run_id,
        record.plan_id,
        record.request_id,
        record.plan_json,
        record.catalog_version,
        record.catalog_fingerprint,
        record.created_at,
    )


def _record_from_row(row: tuple[object, ...]) -> _StoredAnalysisPlan:
    return _StoredAnalysisPlan(
        namespace=str(row[0]),
        tenant_id=str(row[1]),
        user_id=str(row[2]),
        run_id=str(row[3]),
        plan_id=str(row[4]),
        request_id=str(row[5]),
        plan_json=str(row[6]),
        catalog_version=str(row[7]),
        catalog_fingerprint=str(row[8]),
        created_at=row[9],  # type: ignore[arg-type]
    )


def _same_record_content(left: _StoredAnalysisPlan, right: _StoredAnalysisPlan) -> bool:
    return (
        left.namespace,
        left.tenant_id,
        left.user_id,
        left.run_id,
        left.plan_id,
        left.request_id,
        left.plan_json,
        left.catalog_version,
        left.catalog_fingerprint,
    ) == (
        right.namespace,
        right.tenant_id,
        right.user_id,
        right.run_id,
        right.plan_id,
        right.request_id,
        right.plan_json,
        right.catalog_version,
        right.catalog_fingerprint,
    )


def _validated_record(record: _StoredAnalysisPlan) -> AnalysisPlan:
    return validate_stored_plan(
        plan_json=record.plan_json,
        expected_plan_id=record.plan_id,
        stored_request_id=record.request_id,
        stored_catalog_version=record.catalog_version,
        stored_catalog_fingerprint=record.catalog_fingerprint,
    )
