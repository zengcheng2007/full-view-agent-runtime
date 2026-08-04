"""Real PostgreSQL execution through the production analysis composition root."""

import os
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql

from full_view_agent.api.app import RuntimeContainer
from full_view_agent.domain.models import AgentMessage, AgentRun, AgentSession, TextContent

from .test_analysis_executor import _full_auth_context, _overview_plan


def _postgres_test_dsn() -> str:
    dsn = os.getenv("FULL_VIEW_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("FULL_VIEW_TEST_DATABASE_URL is not configured")
    return dsn


@pytest.mark.asyncio
async def test_production_composition_survives_runtime_restart(monkeypatch) -> None:
    dsn = _postgres_test_dsn()
    suffix = uuid4().hex[:12]
    schema = f"fva_exec_{suffix}"
    checkpoint_schema = f"fva_exec_cp_{suffix}"
    monkeypatch.setenv("FULL_VIEW_DATABASE_URL", dsn)
    monkeypatch.setenv("FULL_VIEW_POSTGRES_SCHEMA", schema)
    monkeypatch.setenv(
        "FULL_VIEW_ANALYSIS_LANGGRAPH_POSTGRES_SCHEMA", checkpoint_schema
    )
    monkeypatch.setenv("FULL_VIEW_ANALYSIS_EXECUTION_ENABLED", "true")
    monkeypatch.setenv("FULL_VIEW_RUNTIME_PROFILE", "test")
    monkeypatch.setenv("FULL_VIEW_GOVERNANCE_ADAPTER", "memory")
    monkeypatch.setenv("FULL_VIEW_MODEL_PROVIDER", "deterministic")

    first_runtime = RuntimeContainer()
    assert first_runtime.analysis_orchestrator is not None
    assert first_runtime.analysis_plan_repository is not None
    assert first_runtime.store is not None
    auth = _full_auth_context()
    plan = _overview_plan(first_runtime.semantic_stack.catalog)
    await first_runtime.store.create_session(
        AgentSession(
            session_id=auth.session_id,
            owner_user_id=auth.principal.user_id,
            title="production analysis e2e",
        )
    )
    message = AgentMessage(
        message_id=f"msg-{auth.run_id}",
        session_id=auth.session_id,
        run_id=auth.run_id,
        role="user",
        content=[TextContent(type="text", text="analysis")],
    )
    await first_runtime.store.create_run_if_session_idle(
        user_id=auth.principal.user_id,
        session_id=auth.session_id,
        run=AgentRun(
            run_id=auth.run_id,
            session_id=auth.session_id,
            origin_client_instance_id="analysis-e2e",
            status="queued",
            input_message_id=message.message_id,
            base_context_version=1,
        ),
        input_message=message,
    )
    await first_runtime.store.start_run(
        user_id=auth.principal.user_id, run_id=auth.run_id
    )
    await first_runtime.analysis_plan_repository.save(
        tenant_id=auth.principal.tenant_id,
        user_id=auth.principal.user_id,
        run_id=auth.run_id,
        plan=plan,
    )

    first = await first_runtime.analysis_orchestrator.run(
        user_id=auth.principal.user_id,
        session_id=auth.session_id,
        analysis_run_id=auth.run_id,
        plan_id=plan.plan_id,
        request_id=plan.request_id,
        auth_context=auth,
    )

    restarted = RuntimeContainer()
    assert restarted.analysis_orchestrator is not None
    replayed = await restarted.analysis_orchestrator.run(
        user_id=auth.principal.user_id,
        session_id=auth.session_id,
        analysis_run_id=auth.run_id,
        plan_id=plan.plan_id,
        request_id=plan.request_id,
        auth_context=auth,
    )

    assert first.status == "completed"
    assert replayed == first
    async with await psycopg.AsyncConnection.connect(dsn) as connection:
        step_count = await (
            await connection.execute(
                sql.SQL("SELECT count(*) FROM {}.analysis_step_ledger").format(
                    sql.Identifier(schema)
                )
            )
        ).fetchone()
        report_count = await (
            await connection.execute(
                sql.SQL("SELECT count(*) FROM {}.results WHERE result_id = %s").format(
                    sql.Identifier(schema)
                ),
                (first.report_result_id,),
            )
        ).fetchone()
        assert step_count == (len(plan.steps),)
        assert report_count == (1,)
        await connection.execute(
            sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema))
        )
        await connection.execute(
            sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                sql.Identifier(checkpoint_schema)
            )
        )
