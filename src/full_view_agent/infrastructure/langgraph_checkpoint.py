# pyright: reportArgumentType=false, reportCallIssue=false

import asyncio
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Protocol

import psycopg
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from psycopg.conninfo import make_conninfo

_SAFE_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]{0,62}$")


class LangGraphCheckpointManager(Protocol):
    def saver(self) -> Any: ...


class InMemoryCheckpointManager:
    """Process-local fallback used only when no PostgreSQL DSN is configured."""

    def __init__(self) -> None:
        self._saver = InMemorySaver(
            serde=JsonPlusSerializer(
                pickle_fallback=False,
                allowed_json_modules=(),
            )
        )

    @asynccontextmanager
    async def saver(self) -> AsyncIterator[InMemorySaver]:
        yield self._saver


class LangGraphPostgresCheckpointManager:
    """Owns official LangGraph checkpoint tables in an isolated schema."""

    def __init__(
        self,
        *,
        dsn: str,
        schema: str = "full_view_agent_langgraph",
    ) -> None:
        if not _SAFE_IDENTIFIER.fullmatch(schema):
            raise ValueError("PostgreSQL schema must be a safe lower-case identifier")
        self._dsn = dsn
        self._schema = schema
        self._init_lock = asyncio.Lock()
        self._initialized = False
        self._serde = JsonPlusSerializer(
            pickle_fallback=False,
            allowed_json_modules=(),
        )

    @property
    def schema(self) -> str:
        return self._schema

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
            async with self._open_saver() as saver:
                await saver.setup()
            self._initialized = True

    @asynccontextmanager
    async def saver(self) -> AsyncIterator[AsyncPostgresSaver]:
        await self.initialize()
        async with self._open_saver() as saver:
            yield saver

    @asynccontextmanager
    async def _open_saver(self) -> AsyncIterator[AsyncPostgresSaver]:
        scoped_dsn = make_conninfo(
            self._dsn,
            options=f"-c search_path={self._schema}",
        )
        async with AsyncPostgresSaver.from_conn_string(
            scoped_dsn,
            serde=self._serde,
        ) as saver:
            yield saver
