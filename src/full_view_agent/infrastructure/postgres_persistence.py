# pyright: reportArgumentType=false, reportCallIssue=false

import asyncio
import json
import logging
import os
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol, TypeVar

import psycopg
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pydantic import SecretStr, TypeAdapter

from full_view_agent.application.analysis_plan_repository import validate_plan_for_save
from full_view_agent.application.errors import (
    CommandClientMismatch,
    CredentialUnavailable,
    EventHistoryExpired,
    IdempotencyConflict,
    InputRequestClosed,
    ResourceNotFound,
    RunStateConflict,
    SessionActiveRunConflict,
)
from full_view_agent.application.fingerprints import canonical_fingerprint
from full_view_agent.application.frontend_commands import merge_receipt
from full_view_agent.application.session_run_service import new_id
from full_view_agent.domain.analysis_plan import AnalysisPlan
from full_view_agent.domain.models import (
    AgentEvent,
    AgentMessage,
    AgentRun,
    AgentSession,
    AuthContext,
    CredentialGrant,
    DataResult,
    Evidence,
    FrontendCommand,
    FrontendCommandReceipt,
    PendingInputRequest,
    Steer,
)

T = TypeVar("T")
_DATA_RESULT_ADAPTER = TypeAdapter(DataResult)
_SAFE_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
logger = logging.getLogger(__name__)


def _same_observation_result(left: DataResult, right: DataResult) -> bool:
    return left.model_dump(exclude={"created_at", "payload_expires_at"}) == right.model_dump(
        exclude={"created_at", "payload_expires_at"}
    )


def _same_observation_evidence(left: Evidence, right: Evidence) -> bool:
    return left.model_dump(exclude={"retrieved_at"}) == right.model_dump(
        exclude={"retrieved_at"}
    )


def _same_observation_command(
    left: FrontendCommand, right: FrontendCommand
) -> bool:
    excluded = {"issued_at", "expires_at"}
    return left.model_dump(exclude=excluded) == right.model_dump(exclude=excluded)


def _same_result_identity(left: DataResult, right: DataResult) -> bool:
    if left == right:
        return True
    if left.kind != "analysis_report" or right.kind != "analysis_report":
        return False
    excluded = {"created_at", "payload_expires_at"}
    return left.model_dump(exclude=excluded) == right.model_dump(exclude=excluded)


class EventNotifier(Protocol):
    async def publish(self, *, run_id: str, event_id: str) -> None: ...

    async def wait(self, *, run_id: str, timeout_seconds: float) -> str | None: ...


class PostgresAgentPersistence:
    """PostgreSQL authority store for P0 runtime state.

    JSON payloads deliberately use portable TEXT columns. This keeps the domain
    contract independent from PostgreSQL JSONB extensions and leaves a practical
    migration path to Kingbase after implementation stabilises.
    """

    def __init__(
        self,
        *,
        dsn: str,
        schema: str = "full_view_agent",
        credential_encryption_key: bytes | None = None,
        credential_ttl_seconds: int = 300,
        event_retention_seconds: int = 3600,
        event_poll_interval_seconds: float = 0.05,
        event_notifier: EventNotifier | None = None,
    ) -> None:
        if not _SAFE_IDENTIFIER.fullmatch(schema):
            raise ValueError("PostgreSQL schema must be a safe lower-case identifier")
        if credential_encryption_key is not None and len(credential_encryption_key) not in {
            16,
            24,
            32,
        }:
            raise ValueError("credential encryption key must be 16, 24, or 32 bytes")
        self._dsn = dsn
        self._schema = schema
        self._cipher = (
            AESGCM(credential_encryption_key)
            if credential_encryption_key is not None
            else None
        )
        self._credential_ttl = timedelta(seconds=credential_ttl_seconds)
        self._event_retention = timedelta(seconds=event_retention_seconds)
        self._event_poll_interval = event_poll_interval_seconds
        self._event_notifier = event_notifier
        self._init_lock = asyncio.Lock()
        self._initialized = False

    @property
    def schema(self) -> str:
        return self._schema

    async def initialize(self) -> None:
        await self._ensure_initialized()

    async def drop_schema(self) -> None:
        async with await psycopg.AsyncConnection.connect(self._dsn) as connection:
            await connection.execute(f'DROP SCHEMA IF EXISTS "{self._schema}" CASCADE')
        self._initialized = False

    async def create_session(self, session: AgentSession) -> AgentSession:
        await self._ensure_initialized()
        async with await psycopg.AsyncConnection.connect(self._dsn) as connection:
            await connection.execute(
                f'INSERT INTO "{self._schema}".sessions '
                "(session_id, owner_tenant_id, owner_user_id, app_id, data_json) "
                "VALUES (%s, %s, %s, %s, %s)",
                (
                    session.session_id,
                    session.owner_tenant_id,
                    session.owner_user_id,
                    session.app_id,
                    _session_json(session),
                ),
            )
        return session

    async def get_session(
        self,
        *,
        tenant_id: str = "legacy",
        app_id: str = "full_information_view",
        user_id: str,
        session_id: str,
    ) -> AgentSession:
        await self._ensure_initialized()
        async with await psycopg.AsyncConnection.connect(self._dsn) as connection:
            row = await (
                await connection.execute(
                    f'SELECT data_json FROM "{self._schema}".sessions '
                    "WHERE session_id = %s AND owner_tenant_id = %s "
                    "AND owner_user_id = %s AND app_id = %s",
                    (session_id, tenant_id, user_id, app_id),
                )
            ).fetchone()
        if row is None:
            raise ResourceNotFound("session not found")
        return AgentSession.model_validate_json(row[0])

    async def list_sessions(
        self,
        *,
        tenant_id: str = "legacy",
        app_id: str = "full_information_view",
        user_id: str,
        status: Literal["active", "archived"] | None = None,
    ) -> list[AgentSession]:
        await self._ensure_initialized()
        async with await psycopg.AsyncConnection.connect(self._dsn) as connection:
            rows = await (
                await connection.execute(
                    f'SELECT data_json FROM "{self._schema}".sessions '
                    "WHERE owner_tenant_id = %s AND owner_user_id = %s AND app_id = %s",
                    (tenant_id, user_id, app_id),
                )
            ).fetchall()
        sessions = [AgentSession.model_validate_json(row[0]) for row in rows]
        return sorted(
            (
                session
                for session in sessions
                if status is None or session.status == status
            ),
            key=lambda session: (session.updated_at, session.session_id),
            reverse=True,
        )

    async def update_session(
        self,
        *,
        tenant_id: str = "legacy",
        app_id: str = "full_information_view",
        user_id: str,
        session_id: str,
        title: str | None = None,
        status: Literal["archived"] | None = None,
    ) -> AgentSession:
        await self._ensure_initialized()
        async with await psycopg.AsyncConnection.connect(self._dsn) as connection:
            row = await (
                await connection.execute(
                    f'SELECT data_json FROM "{self._schema}".sessions '
                    "WHERE session_id = %s AND owner_tenant_id = %s "
                    "AND owner_user_id = %s AND app_id = %s FOR UPDATE",
                    (session_id, tenant_id, user_id, app_id),
                )
            ).fetchone()
            if row is None:
                raise ResourceNotFound("session not found")
            session = AgentSession.model_validate_json(row[0])
            if status == "archived" and session.active_run_id is not None:
                raise SessionActiveRunConflict(session.active_run_id)
            changes: dict[str, object] = {}
            if title is not None and title != session.title:
                changes["title"] = title
            if status is not None and status != session.status:
                changes["status"] = status
            if not changes:
                return session
            changes.update(
                updated_at=datetime.now(UTC),
                version=session.version + 1,
            )
            updated = session.model_copy(update=changes)
            await connection.execute(
                f'UPDATE "{self._schema}".sessions SET data_json = %s '
                "WHERE session_id = %s AND owner_tenant_id = %s "
                "AND owner_user_id = %s AND app_id = %s",
                (_session_json(updated), session_id, tenant_id, user_id, app_id),
            )
        return updated

    async def create_run_if_session_idle(
        self,
        *,
        user_id: str,
        session_id: str,
        run: AgentRun,
        input_message: AgentMessage,
    ) -> AgentRun:
        await self._ensure_initialized()
        async with await psycopg.AsyncConnection.connect(self._dsn) as connection:
            row = await (
                await connection.execute(
                    f'SELECT data_json FROM "{self._schema}".sessions '
                    "WHERE session_id = %s FOR UPDATE",
                    (session_id,),
                )
            ).fetchone()
            if row is None:
                raise ResourceNotFound("session not found")
            session = AgentSession.model_validate_json(row[0])
            if session.owner_user_id != user_id:
                raise ResourceNotFound("session not found")
            if session.status == "archived":
                raise RunStateConflict("archived session cannot create runs")
            if session.active_run_id is not None:
                raise SessionActiveRunConflict(session.active_run_id)
            now = datetime.now(UTC)
            updated_session = session.model_copy(
                update={
                    "active_run_id": run.run_id,
                    "updated_at": now,
                    "version": session.version + 1,
                }
            )
            await connection.execute(
                f'INSERT INTO "{self._schema}".runs '
                "(run_id, session_id, data_json) VALUES (%s, %s, %s)",
                (run.run_id, run.session_id, run.model_dump_json()),
            )
            await connection.execute(
                f'INSERT INTO "{self._schema}".messages '
                "(message_id, session_id, run_id, created_at, data_json) "
                "VALUES (%s, %s, %s, %s, %s)",
                (
                    input_message.message_id,
                    input_message.session_id,
                    input_message.run_id,
                    input_message.created_at,
                    input_message.model_dump_json(),
                ),
            )
            await connection.execute(
                f'UPDATE "{self._schema}".sessions SET data_json = %s '
                "WHERE session_id = %s",
                (_session_json(updated_session), session_id),
            )
        return run

    async def list_messages(
        self,
        *,
        user_id: str,
        session_id: str,
        tenant_id: str | None = None,
        app_id: str | None = None,
    ) -> list[AgentMessage]:
        await self._ensure_initialized()
        async with await psycopg.AsyncConnection.connect(self._dsn) as connection:
            session_row = await (
                await connection.execute(
                    f'SELECT data_json FROM "{self._schema}".sessions '
                    "WHERE session_id = %s",
                    (session_id,),
                )
            ).fetchone()
            session = (
                AgentSession.model_validate_json(session_row[0])
                if session_row is not None
                else None
            )
            if not _owns_resource(
                session, user_id=user_id, tenant_id=tenant_id, app_id=app_id
            ):
                raise ResourceNotFound("session not found")
            rows = await (
                await connection.execute(
                    f'SELECT data_json FROM "{self._schema}".messages '
                    "WHERE session_id = %s ORDER BY created_at, message_id",
                    (session_id,),
                )
            ).fetchall()
        return [AgentMessage.model_validate_json(row[0]) for row in rows]

    async def save_message(
        self, *, user_id: str, run_id: str, message: AgentMessage
    ) -> AgentMessage:
        async with await self._owned_run_connection(user_id, run_id) as owned:
            connection, run, session = owned
            existing_row = await (
                await connection.execute(
                    f'SELECT data_json FROM "{self._schema}".messages '
                    "WHERE message_id = %s",
                    (message.message_id,),
                )
            ).fetchone()
            if existing_row is not None:
                existing = AgentMessage.model_validate_json(existing_row[0])
                if existing != message:
                    raise RunStateConflict(
                        "message identity is already bound differently"
                    )
                return existing
            if (
                run.status != "running"
                or session.active_run_id != run_id
                or message.run_id != run_id
                or message.session_id != run.session_id
            ):
                raise RunStateConflict("messages can only be saved for an active run")
            await connection.execute(
                f'INSERT INTO "{self._schema}".messages '
                "(message_id, session_id, run_id, created_at, data_json) "
                "VALUES (%s, %s, %s, %s, %s)",
                (
                    message.message_id,
                    message.session_id,
                    message.run_id,
                    message.created_at,
                    message.model_dump_json(),
                ),
            )
        return message

    async def start_run(self, *, user_id: str, run_id: str) -> AgentRun:
        async with await self._owned_run_connection(user_id, run_id) as owned:
            connection, run, _session = owned
            if run.status != "queued":
                raise RunStateConflict("only a queued run can be started")
            running = run.model_copy(
                update={
                    "status": "running",
                    "current_phase": "planning",
                    "started_at": datetime.now(UTC),
                    "state_version": run.state_version + 1,
                }
            )
            await self._update_run(connection, running)
        return running

    async def get_run(
        self,
        *,
        user_id: str,
        run_id: str,
        tenant_id: str | None = None,
        app_id: str | None = None,
    ) -> AgentRun:
        async with await self._owned_run_connection(
            user_id, run_id, for_update=False
        ) as owned:
            _connection, run, session = owned
            if not _owns_resource(
                session, user_id=user_id, tenant_id=tenant_id, app_id=app_id
            ):
                raise ResourceNotFound("run not found")
            return run

    async def list_recoverable_runs(self) -> list[tuple[str, AgentRun]]:
        await self._ensure_initialized()
        async with await psycopg.AsyncConnection.connect(self._dsn) as connection:
            rows = await (
                await connection.execute(
                    f'SELECT r.data_json, s.data_json FROM "{self._schema}".runs r '
                    f'JOIN "{self._schema}".sessions s ON s.session_id = r.session_id'
                )
            ).fetchall()
        recoverable: list[tuple[str, AgentRun]] = []
        for run_json, session_json in rows:
            run = AgentRun.model_validate_json(run_json)
            session = AgentSession.model_validate_json(session_json)
            if run.status in {"queued", "running", "waiting_input"} and (
                session.active_run_id == run.run_id
            ):
                recoverable.append((session.owner_user_id, run))
        return recoverable

    async def save_result(
        self, *, user_id: str, run_id: str, result: DataResult
    ) -> DataResult:
        async with await self._owned_run_connection(user_id, run_id) as owned:
            connection, run, session = owned
            if run.status != "running" or session.active_run_id != run_id:
                raise RunStateConflict("results can only be saved for an active run")
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (f"result:{result.result_id}",),
            )
            existing_row = await (
                await connection.execute(
                    f'SELECT run_id, data_json FROM "{self._schema}".results '
                    "WHERE result_id = %s FOR UPDATE",
                    (result.result_id,),
                )
            ).fetchone()
            if existing_row is not None:
                existing = _DATA_RESULT_ADAPTER.validate_json(existing_row[1])
                if existing_row[0] != run_id or not _same_result_identity(
                    existing, result
                ):
                    raise RunStateConflict(
                        "result identity is already bound differently"
                    )
                return existing
            await connection.execute(
                f'INSERT INTO "{self._schema}".results '
                "(result_id, run_id, data_json) VALUES (%s, %s, %s)",
                (result.result_id, run_id, result.model_dump_json()),
            )
        return result

    async def save_tool_observation(
        self,
        *,
        user_id: str,
        run_id: str,
        result: DataResult,
        evidence: Evidence,
        commands: tuple[FrontendCommand, ...],
    ) -> tuple[DataResult, Evidence, tuple[FrontendCommand, ...]]:
        """Persist Result, Evidence and UI commands in one DB transaction."""

        async with await self._owned_run_connection(user_id, run_id) as owned:
            connection, run, session = owned
            if run.status != "running" or session.active_run_id != run_id:
                raise RunStateConflict("tool observations require an active run")
            if evidence.result_id != result.result_id:
                raise RunStateConflict("evidence result does not match observation result")
            if result.evidence_ids != [evidence.evidence_id]:
                raise RunStateConflict("result evidence reference is inconsistent")
            for command in commands:
                if command.run_id != run_id:
                    raise RunStateConflict("frontend command run does not match observation")
                if command.target_client_instance_id != run.origin_client_instance_id:
                    raise CommandClientMismatch(
                        "frontend command target does not match run client"
                    )

            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (f"tool-observation:{result.result_id}",),
            )
            result_row = await (
                await connection.execute(
                    f'SELECT run_id, data_json FROM "{self._schema}".results '
                    "WHERE result_id = %s",
                    (result.result_id,),
                )
            ).fetchone()
            evidence_row = await (
                await connection.execute(
                    f'SELECT data_json FROM "{self._schema}".evidence '
                    "WHERE evidence_id = %s",
                    (evidence.evidence_id,),
                )
            ).fetchone()
            command_rows = []
            for command in commands:
                command_rows.append(
                    await (
                        await connection.execute(
                            f'SELECT data_json FROM "{self._schema}".frontend_commands '
                            "WHERE command_id = %s",
                            (command.command_id,),
                        )
                    ).fetchone()
                )

            if result_row is not None:
                stored_result = _DATA_RESULT_ADAPTER.validate_json(result_row[1])
                stored_evidence = (
                    Evidence.model_validate_json(evidence_row[0])
                    if evidence_row is not None
                    else None
                )
                stored_commands = tuple(
                    FrontendCommand.model_validate_json(row[0])
                    if row is not None
                    else None
                    for row in command_rows
                )
                if (
                    result_row[0] != run_id
                    or not _same_observation_result(stored_result, result)
                    or stored_evidence is None
                    or not _same_observation_evidence(stored_evidence, evidence)
                    or any(command is None for command in stored_commands)
                    or not all(
                        _same_observation_command(stored, requested)
                        for stored, requested in zip(
                            stored_commands, commands, strict=True
                        )
                        if stored is not None
                    )
                ):
                    raise RunStateConflict(
                        "observation identity is already bound differently"
                    )
                return stored_result, stored_evidence, tuple(
                    command for command in stored_commands if command is not None
                )
            if evidence_row is not None or any(row is not None for row in command_rows):
                raise RunStateConflict("observation identity is partially occupied")

            await connection.execute(
                f'INSERT INTO "{self._schema}".results '
                "(result_id, run_id, data_json) VALUES (%s, %s, %s)",
                (result.result_id, run_id, result.model_dump_json()),
            )
            await connection.execute(
                f'INSERT INTO "{self._schema}".evidence '
                "(evidence_id, result_id, data_json) VALUES (%s, %s, %s)",
                (evidence.evidence_id, result.result_id, evidence.model_dump_json()),
            )
            for command in commands:
                await connection.execute(
                    f'INSERT INTO "{self._schema}".frontend_commands '
                    "(command_id, run_id, data_json) VALUES (%s, %s, %s)",
                    (command.command_id, run_id, command.model_dump_json()),
                )
        return result, evidence, commands

    async def get_result(
        self,
        *,
        user_id: str,
        result_id: str,
        tenant_id: str | None = None,
        app_id: str | None = None,
    ) -> DataResult:
        await self._ensure_initialized()
        async with await psycopg.AsyncConnection.connect(self._dsn) as connection:
            row = await (
                await connection.execute(
                    f'SELECT r.data_json, ru.data_json, s.data_json '
                    f'FROM "{self._schema}".results r '
                    f'JOIN "{self._schema}".runs ru ON ru.run_id = r.run_id '
                    f'JOIN "{self._schema}".sessions s ON s.session_id = ru.session_id '
                    "WHERE r.result_id = %s",
                    (result_id,),
                )
            ).fetchone()
        session = AgentSession.model_validate_json(row[2]) if row is not None else None
        if not _owns_resource(
            session, user_id=user_id, tenant_id=tenant_id, app_id=app_id
        ):
            raise ResourceNotFound("result not found")
        assert row is not None
        return _DATA_RESULT_ADAPTER.validate_json(row[0])

    async def get_result_for_run(
        self,
        *,
        user_id: str,
        run_id: str,
        result_id: str,
        tenant_id: str | None = None,
        app_id: str | None = None,
    ) -> DataResult:
        await self._ensure_initialized()
        async with await psycopg.AsyncConnection.connect(self._dsn) as connection:
            row = await (
                await connection.execute(
                    f'SELECT r.data_json, s.data_json '
                    f'FROM "{self._schema}".results r '
                    f'JOIN "{self._schema}".runs ru ON ru.run_id = r.run_id '
                    f'JOIN "{self._schema}".sessions s ON s.session_id = ru.session_id '
                    "WHERE r.result_id = %s AND r.run_id = %s",
                    (result_id, run_id),
                )
            ).fetchone()
        session = AgentSession.model_validate_json(row[1]) if row is not None else None
        if not _owns_resource(
            session, user_id=user_id, tenant_id=tenant_id, app_id=app_id
        ):
            raise ResourceNotFound("result not found")
        assert row is not None
        return _DATA_RESULT_ADAPTER.validate_json(row[0])

    async def save_evidence(
        self, *, user_id: str, run_id: str, evidence: Evidence
    ) -> Evidence:
        async with await self._owned_run_connection(user_id, run_id) as owned:
            connection, _run, _session = owned
            row = await (
                await connection.execute(
                    f'SELECT 1 FROM "{self._schema}".results '
                    "WHERE result_id = %s AND run_id = %s",
                    (evidence.result_id, run_id),
                )
            ).fetchone()
            if row is None:
                raise ResourceNotFound("result not found")
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (f"evidence:{evidence.evidence_id}",),
            )
            existing_row = await (
                await connection.execute(
                    f'SELECT data_json FROM "{self._schema}".evidence '
                    "WHERE evidence_id = %s FOR UPDATE",
                    (evidence.evidence_id,),
                )
            ).fetchone()
            if existing_row is not None:
                existing = Evidence.model_validate_json(existing_row[0])
                if existing != evidence:
                    raise RunStateConflict(
                        "evidence identity is already bound differently"
                    )
                return existing
            await connection.execute(
                f'INSERT INTO "{self._schema}".evidence '
                "(evidence_id, result_id, data_json) VALUES (%s, %s, %s)",
                (
                    evidence.evidence_id,
                    evidence.result_id,
                    evidence.model_dump_json(),
                ),
            )
        return evidence

    async def get_evidence(
        self,
        *,
        user_id: str,
        evidence_id: str,
        tenant_id: str | None = None,
        app_id: str | None = None,
    ) -> Evidence:
        await self._ensure_initialized()
        async with await psycopg.AsyncConnection.connect(self._dsn) as connection:
            row = await (
                await connection.execute(
                    f'SELECT e.data_json, s.data_json '
                    f'FROM "{self._schema}".evidence e '
                    f'JOIN "{self._schema}".results r ON r.result_id = e.result_id '
                    f'JOIN "{self._schema}".runs ru ON ru.run_id = r.run_id '
                    f'JOIN "{self._schema}".sessions s ON s.session_id = ru.session_id '
                    "WHERE e.evidence_id = %s",
                    (evidence_id,),
                )
            ).fetchone()
        session = AgentSession.model_validate_json(row[1]) if row is not None else None
        if not _owns_resource(
            session, user_id=user_id, tenant_id=tenant_id, app_id=app_id
        ):
            raise ResourceNotFound("evidence not found")
        assert row is not None
        return Evidence.model_validate_json(row[0])

    async def get_evidence_for_run(
        self,
        *,
        user_id: str,
        run_id: str,
        evidence_id: str,
        tenant_id: str | None = None,
        app_id: str | None = None,
    ) -> Evidence:
        await self._ensure_initialized()
        async with await psycopg.AsyncConnection.connect(self._dsn) as connection:
            row = await (
                await connection.execute(
                    f'SELECT e.data_json, s.data_json '
                    f'FROM "{self._schema}".evidence e '
                    f'JOIN "{self._schema}".results r ON r.result_id = e.result_id '
                    f'JOIN "{self._schema}".runs ru ON ru.run_id = r.run_id '
                    f'JOIN "{self._schema}".sessions s ON s.session_id = ru.session_id '
                    "WHERE e.evidence_id = %s AND r.run_id = %s",
                    (evidence_id, run_id),
                )
            ).fetchone()
        session = AgentSession.model_validate_json(row[1]) if row is not None else None
        if not _owns_resource(
            session, user_id=user_id, tenant_id=tenant_id, app_id=app_id
        ):
            raise ResourceNotFound("evidence not found")
        assert row is not None
        return Evidence.model_validate_json(row[0])

    async def save_frontend_command(
        self, *, user_id: str, run_id: str, command: FrontendCommand
    ) -> FrontendCommand:
        async with await self._owned_run_connection(user_id, run_id) as owned:
            connection, run, session = owned
            if run.status != "running" or session.active_run_id != run_id:
                raise RunStateConflict("frontend commands require an active run")
            if command.run_id != run_id:
                raise RunStateConflict("frontend command run does not match request path")
            if command.target_client_instance_id != run.origin_client_instance_id:
                raise CommandClientMismatch("frontend command target does not match run client")
            await connection.execute(
                f'INSERT INTO "{self._schema}".frontend_commands '
                "(command_id, run_id, data_json) VALUES (%s, %s, %s) "
                "ON CONFLICT (command_id) DO UPDATE SET data_json = EXCLUDED.data_json",
                (command.command_id, run_id, command.model_dump_json()),
            )
        return command

    async def put_frontend_command_receipt(
        self,
        *,
        user_id: str,
        run_id: str,
        command_id: str,
        receipt: FrontendCommandReceipt,
    ) -> FrontendCommandReceipt:
        await self._ensure_initialized()
        async with await psycopg.AsyncConnection.connect(self._dsn) as connection:
            row = await (
                await connection.execute(
                    f'SELECT c.data_json, r.data_json, s.data_json '
                    f'FROM "{self._schema}".frontend_commands c '
                    f'JOIN "{self._schema}".runs r ON r.run_id = c.run_id '
                    f'JOIN "{self._schema}".sessions s ON s.session_id = r.session_id '
                    "WHERE c.command_id = %s AND c.run_id = %s FOR UPDATE OF c",
                    (command_id, run_id),
                )
            ).fetchone()
            if row is None or AgentSession.model_validate_json(row[2]).owner_user_id != user_id:
                raise ResourceNotFound("frontend command not found")
            command = FrontendCommand.model_validate_json(row[0])
            if receipt.command_id != command_id:
                raise RunStateConflict("receipt command does not match request path")
            if receipt.client_instance_id != command.target_client_instance_id:
                raise CommandClientMismatch("receipt client does not match command target")
            existing_row = await (
                await connection.execute(
                    f'SELECT data_json FROM "{self._schema}".frontend_command_receipts '
                    "WHERE command_id = %s AND client_instance_id = %s",
                    (command_id, receipt.client_instance_id),
                )
            ).fetchone()
            existing = (
                FrontendCommandReceipt.model_validate_json(existing_row[0])
                if existing_row is not None
                else None
            )
            stored = merge_receipt(existing, receipt)
            await connection.execute(
                f'INSERT INTO "{self._schema}".frontend_command_receipts '
                "(command_id, client_instance_id, data_json) VALUES (%s, %s, %s) "
                "ON CONFLICT (command_id, client_instance_id) DO UPDATE "
                "SET data_json = EXCLUDED.data_json",
                (command_id, receipt.client_instance_id, stored.model_dump_json()),
            )
        return stored

    async def cancel_run(self, *, user_id: str, run_id: str) -> AgentRun:
        async with await self._owned_run_connection(user_id, run_id) as owned:
            connection, run, session = owned
            if run.status == "cancelled":
                return run
            if session.active_run_id != run_id:
                raise RunStateConflict("run is not active")
            if run.status not in {
                "queued",
                "running",
                "waiting_input",
                "waiting_approval",
                "cancelling",
            }:
                raise RunStateConflict("run cannot be cancelled from its current state")
            now = datetime.now(UTC)
            cancelled = run.model_copy(
                update={
                    "status": "cancelled",
                    "outcome": "cancelled",
                    "completion_reason_code": "user_cancelled",
                    "current_phase": "cancelled",
                    "completed_at": now,
                    "state_version": run.state_version + 1,
                }
            )
            released = session.model_copy(
                update={
                    "active_run_id": None,
                    "updated_at": now,
                    "version": session.version + 1,
                }
            )
            await self._update_run(connection, cancelled)
            await self._update_session(connection, released)
        return cancelled

    async def wait_for_reauthentication(
        self,
        *,
        user_id: str,
        run_id: str,
        analysis_plan_id: str | None = None,
        analysis_request_id: str | None = None,
    ) -> tuple[AgentRun, PendingInputRequest]:
        if (analysis_plan_id is None) != (analysis_request_id is None):
            raise RunStateConflict("analysis reauthentication references are incomplete")
        async with await self._owned_run_connection(user_id, run_id) as owned:
            connection, run, session = owned
            row = await (
                await connection.execute(
                    f'SELECT data_json FROM "{self._schema}".input_requests '
                    "WHERE run_id = %s FOR UPDATE",
                    (run_id,),
                )
            ).fetchone()
            existing = PendingInputRequest.model_validate_json(row[0]) if row else None
            now = datetime.now(UTC)
            if (
                run.status == "waiting_input"
                and run.waiting_for == "reauth"
                and session.active_run_id == run_id
                and existing is not None
                and existing.kind == "reauth"
                and existing.closed_at is None
                and existing.expires_at > now
                and existing.run_state_version == run.state_version
            ):
                if analysis_plan_id is not None:
                    if existing.analysis_plan_id is None:
                        existing = existing.model_copy(
                            update={
                                "analysis_plan_id": analysis_plan_id,
                                "analysis_request_id": analysis_request_id,
                            }
                        )
                        await connection.execute(
                            f'UPDATE "{self._schema}".input_requests '
                            "SET data_json = %s WHERE run_id = %s",
                            (existing.model_dump_json(), run_id),
                        )
                    elif (
                        existing.analysis_plan_id != analysis_plan_id
                        or existing.analysis_request_id != analysis_request_id
                    ):
                        raise RunStateConflict(
                            "analysis reauthentication references changed"
                        )
                return run, existing
            renew_expired = (
                run.status == "waiting_input"
                and run.waiting_for == "reauth"
                and existing is not None
                and existing.kind == "reauth"
                and existing.closed_at is None
                and existing.expires_at <= now
                and existing.run_state_version == run.state_version
            )
            if (
                session.active_run_id != run_id
                or (run.status != "running" and not renew_expired)
            ):
                raise RunStateConflict("only an active running run can wait for reauthentication")
            waiting = run.model_copy(
                update={
                    "status": "waiting_input",
                    "current_phase": "waiting_input",
                    "waiting_for": "reauth",
                    "state_version": run.state_version + 1,
                }
            )
            pending = PendingInputRequest(
                input_request_id=new_id("inreq"),
                run_id=run_id,
                kind="reauth",
                prompt="登录凭据已失效，请重新认证后继续。",
                run_state_version=waiting.state_version,
                expires_at=now + timedelta(minutes=10),
                analysis_plan_id=analysis_plan_id,
                analysis_request_id=analysis_request_id,
            )
            await self._update_run(connection, waiting)
            await connection.execute(
                f'INSERT INTO "{self._schema}".input_requests (run_id, data_json) '
                "VALUES (%s, %s) ON CONFLICT (run_id) DO UPDATE "
                "SET data_json = EXCLUDED.data_json",
                (run_id, pending.model_dump_json()),
            )
        return waiting, pending

    async def get_pending_input(
        self, *, user_id: str, run_id: str
    ) -> PendingInputRequest:
        async with await self._owned_run_connection(user_id, run_id) as owned:
            connection, run, _session = owned
            row = await (
                await connection.execute(
                    f'SELECT data_json FROM "{self._schema}".input_requests '
                    "WHERE run_id = %s",
                    (run_id,),
                )
            ).fetchone()
            pending = PendingInputRequest.model_validate_json(row[0]) if row else None
            if pending is None:
                raise ResourceNotFound("pending input request not found")
        return pending

    async def resume_from_input(
        self,
        *,
        user_id: str,
        run_id: str,
        input_request_id: str,
        run_state_version: int,
    ) -> AgentRun:
        async with await self._owned_run_connection(user_id, run_id) as owned:
            connection, run, _session = owned
            row = await (
                await connection.execute(
                    f'SELECT data_json FROM "{self._schema}".input_requests '
                    "WHERE run_id = %s FOR UPDATE",
                    (run_id,),
                )
            ).fetchone()
            pending = PendingInputRequest.model_validate_json(row[0]) if row else None
            now = datetime.now(UTC)
            if (
                run.status in {"running", "completed", "failed"}
                and run.waiting_for is None
                and pending is not None
                and pending.kind == "reauth"
                and pending.closed_at is not None
                and pending.input_request_id == input_request_id
                and pending.run_state_version == run_state_version
                and run.state_version in {run_state_version + 1, run_state_version + 2}
            ):
                return run
            if (
                run.status != "waiting_input"
                or run.waiting_for != "reauth"
                or pending is None
                or pending.kind != "reauth"
                or pending.closed_at is not None
                or pending.input_request_id != input_request_id
                or pending.run_state_version != run_state_version
                or run.state_version != run_state_version
                or pending.expires_at <= now
            ):
                raise InputRequestClosed("input request is closed or stale")
            resumed = run.model_copy(
                update={
                    "status": "running",
                    "current_phase": "planning",
                    "waiting_for": None,
                    "state_version": run.state_version + 1,
                }
            )
            await self._update_run(connection, resumed)
            await connection.execute(
                f'UPDATE "{self._schema}".input_requests SET data_json = %s '
                "WHERE run_id = %s",
                (pending.model_copy(update={"closed_at": now}).model_dump_json(), run_id),
            )
        return resumed

    async def add_steer(self, *, user_id: str, steer: Steer) -> Steer:
        async with await self._owned_run_connection(user_id, steer.run_id) as owned:
            connection, run, _session = owned
            if run.status not in {"running", "waiting_input", "waiting_approval"}:
                raise RunStateConflict("run cannot accept steer in its current state")
            await connection.execute(
                f'INSERT INTO "{self._schema}".steers '
                "(steer_id, run_id, data_json) VALUES (%s, %s, %s)",
                (steer.steer_id, steer.run_id, steer.model_dump_json()),
            )
        return steer

    async def apply_pending_steers(self, *, user_id: str, run_id: str) -> list[Steer]:
        async with await self._owned_run_connection(user_id, run_id) as owned:
            connection, _run, _session = owned
            rows = await (
                await connection.execute(
                    f'SELECT steer_id, data_json FROM "{self._schema}".steers '
                    "WHERE run_id = %s FOR UPDATE",
                    (run_id,),
                )
            ).fetchall()
            now = datetime.now(UTC)
            applied: list[Steer] = []
            for steer_id, data_json in rows:
                steer = Steer.model_validate_json(data_json)
                if steer.status != "accepted":
                    continue
                updated = steer.model_copy(update={"status": "applied", "applied_at": now})
                await connection.execute(
                    f'UPDATE "{self._schema}".steers SET data_json = %s '
                    "WHERE steer_id = %s",
                    (updated.model_dump_json(), steer_id),
                )
                applied.append(updated)
        return applied

    async def complete_run(
        self,
        *,
        user_id: str,
        run_id: str,
        outcome: str,
        completion_reason_code: str,
    ) -> AgentRun:
        async with await self._owned_run_connection(user_id, run_id) as owned:
            connection, run, session = owned
            if session.active_run_id != run_id:
                raise RunStateConflict("run is not active")
            if run.status != "running":
                raise RunStateConflict("only a running run can be completed")
            now = datetime.now(UTC)
            completed = run.model_copy(
                update={
                    "status": "completed",
                    "outcome": outcome,
                    "completion_reason_code": completion_reason_code,
                    "current_phase": "completed",
                    "completed_at": now,
                    "state_version": run.state_version + 1,
                }
            )
            released = session.model_copy(
                update={
                    "active_run_id": None,
                    "updated_at": now,
                    "version": session.version + 1,
                }
            )
            await self._update_run(connection, completed)
            await self._update_session(connection, released)
        return completed

    async def fail_run(
        self, *, user_id: str, run_id: str, completion_reason_code: str
    ) -> AgentRun:
        async with await self._owned_run_connection(user_id, run_id) as owned:
            connection, run, session = owned
            if session.active_run_id != run_id or run.status != "running":
                raise RunStateConflict("only an active running run can fail")
            now = datetime.now(UTC)
            failed = run.model_copy(
                update={
                    "status": "failed",
                    "outcome": "failed",
                    "completion_reason_code": completion_reason_code,
                    "current_phase": "failed",
                    "completed_at": now,
                    "state_version": run.state_version + 1,
                }
            )
            released = session.model_copy(
                update={
                    "active_run_id": None,
                    "updated_at": now,
                    "version": session.version + 1,
                }
            )
            await self._update_run(connection, failed)
            await self._update_session(connection, released)
        return failed

    async def put(self, auth_context: AuthContext) -> AuthContext:
        await self._ensure_initialized()
        async with await psycopg.AsyncConnection.connect(self._dsn) as connection:
            await connection.execute(
                f'INSERT INTO "{self._schema}".auth_contexts '
                "(run_id, user_id, data_json) VALUES (%s, %s, %s) "
                "ON CONFLICT (run_id) DO UPDATE SET user_id = EXCLUDED.user_id, "
                "data_json = EXCLUDED.data_json",
                (
                    auth_context.run_id,
                    auth_context.principal.user_id,
                    auth_context.model_dump_json(),
                ),
            )
        return auth_context

    async def get(self, *, user_id: str, run_id: str) -> AuthContext:
        await self._ensure_initialized()
        async with await psycopg.AsyncConnection.connect(self._dsn) as connection:
            row = await (
                await connection.execute(
                    f'SELECT user_id, data_json FROM "{self._schema}".auth_contexts '
                    "WHERE run_id = %s",
                    (run_id,),
                )
            ).fetchone()
        if row is None or row[0] != user_id:
            raise ResourceNotFound("auth context not found")
        return AuthContext.model_validate_json(row[1])

    async def delete(self, *, run_id: str) -> None:
        await self._ensure_initialized()
        async with await psycopg.AsyncConnection.connect(self._dsn) as connection:
            await connection.execute(
                f'DELETE FROM "{self._schema}".auth_contexts WHERE run_id = %s',
                (run_id,),
            )

    async def publish(
        self,
        *,
        event_type: str,
        session_id: str,
        run_id: str,
        data: dict[str, object],
        idempotency_key: str | None = None,
    ) -> AgentEvent:
        await self._ensure_initialized()
        async with await psycopg.AsyncConnection.connect(self._dsn) as connection:
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (f"event:{run_id}",),
            )
            event_id = (
                canonical_fingerprint(
                    domain="event-idempotency:1.0",
                    value={"run_id": run_id, "key": idempotency_key},
                )
                if idempotency_key is not None
                else new_id("evt")
            )
            if idempotency_key is not None:
                existing_row = await (
                    await connection.execute(
                        f'SELECT data_json FROM "{self._schema}".events '
                        "WHERE event_id = %s",
                        (event_id,),
                    )
                ).fetchone()
                if existing_row is not None:
                    existing = AgentEvent.model_validate_json(existing_row[0])
                    if (
                        existing.type != event_type
                        or existing.session_id != session_id
                        or existing.run_id != run_id
                        or existing.data != data
                    ):
                        raise RunStateConflict(
                            "event idempotency key was reused differently"
                        )
                    if self._event_notifier is not None:
                        try:
                            await self._event_notifier.publish(
                                run_id=run_id,
                                event_id=existing.event_id,
                            )
                        except Exception:
                            logger.warning(
                                "event replay notification failed",
                                exc_info=True,
                                extra={"run_id": run_id, "event_id": existing.event_id},
                            )
                    return existing
            row = await (
                await connection.execute(
                    f'SELECT COALESCE(MAX(sequence), 0) FROM "{self._schema}".events '
                    "WHERE run_id = %s",
                    (run_id,),
                )
            ).fetchone()
            if row is None:
                raise RuntimeError("failed to allocate the next event sequence")
            event = AgentEvent(
                event_id=event_id,
                sequence=int(row[0]) + 1,
                type=event_type,
                session_id=session_id,
                run_id=run_id,
                trace_id=f"trc_{run_id}",
                data=data,
            )
            expires_at = datetime.now(UTC) + self._event_retention
            await connection.execute(
                f'INSERT INTO "{self._schema}".events '
                "(event_id, run_id, sequence, data_json, expires_at) "
                "VALUES (%s, %s, %s, %s, %s)",
                (
                    event.event_id,
                    run_id,
                    event.sequence,
                    event.model_dump_json(),
                    expires_at,
                ),
            )
        if self._event_notifier is not None:
            try:
                await self._event_notifier.publish(
                    run_id=run_id,
                    event_id=event.event_id,
                )
            except Exception:
                logger.warning(
                    "event persisted but Redis notification failed",
                    exc_info=True,
                    extra={"run_id": run_id, "event_id": event.event_id},
                )
        return event

    async def list_events(self, *, run_id: str) -> list[AgentEvent]:
        await self._ensure_initialized()
        async with await psycopg.AsyncConnection.connect(self._dsn) as connection:
            rows = await (
                await connection.execute(
                    f'SELECT data_json FROM "{self._schema}".events '
                    "WHERE run_id = %s AND expires_at > %s ORDER BY sequence",
                    (run_id, datetime.now(UTC)),
                )
            ).fetchall()
        return [AgentEvent.model_validate_json(row[0]) for row in rows]

    async def validate_cursor(
        self, *, run_id: str, after_event_id: str | None
    ) -> None:
        if after_event_id is None:
            return
        await self._ensure_initialized()
        async with await psycopg.AsyncConnection.connect(self._dsn) as connection:
            row = await (
                await connection.execute(
                    f'SELECT expires_at FROM "{self._schema}".events '
                    "WHERE run_id = %s AND event_id = %s",
                    (run_id, after_event_id),
                )
            ).fetchone()
        if row is None or row[0] <= datetime.now(UTC):
            raise EventHistoryExpired("event history is no longer available")

    async def stream(
        self, *, run_id: str, after_event_id: str | None = None
    ) -> AsyncIterator[AgentEvent]:
        terminal_types = {"run.completed", "run.failed", "run.cancelled"}
        cursor = 0
        existing = await self.list_events(run_id=run_id)
        if after_event_id is not None:
            for index, event in enumerate(existing):
                if event.event_id == after_event_id:
                    cursor = index + 1
                    break
        while True:
            events = await self.list_events(run_id=run_id)
            pending = events[cursor:]
            if not pending:
                latest_type = (
                    events[-1].type
                    if events
                    else await self._latest_event_type(run_id=run_id)
                )
                if latest_type in terminal_types:
                    if not events and await self.list_events(run_id=run_id):
                        continue
                    return
                if self._event_notifier is not None:
                    try:
                        await self._event_notifier.wait(
                            run_id=run_id,
                            timeout_seconds=self._event_poll_interval,
                        )
                    except Exception:
                        logger.warning(
                            "Redis event wait failed; falling back to database polling",
                            exc_info=True,
                            extra={"run_id": run_id},
                        )
                        await asyncio.sleep(self._event_poll_interval)
                else:
                    await asyncio.sleep(self._event_poll_interval)
                continue
            for event in pending:
                cursor += 1
                yield event
                if event.type in terminal_types:
                    return

    async def _latest_event_type(self, *, run_id: str) -> str | None:
        async with await psycopg.AsyncConnection.connect(self._dsn) as connection:
            row = await (
                await connection.execute(
                    f'SELECT data_json FROM "{self._schema}".events '
                    "WHERE run_id = %s ORDER BY sequence DESC LIMIT 1",
                    (run_id,),
                )
            ).fetchone()
        if row is None:
            return None
        return AgentEvent.model_validate_json(row[0]).type

    async def execute(
        self,
        *,
        user_id: str,
        scope: str,
        key: str,
        request_fingerprint: str,
        operation: Callable[[], Awaitable[T]],
    ) -> tuple[T, bool]:
        await self._ensure_initialized()
        lock_key = f"idempotency:{user_id}:{scope}:{key}"
        async with await psycopg.AsyncConnection.connect(self._dsn) as connection:
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (lock_key,),
            )
            row = await (
                await connection.execute(
                    f'SELECT request_fingerprint, result_type, result_json '
                    f'FROM "{self._schema}".idempotency_records '
                    "WHERE user_id = %s AND scope = %s AND key = %s",
                    (user_id, scope, key),
                )
            ).fetchone()
            if row is not None:
                if row[0] != request_fingerprint:
                    raise IdempotencyConflict(
                        "idempotency key was already used with a different request"
                    )
                return _decode_idempotent_result(row[1], row[2]), True  # type: ignore[return-value]
            result = await operation()
            result_type, result_json = _encode_idempotent_result(result)
            await connection.execute(
                f'INSERT INTO "{self._schema}".idempotency_records '
                "(user_id, scope, key, request_fingerprint, result_type, result_json) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (user_id, scope, key, request_fingerprint, result_type, result_json),
            )
        return result, False

    async def issue(
        self,
        *,
        raw_token: SecretStr,
        subject_user_id: str,
        app_id: str,
        run_id: str,
        source_expires_at: datetime,
    ) -> CredentialGrant:
        cipher = self._require_cipher()
        await self._ensure_initialized()
        now = datetime.now(UTC)
        grant = CredentialGrant(
            credential_ref=new_id("cred"),
            subject_user_id=subject_user_id,
            app_id=app_id,
            run_id=run_id,
            created_at=now,
            expires_at=min(source_expires_at, now + self._credential_ttl),
        )
        nonce = os.urandom(12)
        ciphertext = cipher.encrypt(
            nonce,
            raw_token.get_secret_value().encode("utf-8"),
            _credential_aad(grant),
        )
        async with await psycopg.AsyncConnection.connect(self._dsn) as connection:
            await connection.execute(
                f'INSERT INTO "{self._schema}".credentials '
                "(credential_ref, token_ciphertext, nonce, subject_user_id, app_id, "
                "run_id, created_at, expires_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    grant.credential_ref,
                    ciphertext,
                    nonce,
                    grant.subject_user_id,
                    grant.app_id,
                    grant.run_id,
                    grant.created_at,
                    grant.expires_at,
                ),
            )
        return grant

    async def resolve(
        self,
        *,
        credential_ref: str,
        subject_user_id: str,
        app_id: str,
        run_id: str,
    ) -> SecretStr:
        cipher = self._require_cipher()
        await self._ensure_initialized()
        async with await psycopg.AsyncConnection.connect(self._dsn) as connection:
            row = await (
                await connection.execute(
                    f'SELECT token_ciphertext, nonce, subject_user_id, app_id, run_id, '
                    f'created_at, expires_at, revoked_at FROM "{self._schema}".credentials '
                    "WHERE credential_ref = %s",
                    (credential_ref,),
                )
            ).fetchone()
        if row is None:
            raise CredentialUnavailable("credential not found")
        grant = CredentialGrant(
            credential_ref=credential_ref,
            subject_user_id=row[2],
            app_id=row[3],
            run_id=row[4],
            created_at=row[5],
            expires_at=row[6],
        )
        if (
            row[7] is not None
            or grant.subject_user_id != subject_user_id
            or grant.app_id != app_id
            or grant.run_id != run_id
            or grant.expires_at <= datetime.now(UTC)
        ):
            raise CredentialUnavailable("credential not found")
        try:
            plaintext = cipher.decrypt(row[1], row[0], _credential_aad(grant))
        except InvalidTag as exc:
            raise CredentialUnavailable("credential cannot be decrypted") from exc
        return SecretStr(plaintext.decode("utf-8"))

    async def revoke(self, *, credential_ref: str) -> None:
        await self._revoke_credentials("credential_ref = %s", (credential_ref,))

    async def revoke_subject(self, *, subject_user_id: str) -> None:
        await self._revoke_credentials("subject_user_id = %s", (subject_user_id,))

    async def _revoke_credentials(self, where: str, parameters: tuple[str, ...]) -> None:
        await self._ensure_initialized()
        async with await psycopg.AsyncConnection.connect(self._dsn) as connection:
            await connection.execute(
                f'UPDATE "{self._schema}".credentials SET revoked_at = %s WHERE {where}',
                (datetime.now(UTC), *parameters),
            )

    def _require_cipher(self) -> AESGCM:
        if self._cipher is None:
            raise CredentialUnavailable("credential store encryption key is not configured")
        return self._cipher

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
                # Apply capability/runtime migrations (V010+). These are applied
                # idempotently (all use IF NOT EXISTS / ON CONFLICT DO
                # NOTHING) so they're safe to re-run.
                for migration_sql in self._p2_migration_statements():
                    # Substitute the schema name.
                    rendered = migration_sql.replace(
                        "full_view_agent", self._schema
                    )
                    await connection.execute(rendered)
                await connection.execute(
                    f'UPDATE "{self._schema}".events SET expires_at = %s '
                    "WHERE expires_at IS NULL",
                    (datetime.now(UTC) + self._event_retention,),
                )
                await connection.execute(
                    f'ALTER TABLE "{self._schema}".events '
                    "ALTER COLUMN expires_at SET NOT NULL"
                )
            self._initialized = True

    def _p2_migration_statements(self) -> tuple[str, ...]:
        """Load V010+ migration SQL for in-process initialization.

        Tests that call ``persistence.initialize()`` need a fully-set-up
        schema including capability center + run-scoped binding tables.
        The migration files are read once at first call and cached.
        """
        if getattr(self, "_p2_migrations_cache", None) is not None:
            return self._p2_migrations_cache
        import pathlib

        migrations_dir = (
            pathlib.Path(__file__).resolve().parent.parent.parent.parent
            / "scripts"
            / "migrations"
        )
        stmts: list[str] = []
        for name in (
            "V010_capability_center.sql",
            "V011_seed_system_capabilities.sql",
            "V012_run_scoped_bindings.sql",
            "V013_application_registry.sql",
            "V014_session_application_isolation.sql",
            "V015_application_lifecycle.sql",
            "V016_seed_full_view_capabilities.sql",
            "V017_connector_management.sql",
            "V018_event_trend_capability.sql",
            "V019_event_category_capability.sql",
            "V020_knowledge_bases.sql",
            "V021_prompt_templates.sql",
            "V022_seed_knowledge_search.sql",
            "V023_enterprise_industry_distribution.sql",
            "V024_application_agents.sql",
            "V025_seed_governance_power.sql",
            "V026_runtime_observability_indexes.sql",
            "V027_tool_semantic_contracts.sql",
            "V028_prompt_authority_layers.sql",
            "V029_tool_intent_contracts.sql",
            "V030_model_reasoning_profiles.sql",
            "V031_model_resource_center.sql",
            "V032_model_reasoning_test_profile.sql",
            "V033_population_median_contract_boundary.sql",
            "V034_capability_guidance_fields.sql",
            "V035_populate_tool_guidance.sql",
        ):
            path = migrations_dir / name
            if path.exists():
                stmts.append(path.read_text(encoding="utf-8"))
        self._p2_migrations_cache = tuple(stmts)
        return self._p2_migrations_cache

    def _ddl_statements(self) -> tuple[str, ...]:
        prefix = f'"{self._schema}".'
        return (
            f"CREATE TABLE IF NOT EXISTS {prefix}sessions ("
            "session_id TEXT PRIMARY KEY, owner_tenant_id TEXT NOT NULL DEFAULT 'legacy', "
            "owner_user_id TEXT NOT NULL, app_id TEXT NOT NULL DEFAULT 'full_information_view', "
            "data_json TEXT NOT NULL)",
            f"CREATE TABLE IF NOT EXISTS {prefix}runs ("
            "run_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, data_json TEXT NOT NULL)",
            f"CREATE INDEX IF NOT EXISTS idx_fva_runs_session ON {prefix}runs(session_id)",
            f"CREATE TABLE IF NOT EXISTS {prefix}messages ("
            "message_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, run_id TEXT NOT NULL, "
            "created_at TIMESTAMPTZ NOT NULL, data_json TEXT NOT NULL)",
            f"CREATE INDEX IF NOT EXISTS idx_fva_messages_session "
            f"ON {prefix}messages(session_id, created_at, message_id)",
            f"CREATE TABLE IF NOT EXISTS {prefix}results ("
            "result_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, data_json TEXT NOT NULL)",
            f"CREATE TABLE IF NOT EXISTS {prefix}evidence ("
            "evidence_id TEXT PRIMARY KEY, result_id TEXT NOT NULL, data_json TEXT NOT NULL)",
            f"CREATE TABLE IF NOT EXISTS {prefix}frontend_commands ("
            "command_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, data_json TEXT NOT NULL)",
            f"CREATE INDEX IF NOT EXISTS idx_fva_frontend_commands_run "
            f"ON {prefix}frontend_commands(run_id)",
            f"CREATE TABLE IF NOT EXISTS {prefix}frontend_command_receipts ("
            "command_id TEXT NOT NULL, client_instance_id TEXT NOT NULL, "
            "data_json TEXT NOT NULL, PRIMARY KEY(command_id, client_instance_id))",
            f"CREATE TABLE IF NOT EXISTS {prefix}steers ("
            "steer_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, data_json TEXT NOT NULL)",
            f"CREATE TABLE IF NOT EXISTS {prefix}input_requests ("
            "run_id TEXT PRIMARY KEY, data_json TEXT NOT NULL)",
            f"CREATE TABLE IF NOT EXISTS {prefix}auth_contexts ("
            "run_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, data_json TEXT NOT NULL)",
            f"CREATE TABLE IF NOT EXISTS {prefix}events ("
            "event_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, sequence INTEGER NOT NULL, "
            "data_json TEXT NOT NULL, expires_at TIMESTAMPTZ NOT NULL, "
            "UNIQUE(run_id, sequence))",
            f"ALTER TABLE {prefix}events ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ",
            f"CREATE TABLE IF NOT EXISTS {prefix}idempotency_records ("
            "user_id TEXT NOT NULL, scope TEXT NOT NULL, key TEXT NOT NULL, "
            "request_fingerprint TEXT NOT NULL, result_type TEXT NOT NULL, "
            "result_json TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP, "
            "PRIMARY KEY(user_id, scope, key))",
            f"CREATE TABLE IF NOT EXISTS {prefix}credentials ("
            "credential_ref TEXT PRIMARY KEY, token_ciphertext BYTEA NOT NULL, "
            "nonce BYTEA NOT NULL, "
            "subject_user_id TEXT NOT NULL, app_id TEXT NOT NULL, run_id TEXT NOT NULL, "
            "created_at TIMESTAMPTZ NOT NULL, expires_at TIMESTAMPTZ NOT NULL, "
            "revoked_at TIMESTAMPTZ NULL)",
            f"CREATE INDEX IF NOT EXISTS idx_fva_credentials_subject "
            f"ON {prefix}credentials(subject_user_id)",
            # V001-tracked migration version. Required by V010+ migrations
            # which INSERT INTO schema_version.
            f"CREATE TABLE IF NOT EXISTS {prefix}schema_version ("
            "version INTEGER PRIMARY KEY, "
            "applied_at TIMESTAMPTZ NOT NULL DEFAULT now())",
        )

    async def _owned_run_connection(
        self, user_id: str, run_id: str, *, for_update: bool = True
    ):
        await self._ensure_initialized()
        connection = await psycopg.AsyncConnection.connect(self._dsn)
        return _OwnedRunConnection(
            persistence=self,
            connection=connection,
            user_id=user_id,
            run_id=run_id,
            for_update=for_update,
        )

    async def _update_run(
        self, connection: psycopg.AsyncConnection, run: AgentRun
    ) -> None:
        await connection.execute(
            f'UPDATE "{self._schema}".runs SET data_json = %s WHERE run_id = %s',
            (run.model_dump_json(), run.run_id),
        )

    async def _update_session(
        self, connection: psycopg.AsyncConnection, session: AgentSession
    ) -> None:
        await connection.execute(
            f'UPDATE "{self._schema}".sessions SET data_json = %s WHERE session_id = %s',
            (_session_json(session), session.session_id),
        )

    async def health_check(self) -> None:
        async with await psycopg.AsyncConnection.connect(self._dsn) as conn:
            await conn.execute("SELECT 1")


class _OwnedRunConnection:
    def __init__(
        self,
        *,
        persistence: PostgresAgentPersistence,
        connection: psycopg.AsyncConnection,
        user_id: str,
        run_id: str,
        for_update: bool,
    ) -> None:
        self._persistence = persistence
        self._connection = connection
        self._user_id = user_id
        self._run_id = run_id
        self._for_update = for_update

    async def __aenter__(
        self,
    ) -> tuple[psycopg.AsyncConnection, AgentRun, AgentSession]:
        suffix = " FOR UPDATE OF r, s" if self._for_update else ""
        row = await (
            await self._connection.execute(
                f'SELECT r.data_json, s.data_json FROM "{self._persistence.schema}".runs r '
                f'JOIN "{self._persistence.schema}".sessions s '
                f'ON s.session_id = r.session_id WHERE r.run_id = %s{suffix}',
                (self._run_id,),
            )
        ).fetchone()
        if row is None:
            await self._connection.rollback()
            await self._connection.close()
            raise ResourceNotFound("run not found")
        run = AgentRun.model_validate_json(row[0])
        session = AgentSession.model_validate_json(row[1])
        if session.owner_user_id != self._user_id:
            await self._connection.rollback()
            await self._connection.close()
            raise ResourceNotFound("run not found")
        return self._connection, run, session

    async def __aexit__(self, exc_type, _exc, _tb) -> None:
        try:
            if exc_type is None:
                await self._connection.commit()
            else:
                await self._connection.rollback()
        finally:
            await self._connection.close()


def _credential_aad(grant: CredentialGrant) -> bytes:
    return "\x1f".join(
        [
            grant.credential_ref,
            grant.subject_user_id,
            grant.app_id,
            grant.run_id,
            str(int(grant.expires_at.timestamp())),
        ]
    ).encode("utf-8")


def _encode_idempotent_result(result: object) -> tuple[str, str]:
    for result_type, model in (
        ("agent_session", AgentSession),
        ("agent_run", AgentRun),
        ("steer", Steer),
        ("analysis_plan", AnalysisPlan),
    ):
        if isinstance(result, model):
            return result_type, (
                _session_json(result)
                if isinstance(result, AgentSession)
                else result.model_dump_json()
            )
    raise TypeError(f"unsupported idempotent result type: {type(result).__name__}")


def _decode_idempotent_result(result_type: str, result_json: str) -> object:
    if result_type == "analysis_plan":
        return validate_plan_for_save(AnalysisPlan.model_validate_json(result_json))
    models = {
        "agent_session": AgentSession,
        "agent_run": AgentRun,
        "steer": Steer,
    }
    model = models.get(result_type)
    if model is None:
        raise RuntimeError(f"unknown persisted idempotent result type: {result_type}")
    return model.model_validate_json(result_json)


def _session_json(session: AgentSession) -> str:
    payload = session.model_dump(mode="json")
    payload["owner_tenant_id"] = session.owner_tenant_id
    payload["owner_user_id"] = session.owner_user_id
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _owns_resource(
    session: AgentSession | None,
    *,
    user_id: str,
    tenant_id: str | None,
    app_id: str | None,
) -> bool:
    return bool(
        session is not None
        and session.owner_user_id == user_id
        and (tenant_id is None or session.owner_tenant_id == tenant_id)
        and (app_id is None or session.app_id == app_id)
    )
