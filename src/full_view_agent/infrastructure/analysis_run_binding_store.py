# pyright: reportArgumentType=false, reportCallIssue=false

"""In-memory and PostgreSQL stores for durable analysis run bindings."""

from __future__ import annotations

import asyncio
import re

import psycopg
from pydantic import ValidationError

from full_view_agent.application.analysis_run_binding import (
    AnalysisRunBinding,
    AnalysisRunBindingConflict,
    AnalysisRunBindingStatus,
)
from full_view_agent.application.errors import ResourceNotFound, RunStateConflict

_SAFE_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]{0,62}$")


class InMemoryAnalysisRunBindingStore:
    def __init__(self) -> None:
        self._bindings: dict[str, AnalysisRunBinding] = {}
        self._lock = asyncio.Lock()

    async def ensure_binding(
        self,
        *,
        tenant_id: str,
        user_id: str,
        session_id: str,
        run_id: str,
        plan_id: str,
        request_id: str,
        invocation_fingerprint: str,
    ) -> AnalysisRunBinding:
        requested = AnalysisRunBinding(
            tenant_id=tenant_id,
            user_id=user_id,
            session_id=session_id,
            run_id=run_id,
            plan_id=plan_id,
            request_id=request_id,
            invocation_fingerprint=invocation_fingerprint,
        )
        async with self._lock:
            existing = self._bindings.get(run_id)
            if existing is None:
                self._bindings[run_id] = requested
                return requested
            _require_owner(existing, tenant_id=tenant_id, user_id=user_id)
            if not _same_invocation(existing, requested):
                raise AnalysisRunBindingConflict(
                    "BINDING_CONFLICT",
                    "analysis run is already bound to a different invocation",
                )
            return existing

    async def get_binding(
        self,
        *,
        tenant_id: str,
        user_id: str,
        run_id: str,
        invocation_fingerprint: str,
    ) -> AnalysisRunBinding:
        async with self._lock:
            binding = self._bindings.get(run_id)
            if binding is None:
                raise ResourceNotFound("analysis run binding not found")
            _require_owner(binding, tenant_id=tenant_id, user_id=user_id)
            _require_fingerprint(binding, invocation_fingerprint)
            return binding

    async def update_binding(
        self,
        *,
        tenant_id: str,
        user_id: str,
        run_id: str,
        invocation_fingerprint: str,
        expected_version: int,
        status: AnalysisRunBindingStatus,
        report_result_id: str | None,
    ) -> AnalysisRunBinding:
        async with self._lock:
            existing = self._bindings.get(run_id)
            if existing is None:
                raise ResourceNotFound("analysis run binding not found")
            _require_owner(existing, tenant_id=tenant_id, user_id=user_id)
            _require_fingerprint(existing, invocation_fingerprint)
            if existing.version != expected_version:
                if (
                    existing.status == status
                    and existing.report_result_id == report_result_id
                ):
                    return existing
                raise RunStateConflict("analysis run binding version changed")
            updated = AnalysisRunBinding.model_validate(
                {
                    **existing.model_dump(mode="python"),
                    "status": status,
                    "report_result_id": report_result_id,
                    "version": existing.version + 1,
                }
            )
            self._bindings[run_id] = updated
            return updated


class PostgresAnalysisRunBindingStore:
    """Portable PostgreSQL implementation without extension-specific types."""

    def __init__(self, *, dsn: str, schema: str = "full_view_agent") -> None:
        if not _SAFE_IDENTIFIER.fullmatch(schema):
            raise ValueError("PostgreSQL schema must be a safe lower-case identifier")
        self._dsn = dsn
        self._schema = schema
        self._init_lock = asyncio.Lock()
        self._initialized = False

    async def initialize(self) -> None:
        if self._initialized:
            return
        async with self._init_lock:
            if self._initialized:
                return
            async with await psycopg.AsyncConnection.connect(self._dsn) as connection:
                await connection.execute(
                    f'CREATE SCHEMA IF NOT EXISTS "{self._schema}"'
                )
                for statement in self._ddl_statements():
                    await connection.execute(statement)
            self._initialized = True

    async def drop_schema(self) -> None:
        async with await psycopg.AsyncConnection.connect(self._dsn) as connection:
            await connection.execute(f'DROP SCHEMA IF EXISTS "{self._schema}" CASCADE')
        self._initialized = False

    async def ensure_binding(
        self,
        *,
        tenant_id: str,
        user_id: str,
        session_id: str,
        run_id: str,
        plan_id: str,
        request_id: str,
        invocation_fingerprint: str,
    ) -> AnalysisRunBinding:
        requested = AnalysisRunBinding(
            tenant_id=tenant_id,
            user_id=user_id,
            session_id=session_id,
            run_id=run_id,
            plan_id=plan_id,
            request_id=request_id,
            invocation_fingerprint=invocation_fingerprint,
        )
        await self.initialize()
        async with await psycopg.AsyncConnection.connect(self._dsn) as connection:
            await connection.execute(
                f'INSERT INTO "{self._schema}".analysis_run_bindings '
                "(tenant_id, user_id, session_id, run_id, plan_id, request_id, "
                "invocation_fingerprint, status, report_result_id, version) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (run_id) DO NOTHING",
                _binding_values(requested),
            )
            row = await self._select_by_run(connection, run_id)
        if row is None:
            raise AnalysisRunBindingConflict(
                "BINDING_STORE_INVALID", "binding insert was not readable"
            )
        existing = _binding_from_row(row)
        _require_owner(existing, tenant_id=tenant_id, user_id=user_id)
        if not _same_invocation(existing, requested):
            raise AnalysisRunBindingConflict(
                "BINDING_CONFLICT",
                "analysis run is already bound to a different invocation",
            )
        return existing

    async def get_binding(
        self,
        *,
        tenant_id: str,
        user_id: str,
        run_id: str,
        invocation_fingerprint: str,
    ) -> AnalysisRunBinding:
        await self.initialize()
        async with await psycopg.AsyncConnection.connect(self._dsn) as connection:
            row = await self._select_by_run(connection, run_id)
        if row is None:
            raise ResourceNotFound("analysis run binding not found")
        binding = _binding_from_row(row)
        _require_owner(binding, tenant_id=tenant_id, user_id=user_id)
        _require_fingerprint(binding, invocation_fingerprint)
        return binding

    async def update_binding(
        self,
        *,
        tenant_id: str,
        user_id: str,
        run_id: str,
        invocation_fingerprint: str,
        expected_version: int,
        status: AnalysisRunBindingStatus,
        report_result_id: str | None,
    ) -> AnalysisRunBinding:
        await self.initialize()
        async with await psycopg.AsyncConnection.connect(self._dsn) as connection:
            row = await (
                await connection.execute(
                    f'UPDATE "{self._schema}".analysis_run_bindings '
                    "SET status = %s, report_result_id = %s, version = version + 1 "
                    "WHERE run_id = %s AND tenant_id = %s AND user_id = %s "
                    "AND invocation_fingerprint = %s AND version = %s "
                    "RETURNING tenant_id, user_id, session_id, run_id, plan_id, "
                    "request_id, invocation_fingerprint, status, report_result_id, version",
                    (
                        status,
                        report_result_id,
                        run_id,
                        tenant_id,
                        user_id,
                        invocation_fingerprint,
                        expected_version,
                    ),
                )
            ).fetchone()
            if row is not None:
                return _binding_from_row(row)
            existing_row = await self._select_by_run(connection, run_id)
        if existing_row is None:
            raise ResourceNotFound("analysis run binding not found")
        existing = _binding_from_row(existing_row)
        _require_owner(existing, tenant_id=tenant_id, user_id=user_id)
        _require_fingerprint(existing, invocation_fingerprint)
        if existing.status == status and existing.report_result_id == report_result_id:
            return existing
        raise RunStateConflict("analysis run binding version changed")

    async def _select_by_run(self, connection, run_id: str):
        return await (
            await connection.execute(
                f'SELECT tenant_id, user_id, session_id, run_id, plan_id, request_id, '
                "invocation_fingerprint, status, report_result_id, version "
                f'FROM "{self._schema}".analysis_run_bindings WHERE run_id = %s',
                (run_id,),
            )
        ).fetchone()

    def _ddl_statements(self) -> tuple[str, ...]:
        prefix = f'"{self._schema}".'
        return (
            f"CREATE TABLE IF NOT EXISTS {prefix}analysis_run_bindings ("
            "tenant_id TEXT NOT NULL, user_id TEXT NOT NULL, session_id TEXT NOT NULL, "
            "run_id TEXT PRIMARY KEY, plan_id TEXT NOT NULL, request_id TEXT NOT NULL, "
            "invocation_fingerprint TEXT NOT NULL, status TEXT NOT NULL, "
            "report_result_id TEXT, version BIGINT NOT NULL CHECK (version > 0))",
            f"CREATE INDEX IF NOT EXISTS idx_fva_analysis_bindings_owner "
            f"ON {prefix}analysis_run_bindings(tenant_id, user_id, session_id)",
        )


def _binding_values(binding: AnalysisRunBinding) -> tuple[object, ...]:
    return (
        binding.tenant_id,
        binding.user_id,
        binding.session_id,
        binding.run_id,
        binding.plan_id,
        binding.request_id,
        binding.invocation_fingerprint,
        binding.status,
        binding.report_result_id,
        binding.version,
    )


def _binding_from_row(row: tuple[object, ...]) -> AnalysisRunBinding:
    try:
        return AnalysisRunBinding(
            tenant_id=str(row[0]),
            user_id=str(row[1]),
            session_id=str(row[2]),
            run_id=str(row[3]),
            plan_id=str(row[4]),
            request_id=str(row[5]),
            invocation_fingerprint=str(row[6]),
            status=str(row[7]),  # type: ignore[arg-type]
            report_result_id=None if row[8] is None else str(row[8]),
            version=int(str(row[9])),
        )
    except (TypeError, ValueError, ValidationError) as exc:
        raise AnalysisRunBindingConflict(
            "BINDING_STORE_INVALID", "stored binding failed contract validation"
        ) from exc


def _require_owner(
    binding: AnalysisRunBinding, *, tenant_id: str, user_id: str
) -> None:
    if binding.tenant_id != tenant_id or binding.user_id != user_id:
        raise ResourceNotFound("analysis run binding not found")


def _require_fingerprint(binding: AnalysisRunBinding, expected: str) -> None:
    if binding.invocation_fingerprint != expected:
        raise AnalysisRunBindingConflict(
            "BINDING_FINGERPRINT_MISMATCH",
            "analysis invocation fingerprint does not match the stored binding",
        )


def _same_invocation(
    left: AnalysisRunBinding, right: AnalysisRunBinding
) -> bool:
    return (
        left.tenant_id,
        left.user_id,
        left.session_id,
        left.run_id,
        left.plan_id,
        left.request_id,
        left.invocation_fingerprint,
    ) == (
        right.tenant_id,
        right.user_id,
        right.session_id,
        right.run_id,
        right.plan_id,
        right.request_id,
        right.invocation_fingerprint,
    )
