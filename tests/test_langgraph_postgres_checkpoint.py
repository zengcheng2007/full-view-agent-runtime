import os
from contextlib import asynccontextmanager
from typing import TypedDict
from uuid import uuid4

import psycopg
import pytest
from langgraph.graph import END, START, StateGraph

from full_view_agent.infrastructure.langgraph_checkpoint import (
    LangGraphPostgresCheckpointManager,
)


class CounterState(TypedDict):
    value: int


def postgres_test_dsn() -> str:
    dsn = os.getenv("FULL_VIEW_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("FULL_VIEW_TEST_DATABASE_URL is not configured")
    return dsn


def increment(state: CounterState) -> CounterState:
    return {"value": state["value"] + 1}


@pytest.mark.asyncio
async def test_initialize_closes_connection_when_advisory_unlock_fails(
    monkeypatch,
) -> None:
    class _Cursor:
        async def fetchone(self):
            return (True,)

    class _Connection:
        closed = False

        async def execute(self, query, _params=None):
            if "pg_advisory_unlock" in str(query):
                raise RuntimeError("unlock failed")
            return _Cursor()

        async def close(self):
            self.closed = True

    class _Saver:
        async def setup(self):
            return None

    connection = _Connection()

    async def connect(*_args, **_kwargs):
        return connection

    @asynccontextmanager
    async def open_saver():
        yield _Saver()

    monkeypatch.setattr(psycopg.AsyncConnection, "connect", connect)
    manager = LangGraphPostgresCheckpointManager(
        dsn="postgresql://unused",
        schema="fva_unlock_failure",
    )
    monkeypatch.setattr(manager, "_open_saver", open_saver)

    with pytest.raises(RuntimeError, match="unlock failed"):
        await manager.initialize()

    assert connection.closed is True


@pytest.mark.asyncio
async def test_official_langgraph_checkpoint_survives_manager_restart() -> None:
    schema = f"fva_langgraph_test_{uuid4().hex[:12]}"
    manager = LangGraphPostgresCheckpointManager(
        dsn=postgres_test_dsn(),
        schema=schema,
    )
    try:
        await manager.initialize()
        graph_builder = StateGraph(CounterState)
        graph_builder.add_node("increment", increment)
        graph_builder.add_edge(START, "increment")
        graph_builder.add_edge("increment", END)
        config = {"configurable": {"thread_id": "fva:run:run-1"}}

        async with manager.saver() as saver:
            graph = graph_builder.compile(checkpointer=saver)
            result = await graph.ainvoke({"value": 1}, config)
            assert result["value"] == 2

        restarted = LangGraphPostgresCheckpointManager(
            dsn=postgres_test_dsn(),
            schema=schema,
        )
        async with restarted.saver() as saver:
            graph = graph_builder.compile(checkpointer=saver)
            recovered = await graph.aget_state(config)

        assert recovered.values["value"] == 2
        async with await psycopg.AsyncConnection.connect(
            postgres_test_dsn(),
        ) as connection:
            tables = await (
                await connection.execute(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = %s",
                    (schema,),
                )
            ).fetchall()
        table_names = {str(row[0]) for row in tables}
        assert "checkpoints" in table_names
        assert "orchestration_checkpoint_mappings" not in table_names
    finally:
        async with await psycopg.AsyncConnection.connect(
            postgres_test_dsn(),
        ) as connection:
            await connection.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
