import asyncio
import os

import pytest

from full_view_agent.infrastructure.analysis_run_lease import (
    PostgresAnalysisRunLeaseManager,
)


def _postgres_test_dsn() -> str:
    dsn = os.getenv("FULL_VIEW_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("FULL_VIEW_TEST_DATABASE_URL is not configured")
    return dsn


@pytest.mark.asyncio
async def test_postgres_analysis_run_lease_serializes_independent_workers() -> None:
    first = PostgresAnalysisRunLeaseManager(dsn=_postgres_test_dsn())
    second = PostgresAnalysisRunLeaseManager(dsn=_postgres_test_dsn())
    acquired = asyncio.Event()

    async def competing_worker() -> None:
        async with second.lease(analysis_run_id="analysis-run-pg-lock"):
            acquired.set()

    async with first.lease(analysis_run_id="analysis-run-pg-lock"):
        task = asyncio.create_task(competing_worker())
        await asyncio.sleep(0.05)
        assert not acquired.is_set()

    await asyncio.wait_for(task, timeout=2)
    assert acquired.is_set()
