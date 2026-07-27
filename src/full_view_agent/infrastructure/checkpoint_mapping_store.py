# pyright: reportArgumentType=false, reportCallIssue=false

import asyncio
import re
from dataclasses import replace
from datetime import UTC, datetime

import psycopg

from full_view_agent.application.checkpoint_mapping import (
    CHECKPOINT_NAMESPACE,
    CheckpointThreadMapping,
    build_checkpoint_thread_id,
)
from full_view_agent.application.errors import ResourceNotFound, RunStateConflict

_SAFE_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]{0,62}$")


class InMemoryCheckpointMappingStore:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._mappings: dict[str, CheckpointThreadMapping] = {}

    async def ensure_mapping(
        self,
        *,
        user_id: str,
        run_id: str,
        session_id: str,
    ) -> CheckpointThreadMapping:
        async with self._lock:
            existing = self._mappings.get(run_id)
            if existing is not None:
                if (
                    existing.owner_user_id != user_id
                    or existing.session_id != session_id
                ):
                    raise ResourceNotFound("checkpoint mapping not found")
                return existing
            now = datetime.now(UTC)
            mapping = CheckpointThreadMapping(
                run_id=run_id,
                session_id=session_id,
                owner_user_id=user_id,
                thread_id=build_checkpoint_thread_id(run_id),
                checkpoint_ns=CHECKPOINT_NAMESPACE,
                orchestrator="langgraph",
                checkpoint_id=None,
                version=1,
                created_at=now,
                updated_at=now,
            )
            self._mappings[run_id] = mapping
            return mapping

    async def get_mapping(
        self,
        *,
        user_id: str,
        run_id: str,
    ) -> CheckpointThreadMapping:
        async with self._lock:
            mapping = self._mappings.get(run_id)
            if mapping is None or mapping.owner_user_id != user_id:
                raise ResourceNotFound("checkpoint mapping not found")
            return mapping

    async def record_checkpoint(
        self,
        *,
        user_id: str,
        run_id: str,
        checkpoint_id: str,
        expected_version: int,
    ) -> CheckpointThreadMapping:
        async with self._lock:
            mapping = self._mappings.get(run_id)
            if mapping is None or mapping.owner_user_id != user_id:
                raise ResourceNotFound("checkpoint mapping not found")
            if mapping.version != expected_version:
                if mapping.checkpoint_id == checkpoint_id:
                    return mapping
                raise RunStateConflict("checkpoint mapping version changed")
            updated = replace(
                mapping,
                checkpoint_id=checkpoint_id,
                version=mapping.version + 1,
                updated_at=datetime.now(UTC),
            )
            self._mappings[run_id] = updated
            return updated


class PostgresCheckpointMappingStore:
    """Product-ledger mapping for LangGraph threads and checkpoints."""

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
                await connection.execute(
                    f'CREATE TABLE IF NOT EXISTS "{self._schema}".'
                    "orchestration_checkpoint_mappings ("
                    "run_id TEXT PRIMARY KEY, "
                    "session_id TEXT NOT NULL, "
                    "owner_user_id TEXT NOT NULL, "
                    "thread_id VARCHAR(255) NOT NULL UNIQUE, "
                    "checkpoint_ns TEXT NOT NULL, "
                    "orchestrator TEXT NOT NULL CHECK (orchestrator = 'langgraph'), "
                    "checkpoint_id TEXT, "
                    "version BIGINT NOT NULL CHECK (version > 0), "
                    "created_at TIMESTAMPTZ NOT NULL, "
                    "updated_at TIMESTAMPTZ NOT NULL"
                    ")"
                )
                await connection.execute(
                    f'CREATE INDEX IF NOT EXISTS "{self._schema}_checkpoint_owner" '
                    f'ON "{self._schema}".orchestration_checkpoint_mappings '
                    "(owner_user_id, session_id)"
                )
            self._initialized = True

    async def ensure_mapping(
        self,
        *,
        user_id: str,
        run_id: str,
        session_id: str,
    ) -> CheckpointThreadMapping:
        await self.initialize()
        now = datetime.now(UTC)
        async with await psycopg.AsyncConnection.connect(self._dsn) as connection:
            await connection.execute(
                f'INSERT INTO "{self._schema}".orchestration_checkpoint_mappings '
                "(run_id, session_id, owner_user_id, thread_id, checkpoint_ns, "
                "orchestrator, checkpoint_id, version, created_at, updated_at) "
                "VALUES (%s, %s, %s, %s, %s, 'langgraph', NULL, 1, %s, %s) "
                "ON CONFLICT (run_id) DO NOTHING",
                (
                    run_id,
                    session_id,
                    user_id,
                    build_checkpoint_thread_id(run_id),
                    CHECKPOINT_NAMESPACE,
                    now,
                    now,
                ),
            )
            row = await (
                await connection.execute(
                    f'SELECT run_id, session_id, owner_user_id, thread_id, '
                    "checkpoint_ns, orchestrator, checkpoint_id, version, "
                    f'created_at, updated_at FROM "{self._schema}".'
                    "orchestration_checkpoint_mappings WHERE run_id = %s "
                    "AND owner_user_id = %s AND session_id = %s",
                    (run_id, user_id, session_id),
                )
            ).fetchone()
        if row is None:
            raise ResourceNotFound("checkpoint mapping not found")
        return _mapping_from_row(row)

    async def get_mapping(
        self,
        *,
        user_id: str,
        run_id: str,
    ) -> CheckpointThreadMapping:
        await self.initialize()
        async with await psycopg.AsyncConnection.connect(self._dsn) as connection:
            row = await (
                await connection.execute(
                    f'SELECT run_id, session_id, owner_user_id, thread_id, '
                    "checkpoint_ns, orchestrator, checkpoint_id, version, "
                    f'created_at, updated_at FROM "{self._schema}".'
                    "orchestration_checkpoint_mappings WHERE run_id = %s "
                    "AND owner_user_id = %s",
                    (run_id, user_id),
                )
            ).fetchone()
        if row is None:
            raise ResourceNotFound("checkpoint mapping not found")
        return _mapping_from_row(row)

    async def record_checkpoint(
        self,
        *,
        user_id: str,
        run_id: str,
        checkpoint_id: str,
        expected_version: int,
    ) -> CheckpointThreadMapping:
        await self.initialize()
        now = datetime.now(UTC)
        async with await psycopg.AsyncConnection.connect(self._dsn) as connection:
            row = await (
                await connection.execute(
                    f'UPDATE "{self._schema}".orchestration_checkpoint_mappings '
                    "SET checkpoint_id = %s, version = version + 1, updated_at = %s "
                    "WHERE run_id = %s AND owner_user_id = %s AND version = %s "
                    "RETURNING run_id, session_id, owner_user_id, thread_id, "
                    "checkpoint_ns, orchestrator, checkpoint_id, version, "
                    "created_at, updated_at",
                    (checkpoint_id, now, run_id, user_id, expected_version),
                )
            ).fetchone()
            if row is not None:
                return _mapping_from_row(row)
            existing = await (
                await connection.execute(
                    f'SELECT run_id, session_id, owner_user_id, thread_id, '
                    "checkpoint_ns, orchestrator, checkpoint_id, version, "
                    f'created_at, updated_at FROM "{self._schema}".'
                    "orchestration_checkpoint_mappings WHERE run_id = %s "
                    "AND owner_user_id = %s",
                    (run_id, user_id),
                )
            ).fetchone()
        if existing is None:
            raise ResourceNotFound("checkpoint mapping not found")
        current = _mapping_from_row(existing)
        if current.checkpoint_id == checkpoint_id:
            return current
        raise RunStateConflict("checkpoint mapping version changed")


def _mapping_from_row(row: tuple[object, ...]) -> CheckpointThreadMapping:
    return CheckpointThreadMapping(
        run_id=str(row[0]),
        session_id=str(row[1]),
        owner_user_id=str(row[2]),
        thread_id=str(row[3]),
        checkpoint_ns=str(row[4]),
        orchestrator="langgraph",
        checkpoint_id=None if row[6] is None else str(row[6]),
        version=int(str(row[7])),
        created_at=row[8],  # type: ignore[arg-type]
        updated_at=row[9],  # type: ignore[arg-type]
    )
