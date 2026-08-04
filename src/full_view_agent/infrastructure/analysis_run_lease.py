"""Process-local and PostgreSQL leases for analysis-run serialization."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import psycopg


class InMemoryAnalysisRunLeaseManager:
    """Development/test lease shared by one runtime container."""

    def __init__(self) -> None:
        self._guard = asyncio.Lock()
        self._locks: dict[str, asyncio.Lock] = {}

    @asynccontextmanager
    async def lease(self, *, analysis_run_id: str) -> AsyncIterator[None]:
        async with self._guard:
            lock = self._locks.setdefault(analysis_run_id, asyncio.Lock())
        async with lock:
            yield


class PostgresAnalysisRunLeaseManager:
    """Cross-worker session advisory lock for one dedicated analysis run."""

    def __init__(self, *, dsn: str) -> None:
        self._dsn = dsn

    @asynccontextmanager
    async def lease(self, *, analysis_run_id: str) -> AsyncIterator[None]:
        connection = await psycopg.AsyncConnection.connect(self._dsn)
        lock_key = f"full-view-analysis-run:{analysis_run_id}"
        try:
            await connection.execute(
                "SELECT pg_advisory_lock(hashtextextended(%s, 0))",
                (lock_key,),
            )
            yield
        finally:
            if not connection.closed:
                try:
                    await connection.execute(
                        "SELECT pg_advisory_unlock(hashtextextended(%s, 0))",
                        (lock_key,),
                    )
                finally:
                    await connection.close()
