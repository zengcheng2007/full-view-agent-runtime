# pyright: reportArgumentType=false, reportCallIssue=false

"""In-memory and portable PostgreSQL analysis step ledgers."""

from __future__ import annotations

import asyncio
import json
import re

import psycopg
from pydantic import ValidationError

from full_view_agent.application.analysis_step_ledger import (
    AnalysisStepLedgerConflict,
    AnalysisStepLedgerEntry,
    AnalysisStepLedgerStatus,
    AnalysisStepObservationValidator,
    analysis_step_tool_call_id,
)
from full_view_agent.application.errors import ResourceNotFound, RunStateConflict

_SAFE_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
_TRANSITIONS: dict[AnalysisStepLedgerStatus, frozenset[AnalysisStepLedgerStatus]] = {
    "reserved": frozenset({"executing"}),
    "executing": frozenset({"persisted", "indeterminate", "failed"}),
    "persisted": frozenset(),
    "indeterminate": frozenset(),
    "failed": frozenset(),
}


class InMemoryAnalysisStepLedgerStore:
    def __init__(
        self, *, observation_validator: AnalysisStepObservationValidator | None = None
    ) -> None:
        self._entries: dict[tuple[str, str], AnalysisStepLedgerEntry] = {}
        self._lock = asyncio.Lock()
        self._observation_validator = observation_validator

    async def reserve_step(
        self,
        *,
        tenant_id: str,
        user_id: str,
        run_id: str,
        plan_id: str,
        request_id: str,
        step_id: str,
        tool_call_id: str,
        invocation_fingerprint: str,
    ) -> AnalysisStepLedgerEntry:
        requested = _reserved_entry(
            tenant_id=tenant_id,
            user_id=user_id,
            run_id=run_id,
            plan_id=plan_id,
            request_id=request_id,
            step_id=step_id,
            tool_call_id=tool_call_id,
            invocation_fingerprint=invocation_fingerprint,
        )
        key = (run_id, step_id)
        async with self._lock:
            existing = self._entries.get(key)
            if existing is None:
                self._entries[key] = requested
                return requested
            _require_owner(existing, tenant_id=tenant_id, user_id=user_id)
            if not _same_invocation(existing, requested):
                raise AnalysisStepLedgerConflict(
                    "STEP_LEDGER_CONFLICT",
                    "analysis step is already reserved for another invocation",
                )
            return existing

    async def get_step(
        self,
        *,
        tenant_id: str,
        user_id: str,
        run_id: str,
        step_id: str,
        invocation_fingerprint: str,
    ) -> AnalysisStepLedgerEntry:
        async with self._lock:
            entry = self._entries.get((run_id, step_id))
            if entry is None:
                raise ResourceNotFound("analysis step ledger entry not found")
            _require_owner(entry, tenant_id=tenant_id, user_id=user_id)
            _require_invocation(entry, invocation_fingerprint)
            return entry

    async def transition_step(
        self,
        *,
        tenant_id: str,
        user_id: str,
        run_id: str,
        step_id: str,
        invocation_fingerprint: str,
        expected_version: int,
        status: AnalysisStepLedgerStatus,
        result_id: str | None,
        evidence_ids: tuple[str, ...],
    ) -> AnalysisStepLedgerEntry:
        await self._validate_persisted_observation(
            tenant_id=tenant_id,
            user_id=user_id,
            run_id=run_id,
            step_id=step_id,
            status=status,
            result_id=result_id,
            evidence_ids=evidence_ids,
        )
        async with self._lock:
            existing = self._entries.get((run_id, step_id))
            if existing is None:
                raise ResourceNotFound("analysis step ledger entry not found")
            updated = _validated_transition(
                existing,
                tenant_id=tenant_id,
                user_id=user_id,
                invocation_fingerprint=invocation_fingerprint,
                expected_version=expected_version,
                status=status,
                result_id=result_id,
                evidence_ids=evidence_ids,
            )
            if updated is not existing:
                self._entries[(run_id, step_id)] = updated
            return updated

    async def _validate_persisted_observation(
        self,
        *,
        tenant_id: str,
        user_id: str,
        run_id: str,
        step_id: str,
        status: AnalysisStepLedgerStatus,
        result_id: str | None,
        evidence_ids: tuple[str, ...],
    ) -> None:
        if status != "persisted" or result_id is None or not evidence_ids:
            return
        entry = self._entries.get((run_id, step_id))
        if entry is None:
            raise ResourceNotFound("analysis step ledger entry not found")
        if self._observation_validator is None:
            raise AnalysisStepLedgerConflict(
                "STEP_OBSERVATION_UNVERIFIED",
                "persisted step references require a trusted observation validator",
            )
        await self._observation_validator.validate(
            tenant_id=tenant_id,
            user_id=user_id,
            run_id=run_id,
            tool_call_id=entry.tool_call_id,
            result_id=result_id,
            evidence_ids=evidence_ids,
        )

    async def mark_indeterminate_if_executing(
        self,
        *,
        tenant_id: str,
        user_id: str,
        run_id: str,
        step_id: str,
        invocation_fingerprint: str,
        expected_version: int,
    ) -> AnalysisStepLedgerEntry:
        return await self.transition_step(
            tenant_id=tenant_id,
            user_id=user_id,
            run_id=run_id,
            step_id=step_id,
            invocation_fingerprint=invocation_fingerprint,
            expected_version=expected_version,
            status="indeterminate",
            result_id=None,
            evidence_ids=(),
        )


class PostgresAnalysisStepLedgerStore:
    def __init__(
        self,
        *,
        dsn: str,
        schema: str = "full_view_agent",
        observation_validator: AnalysisStepObservationValidator | None = None,
    ) -> None:
        if not _SAFE_IDENTIFIER.fullmatch(schema):
            raise ValueError("PostgreSQL schema must be a safe lower-case identifier")
        self._dsn = dsn
        self._schema = schema
        self._init_lock = asyncio.Lock()
        self._initialized = False
        self._observation_validator = observation_validator

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

    async def reserve_step(
        self,
        *,
        tenant_id: str,
        user_id: str,
        run_id: str,
        plan_id: str,
        request_id: str,
        step_id: str,
        tool_call_id: str,
        invocation_fingerprint: str,
    ) -> AnalysisStepLedgerEntry:
        requested = _reserved_entry(
            tenant_id=tenant_id,
            user_id=user_id,
            run_id=run_id,
            plan_id=plan_id,
            request_id=request_id,
            step_id=step_id,
            tool_call_id=tool_call_id,
            invocation_fingerprint=invocation_fingerprint,
        )
        await self.initialize()
        async with await psycopg.AsyncConnection.connect(self._dsn) as connection:
            await connection.execute(
                f'INSERT INTO "{self._schema}".analysis_step_ledger '
                "(tenant_id, user_id, run_id, plan_id, request_id, step_id, "
                "tool_call_id, invocation_fingerprint, status, result_id, "
                "evidence_ids, version) VALUES (%s, %s, %s, %s, %s, %s, %s, "
                "%s, %s, %s, %s, %s) ON CONFLICT (run_id, step_id) DO NOTHING",
                _entry_values(requested),
            )
            row = await self._select_entry(connection, run_id=run_id, step_id=step_id)
        if row is None:
            raise AnalysisStepLedgerConflict(
                "STEP_STORE_INVALID", "reserved step was not readable"
            )
        existing = _entry_from_row(row)
        _require_owner(existing, tenant_id=tenant_id, user_id=user_id)
        if not _same_invocation(existing, requested):
            raise AnalysisStepLedgerConflict(
                "STEP_LEDGER_CONFLICT",
                "analysis step is already reserved for another invocation",
            )
        return existing

    async def get_step(
        self,
        *,
        tenant_id: str,
        user_id: str,
        run_id: str,
        step_id: str,
        invocation_fingerprint: str,
    ) -> AnalysisStepLedgerEntry:
        await self.initialize()
        async with await psycopg.AsyncConnection.connect(self._dsn) as connection:
            row = await self._select_entry(connection, run_id=run_id, step_id=step_id)
        if row is None:
            raise ResourceNotFound("analysis step ledger entry not found")
        entry = _entry_from_row(row)
        _require_owner(entry, tenant_id=tenant_id, user_id=user_id)
        _require_invocation(entry, invocation_fingerprint)
        return entry

    async def transition_step(
        self,
        *,
        tenant_id: str,
        user_id: str,
        run_id: str,
        step_id: str,
        invocation_fingerprint: str,
        expected_version: int,
        status: AnalysisStepLedgerStatus,
        result_id: str | None,
        evidence_ids: tuple[str, ...],
    ) -> AnalysisStepLedgerEntry:
        await self.initialize()
        async with await psycopg.AsyncConnection.connect(self._dsn) as connection:
            row = await self._select_entry(
                connection, run_id=run_id, step_id=step_id, for_update=True
            )
            if row is None:
                raise ResourceNotFound("analysis step ledger entry not found")
            existing = _entry_from_row(row)
            if status == "persisted" and result_id is not None and evidence_ids:
                if self._observation_validator is None:
                    raise AnalysisStepLedgerConflict(
                        "STEP_OBSERVATION_UNVERIFIED",
                        "persisted step references require a trusted observation validator",
                    )
                await self._observation_validator.validate(
                    tenant_id=tenant_id,
                    user_id=user_id,
                    run_id=run_id,
                    tool_call_id=existing.tool_call_id,
                    result_id=result_id,
                    evidence_ids=evidence_ids,
                )
            updated = _validated_transition(
                existing,
                tenant_id=tenant_id,
                user_id=user_id,
                invocation_fingerprint=invocation_fingerprint,
                expected_version=expected_version,
                status=status,
                result_id=result_id,
                evidence_ids=evidence_ids,
            )
            if updated is existing:
                return existing
            changed = await (
                await connection.execute(
                    f'UPDATE "{self._schema}".analysis_step_ledger SET '
                    "status = %s, result_id = %s, evidence_ids = %s, version = %s "
                    "WHERE run_id = %s AND step_id = %s AND version = %s "
                    "RETURNING tenant_id, user_id, run_id, plan_id, request_id, "
                    "step_id, tool_call_id, invocation_fingerprint, status, "
                    "result_id, evidence_ids, version",
                    (
                        updated.status,
                        updated.result_id,
                        _evidence_json(updated.evidence_ids),
                        updated.version,
                        run_id,
                        step_id,
                        expected_version,
                    ),
                )
            ).fetchone()
            if changed is None:
                raise RunStateConflict("analysis step ledger version changed")
            return _entry_from_row(changed)

    async def mark_indeterminate_if_executing(
        self,
        *,
        tenant_id: str,
        user_id: str,
        run_id: str,
        step_id: str,
        invocation_fingerprint: str,
        expected_version: int,
    ) -> AnalysisStepLedgerEntry:
        return await self.transition_step(
            tenant_id=tenant_id,
            user_id=user_id,
            run_id=run_id,
            step_id=step_id,
            invocation_fingerprint=invocation_fingerprint,
            expected_version=expected_version,
            status="indeterminate",
            result_id=None,
            evidence_ids=(),
        )

    async def _select_entry(
        self, connection, *, run_id: str, step_id: str, for_update: bool = False
    ):
        suffix = " FOR UPDATE" if for_update else ""
        return await (
            await connection.execute(
                f'SELECT tenant_id, user_id, run_id, plan_id, request_id, step_id, '
                "tool_call_id, invocation_fingerprint, status, result_id, evidence_ids, "
                f'version FROM "{self._schema}".analysis_step_ledger '
                f"WHERE run_id = %s AND step_id = %s{suffix}",
                (run_id, step_id),
            )
        ).fetchone()

    def _ddl_statements(self) -> tuple[str, ...]:
        prefix = f'"{self._schema}".'
        return (
            f"CREATE TABLE IF NOT EXISTS {prefix}schema_version ("
            "version INTEGER PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())",
            f"CREATE TABLE IF NOT EXISTS {prefix}analysis_step_ledger ("
            "tenant_id TEXT NOT NULL, user_id TEXT NOT NULL, run_id TEXT NOT NULL, "
            "plan_id TEXT NOT NULL, request_id TEXT NOT NULL, step_id TEXT NOT NULL, "
            "tool_call_id TEXT NOT NULL, invocation_fingerprint TEXT NOT NULL, "
            "status TEXT NOT NULL CHECK (status IN ('reserved', 'executing', "
            "'persisted', 'indeterminate', 'failed')), result_id TEXT, "
            "evidence_ids TEXT NOT NULL, version BIGINT NOT NULL CHECK (version > 0), "
            "PRIMARY KEY (run_id, step_id), CHECK ((status = 'persisted' AND "
            "result_id IS NOT NULL AND evidence_ids <> '[]') OR (status <> 'persisted' "
            "AND result_id IS NULL AND evidence_ids = '[]')))",
            f"CREATE INDEX IF NOT EXISTS idx_fva_analysis_step_owner "
            f"ON {prefix}analysis_step_ledger(tenant_id, user_id, run_id)",
            f"CREATE INDEX IF NOT EXISTS idx_fva_analysis_step_call "
            f"ON {prefix}analysis_step_ledger(tool_call_id)",
            f"INSERT INTO {prefix}schema_version (version) VALUES (5) "
            "ON CONFLICT DO NOTHING",
        )


def _reserved_entry(**values: str) -> AnalysisStepLedgerEntry:
    expected_call_id = analysis_step_tool_call_id(
        tenant_id=values["tenant_id"],
        run_id=values["run_id"],
        plan_id=values["plan_id"],
        step_id=values["step_id"],
    )
    if values["tool_call_id"] != expected_call_id:
        raise AnalysisStepLedgerConflict(
            "STEP_TOOL_CALL_ID_MISMATCH",
            "tool call id is not derived from the plan and step identity",
        )
    try:
        return AnalysisStepLedgerEntry(**values)
    except (TypeError, ValueError, ValidationError) as exc:
        raise AnalysisStepLedgerConflict(
            "STEP_STATE_INVALID", "step reservation failed contract validation"
        ) from exc


def _validated_transition(
    existing: AnalysisStepLedgerEntry,
    *,
    tenant_id: str,
    user_id: str,
    invocation_fingerprint: str,
    expected_version: int,
    status: AnalysisStepLedgerStatus,
    result_id: str | None,
    evidence_ids: tuple[str, ...],
) -> AnalysisStepLedgerEntry:
    _require_owner(existing, tenant_id=tenant_id, user_id=user_id)
    _require_invocation(existing, invocation_fingerprint)
    if (
        existing.status == status
        and existing.result_id == result_id
        and existing.evidence_ids == evidence_ids
    ):
        return existing
    if existing.version != expected_version:
        raise RunStateConflict("analysis step ledger version changed")
    if status not in _TRANSITIONS[existing.status]:
        raise AnalysisStepLedgerConflict(
            "STEP_TRANSITION_INVALID",
            f"step cannot transition from {existing.status} to {status}",
        )
    try:
        return AnalysisStepLedgerEntry.model_validate(
            {
                **existing.model_dump(mode="python"),
                "status": status,
                "result_id": result_id,
                "evidence_ids": evidence_ids,
                "version": existing.version + 1,
            }
        )
    except (TypeError, ValueError, ValidationError) as exc:
        raise AnalysisStepLedgerConflict(
            "STEP_STATE_INVALID", "step state and result references are inconsistent"
        ) from exc


def _entry_values(entry: AnalysisStepLedgerEntry) -> tuple[object, ...]:
    return (
        entry.tenant_id,
        entry.user_id,
        entry.run_id,
        entry.plan_id,
        entry.request_id,
        entry.step_id,
        entry.tool_call_id,
        entry.invocation_fingerprint,
        entry.status,
        entry.result_id,
        _evidence_json(entry.evidence_ids),
        entry.version,
    )


def _entry_from_row(row: tuple[object, ...]) -> AnalysisStepLedgerEntry:
    try:
        raw_evidence = json.loads(str(row[10]))
        if not isinstance(raw_evidence, list) or not all(
            isinstance(item, str) for item in raw_evidence
        ):
            raise ValueError("evidence ids are not a string list")
        return AnalysisStepLedgerEntry(
            tenant_id=str(row[0]),
            user_id=str(row[1]),
            run_id=str(row[2]),
            plan_id=str(row[3]),
            request_id=str(row[4]),
            step_id=str(row[5]),
            tool_call_id=str(row[6]),
            invocation_fingerprint=str(row[7]),
            status=str(row[8]),  # type: ignore[arg-type]
            result_id=None if row[9] is None else str(row[9]),
            evidence_ids=tuple(raw_evidence),
            version=int(str(row[11])),
        )
    except (TypeError, ValueError, json.JSONDecodeError, ValidationError) as exc:
        raise AnalysisStepLedgerConflict(
            "STEP_STORE_INVALID", "stored step failed contract validation"
        ) from exc


def _evidence_json(evidence_ids: tuple[str, ...]) -> str:
    return json.dumps(evidence_ids, ensure_ascii=True, separators=(",", ":"))


def _require_owner(
    entry: AnalysisStepLedgerEntry, *, tenant_id: str, user_id: str
) -> None:
    if entry.tenant_id != tenant_id or entry.user_id != user_id:
        raise ResourceNotFound("analysis step ledger entry not found")


def _require_invocation(entry: AnalysisStepLedgerEntry, expected: str) -> None:
    if entry.invocation_fingerprint != expected:
        raise AnalysisStepLedgerConflict(
            "STEP_INVOCATION_MISMATCH",
            "analysis invocation fingerprint does not match the step ledger",
        )


def _same_invocation(
    left: AnalysisStepLedgerEntry, right: AnalysisStepLedgerEntry
) -> bool:
    return (
        left.tenant_id,
        left.user_id,
        left.run_id,
        left.plan_id,
        left.request_id,
        left.step_id,
        left.tool_call_id,
        left.invocation_fingerprint,
    ) == (
        right.tenant_id,
        right.user_id,
        right.run_id,
        right.plan_id,
        right.request_id,
        right.step_id,
        right.tool_call_id,
        right.invocation_fingerprint,
    )
