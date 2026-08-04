# pyright: reportArgumentType=false, reportCallIssue=false

"""In-memory and PostgreSQL authorities for AnalysisIntent handoffs."""

from __future__ import annotations

import asyncio
import json
import re
from datetime import UTC, datetime

import psycopg
from pydantic import TypeAdapter, ValidationError

from full_view_agent.application.analysis_intent_handoff import (
    AnalysisIntentHandoff,
    AnalysisIntentHandoffConflict,
    AnalysisIntentHandoffStatus,
    HandoffClarificationOption,
    validate_handoff_transition,
)
from full_view_agent.application.errors import ResourceNotFound, RunStateConflict
from full_view_agent.application.fingerprints import canonical_fingerprint
from full_view_agent.application.session_run_service import new_id
from full_view_agent.domain.analysis_intent import AnalysisIntentV1

_SAFE_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
_OPTIONS_ADAPTER = TypeAdapter(tuple[HandoffClarificationOption, ...])


class InMemoryAnalysisIntentHandoffStore:
    def __init__(self) -> None:
        self._handoffs: dict[str, AnalysisIntentHandoff] = {}
        self._lock = asyncio.Lock()

    async def capture(
        self,
        *,
        tenant_id: str,
        user_id: str,
        session_id: str,
        run_id: str,
        intent: AnalysisIntentV1,
    ) -> AnalysisIntentHandoff:
        requested = _new_handoff(
            tenant_id=tenant_id,
            user_id=user_id,
            session_id=session_id,
            run_id=run_id,
            intent=intent,
        )
        async with self._lock:
            existing = self._handoffs.get(run_id)
            if existing is None:
                self._handoffs[run_id] = requested
                return requested
            _require_owner(existing, tenant_id=tenant_id, user_id=user_id)
            _require_same_capture(existing, requested)
            return existing

    async def get_for_run(
        self, *, tenant_id: str, user_id: str, run_id: str
    ) -> AnalysisIntentHandoff:
        async with self._lock:
            existing = self._handoffs.get(run_id)
            if existing is None:
                raise ResourceNotFound("analysis intent handoff not found")
            _require_owner(existing, tenant_id=tenant_id, user_id=user_id)
            return existing

    async def advance(
        self,
        *,
        tenant_id: str,
        user_id: str,
        run_id: str,
        expected_version: int,
        status: AnalysisIntentHandoffStatus,
        clarification_options: tuple[HandoffClarificationOption, ...] = (),
        selected_area_code: str | None = None,
        plan_id: str | None = None,
        request_id: str | None = None,
        failure_code: str | None = None,
        report_result_id: str | None = None,
    ) -> AnalysisIntentHandoff:
        async with self._lock:
            existing = self._handoffs.get(run_id)
            if existing is None:
                raise ResourceNotFound("analysis intent handoff not found")
            _require_owner(existing, tenant_id=tenant_id, user_id=user_id)
            requested = _advanced_handoff(
                existing,
                status=status,
                clarification_options=clarification_options,
                selected_area_code=selected_area_code,
                plan_id=plan_id,
                request_id=request_id,
                failure_code=failure_code,
                report_result_id=report_result_id,
            )
            if _same_mutable_state(existing, requested):
                return existing
            if existing.version != expected_version:
                raise RunStateConflict("analysis handoff version changed")
            validate_handoff_transition(existing, requested)
            self._handoffs[run_id] = requested
            return requested


class PostgresAnalysisIntentHandoffStore:
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
                await connection.execute(f'CREATE SCHEMA IF NOT EXISTS "{self._schema}"')
                for statement in self._ddl_statements():
                    await connection.execute(statement)
            self._initialized = True

    async def drop_schema(self) -> None:
        async with await psycopg.AsyncConnection.connect(self._dsn) as connection:
            await connection.execute(f'DROP SCHEMA IF EXISTS "{self._schema}" CASCADE')
        self._initialized = False

    async def capture(
        self,
        *,
        tenant_id: str,
        user_id: str,
        session_id: str,
        run_id: str,
        intent: AnalysisIntentV1,
    ) -> AnalysisIntentHandoff:
        requested = _new_handoff(
            tenant_id=tenant_id,
            user_id=user_id,
            session_id=session_id,
            run_id=run_id,
            intent=intent,
        )
        await self.initialize()
        async with await psycopg.AsyncConnection.connect(self._dsn) as connection:
            await connection.execute(
                f'INSERT INTO "{self._schema}".analysis_intent_handoffs '
                "(handoff_id, tenant_id, user_id, session_id, run_id, intent_json, "
                "intent_fingerprint, status, clarification_json, selected_area_code, "
                "plan_id, request_id, failure_code, report_result_id, version, "
                "created_at, updated_at) VALUES ("
                + ", ".join(["%s"] * 17)
                + ") ON CONFLICT (run_id) DO NOTHING",
                _handoff_values(requested),
            )
            row = await self._select_by_run(connection, run_id)
        if row is None:
            raise AnalysisIntentHandoffConflict(
                "HANDOFF_STORE_INVALID", "captured handoff was not readable"
            )
        existing = _handoff_from_row(row)
        _require_owner(existing, tenant_id=tenant_id, user_id=user_id)
        _require_same_capture(existing, requested)
        return existing

    async def get_for_run(
        self, *, tenant_id: str, user_id: str, run_id: str
    ) -> AnalysisIntentHandoff:
        await self.initialize()
        async with await psycopg.AsyncConnection.connect(self._dsn) as connection:
            row = await self._select_by_run(connection, run_id)
        if row is None:
            raise ResourceNotFound("analysis intent handoff not found")
        existing = _handoff_from_row(row)
        _require_owner(existing, tenant_id=tenant_id, user_id=user_id)
        return existing

    async def advance(
        self,
        *,
        tenant_id: str,
        user_id: str,
        run_id: str,
        expected_version: int,
        status: AnalysisIntentHandoffStatus,
        clarification_options: tuple[HandoffClarificationOption, ...] = (),
        selected_area_code: str | None = None,
        plan_id: str | None = None,
        request_id: str | None = None,
        failure_code: str | None = None,
        report_result_id: str | None = None,
    ) -> AnalysisIntentHandoff:
        await self.initialize()
        async with await psycopg.AsyncConnection.connect(self._dsn) as connection:
            row = await self._select_by_run(connection, run_id)
            if row is None:
                raise ResourceNotFound("analysis intent handoff not found")
            existing = _handoff_from_row(row)
            _require_owner(existing, tenant_id=tenant_id, user_id=user_id)
            requested = _advanced_handoff(
                existing,
                status=status,
                clarification_options=clarification_options,
                selected_area_code=selected_area_code,
                plan_id=plan_id,
                request_id=request_id,
                failure_code=failure_code,
                report_result_id=report_result_id,
            )
            if _same_mutable_state(existing, requested):
                return existing
            if existing.version != expected_version:
                raise RunStateConflict("analysis handoff version changed")
            validate_handoff_transition(existing, requested)
            updated_row = await (
                await connection.execute(
                    f'UPDATE "{self._schema}".analysis_intent_handoffs SET '
                    "status=%s, clarification_json=%s, selected_area_code=%s, "
                    "plan_id=%s, request_id=%s, failure_code=%s, report_result_id=%s, "
                    "version=version+1, updated_at=%s WHERE run_id=%s AND tenant_id=%s "
                    "AND user_id=%s AND version=%s RETURNING "
                    + _SELECT_COLUMNS,
                    (
                        requested.status,
                        _options_json(requested.clarification_options),
                        requested.selected_area_code,
                        requested.plan_id,
                        requested.request_id,
                        requested.failure_code,
                        requested.report_result_id,
                        requested.updated_at,
                        run_id,
                        tenant_id,
                        user_id,
                        expected_version,
                    ),
                )
            ).fetchone()
            if updated_row is not None:
                return _handoff_from_row(updated_row)
            current_row = await self._select_by_run(connection, run_id)
        if current_row is None:
            raise ResourceNotFound("analysis intent handoff not found")
        current = _handoff_from_row(current_row)
        _require_owner(current, tenant_id=tenant_id, user_id=user_id)
        if _same_mutable_state(current, requested):
            return current
        raise RunStateConflict("analysis handoff version changed")

    async def _select_by_run(self, connection, run_id: str):
        return await (
            await connection.execute(
                f'SELECT {_SELECT_COLUMNS} FROM "{self._schema}".'
                "analysis_intent_handoffs WHERE run_id=%s",
                (run_id,),
            )
        ).fetchone()

    def _ddl_statements(self) -> tuple[str, ...]:
        prefix = f'"{self._schema}".'
        statuses = ", ".join(
            f"'{item}'"
            for item in (
                "captured",
                "waiting_clarification",
                "waiting_reauth",
                "compiling",
                "compiled",
                "executing",
                "completed",
                "partial",
                "failed",
                "denied",
                "cancelled",
            )
        )
        return (
            f"CREATE TABLE IF NOT EXISTS {prefix}analysis_intent_handoffs ("
            "handoff_id TEXT NOT NULL, tenant_id TEXT NOT NULL, user_id TEXT NOT NULL, "
            "session_id TEXT NOT NULL, run_id TEXT PRIMARY KEY, intent_json TEXT NOT NULL, "
            "intent_fingerprint TEXT NOT NULL, status TEXT NOT NULL CHECK (status IN ("
            f"{statuses})), clarification_json TEXT NOT NULL, selected_area_code TEXT, "
            "plan_id TEXT, request_id TEXT, failure_code TEXT, report_result_id TEXT, "
            "version BIGINT NOT NULL CHECK (version > 0), "
            "created_at TIMESTAMP WITH TIME ZONE NOT NULL, "
            "updated_at TIMESTAMP WITH TIME ZONE NOT NULL, "
            "UNIQUE (handoff_id), CHECK ((plan_id IS NULL) = (request_id IS NULL)))",
            f"CREATE INDEX IF NOT EXISTS idx_fva_intent_handoffs_owner "
            f"ON {prefix}analysis_intent_handoffs(tenant_id, user_id, session_id)",
            f"CREATE INDEX IF NOT EXISTS idx_fva_intent_handoffs_status "
            f"ON {prefix}analysis_intent_handoffs(status, updated_at)",
        )


_SELECT_COLUMNS = (
    "handoff_id, tenant_id, user_id, session_id, run_id, intent_json, "
    "intent_fingerprint, status, clarification_json, selected_area_code, plan_id, "
    "request_id, failure_code, report_result_id, version, created_at, updated_at"
)


def _new_handoff(
    *, tenant_id: str, user_id: str, session_id: str, run_id: str, intent: AnalysisIntentV1
) -> AnalysisIntentHandoff:
    return AnalysisIntentHandoff(
        handoff_id=new_id("ahf"),
        tenant_id=tenant_id,
        user_id=user_id,
        session_id=session_id,
        run_id=run_id,
        intent=intent,
        intent_fingerprint=canonical_fingerprint(
            domain="analysis-intent-handoff:1.0", value=intent
        ),
    )


def _advanced_handoff(
    current: AnalysisIntentHandoff,
    *,
    status: AnalysisIntentHandoffStatus,
    clarification_options: tuple[HandoffClarificationOption, ...],
    selected_area_code: str | None,
    plan_id: str | None,
    request_id: str | None,
    failure_code: str | None,
    report_result_id: str | None,
) -> AnalysisIntentHandoff:
    try:
        return AnalysisIntentHandoff.model_validate(
            {
                **current.model_dump(mode="python"),
                "status": status,
                "clarification_options": clarification_options,
                "selected_area_code": selected_area_code,
                "plan_id": plan_id,
                "request_id": request_id,
                "failure_code": failure_code,
                "report_result_id": report_result_id,
                "version": current.version + 1,
                "updated_at": datetime.now(UTC),
            }
        )
    except ValueError as exc:
        raise RunStateConflict("analysis handoff state is inconsistent") from exc


def _require_owner(
    handoff: AnalysisIntentHandoff, *, tenant_id: str, user_id: str
) -> None:
    if handoff.tenant_id != tenant_id or handoff.user_id != user_id:
        raise ResourceNotFound("analysis intent handoff not found")


def _require_same_capture(
    existing: AnalysisIntentHandoff, requested: AnalysisIntentHandoff
) -> None:
    if (
        existing.session_id != requested.session_id
        or existing.intent_fingerprint != requested.intent_fingerprint
        or existing.intent != requested.intent
    ):
        raise AnalysisIntentHandoffConflict(
            "HANDOFF_CONFLICT", "run is already owned by a different analysis intent"
        )


def _same_mutable_state(
    left: AnalysisIntentHandoff, right: AnalysisIntentHandoff
) -> bool:
    fields = (
        "status",
        "clarification_options",
        "selected_area_code",
        "plan_id",
        "request_id",
        "failure_code",
        "report_result_id",
    )
    return all(getattr(left, field) == getattr(right, field) for field in fields)


def _options_json(options: tuple[HandoffClarificationOption, ...]) -> str:
    return json.dumps(
        [option.model_dump(mode="json") for option in options],
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _handoff_values(handoff: AnalysisIntentHandoff) -> tuple[object, ...]:
    return (
        handoff.handoff_id,
        handoff.tenant_id,
        handoff.user_id,
        handoff.session_id,
        handoff.run_id,
        handoff.intent.model_dump_json(),
        handoff.intent_fingerprint,
        handoff.status,
        _options_json(handoff.clarification_options),
        handoff.selected_area_code,
        handoff.plan_id,
        handoff.request_id,
        handoff.failure_code,
        handoff.report_result_id,
        handoff.version,
        handoff.created_at,
        handoff.updated_at,
    )


def _handoff_from_row(row: tuple[object, ...]) -> AnalysisIntentHandoff:
    try:
        return AnalysisIntentHandoff(
            handoff_id=str(row[0]),
            tenant_id=str(row[1]),
            user_id=str(row[2]),
            session_id=str(row[3]),
            run_id=str(row[4]),
            intent=AnalysisIntentV1.model_validate_json(str(row[5])),
            intent_fingerprint=str(row[6]),
            status=str(row[7]),  # type: ignore[arg-type]
            clarification_options=_OPTIONS_ADAPTER.validate_json(str(row[8])),
            selected_area_code=None if row[9] is None else str(row[9]),
            plan_id=None if row[10] is None else str(row[10]),
            request_id=None if row[11] is None else str(row[11]),
            failure_code=None if row[12] is None else str(row[12]),
            report_result_id=None if row[13] is None else str(row[13]),
            version=int(str(row[14])),
            created_at=row[15],  # type: ignore[arg-type]
            updated_at=row[16],  # type: ignore[arg-type]
        )
    except (TypeError, ValueError, ValidationError) as exc:
        raise AnalysisIntentHandoffConflict(
            "HANDOFF_STORE_INVALID", "stored handoff failed contract validation"
        ) from exc
