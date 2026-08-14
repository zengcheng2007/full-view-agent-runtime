"""Real PostgreSQL execution through the production analysis composition root."""

import asyncio
import json
import os
from base64 import urlsafe_b64encode
from uuid import uuid4

import httpx
import psycopg
import pytest
from psycopg import sql
from pydantic import SecretStr

from full_view_agent.api.app import RuntimeContainer, create_app
from full_view_agent.domain.models import AgentMessage, AgentRun, AgentSession, TextContent
from full_view_agent.infrastructure.legacy_identity import HashedLegacyIdentityAdapter

from .test_analysis_execution_api import _create_plan, _create_run
from .test_analysis_executor import _full_auth_context, _overview_plan


def _postgres_test_dsn() -> str:
    dsn = os.getenv("FULL_VIEW_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("FULL_VIEW_TEST_DATABASE_URL is not configured")
    return dsn


@pytest.mark.asyncio
@pytest.mark.db
async def test_queued_plan_is_discoverable_after_runtime_restart(monkeypatch) -> None:
    dsn = _postgres_test_dsn()
    suffix = uuid4().hex[:12]
    schema = f"fva_queued_{suffix}"
    checkpoint_schema = f"fva_queued_cp_{suffix}"
    monkeypatch.setenv("FULL_VIEW_DATABASE_URL", dsn)
    monkeypatch.setenv("FULL_VIEW_POSTGRES_SCHEMA", schema)
    monkeypatch.setenv(
        "FULL_VIEW_ANALYSIS_LANGGRAPH_POSTGRES_SCHEMA", checkpoint_schema
    )
    monkeypatch.setenv("FULL_VIEW_ANALYSIS_EXECUTION_ENABLED", "true")
    monkeypatch.setenv("FULL_VIEW_RUNTIME_PROFILE", "test")
    monkeypatch.setenv("FULL_VIEW_GOVERNANCE_ADAPTER", "memory")
    monkeypatch.setenv("FULL_VIEW_MODEL_PROVIDER", "deterministic")
    monkeypatch.setenv(
        "FULL_VIEW_CREDENTIAL_KEY",
        urlsafe_b64encode(b"queued-plan-test-key-32-bytes!!!").decode("ascii"),
    )
    token = f"queued-restart-{suffix}"

    first_runtime = RuntimeContainer(identity_port=HashedLegacyIdentityAdapter())
    first_app = create_app(first_runtime)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=first_app), base_url="http://test"
        ) as client:
            run = await _create_run(client, token)
            plan = await _create_plan(client, token, str(run["run_id"]))

        restarted_app = create_app(
            RuntimeContainer(identity_port=HashedLegacyIdentityAdapter())
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=restarted_app), base_url="http://test"
        ) as client:
            replayed_plan = await _create_plan(client, token, str(run["run_id"]))
            discovered = await client.get(
                f"/agent-api/v1/runs/{run['run_id']}/analysis-plans/current",
                headers={"geoToken": token},
            )

        assert replayed_plan == plan
        assert discovered.status_code == 200
        assert discovered.json()["data"] == plan

        async with await psycopg.AsyncConnection.connect(dsn) as connection:
            row = await (
                await connection.execute(
                    sql.SQL(
                        "SELECT result_json FROM {}.idempotency_records "
                        "WHERE result_type = 'analysis_plan'"
                    ).format(sql.Identifier(schema))
                )
            ).fetchone()
            assert row is not None
            tampered = json.loads(row[0])
            tampered["request_id"] = "analysis-request-tampered"
            await connection.execute(
                sql.SQL(
                    "UPDATE {}.idempotency_records SET result_json = %s "
                    "WHERE result_type = 'analysis_plan'"
                ).format(sql.Identifier(schema)),
                (json.dumps(tampered),),
            )

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=restarted_app), base_url="http://test"
        ) as client:
            tampered_replay = await client.post(
                f"/agent-api/v1/runs/{run['run_id']}/analysis-plans",
                headers={"geoToken": token, "Idempotency-Key": f"plan-{token}"},
                json={
                    "request_id": "analysis-request-exec-01",
                    "goals": ["housing"],
                    "scope_ref": {
                        "kind": "area",
                        "scope": {"area_code": "330106"},
                    },
                    "budget": {
                        "max_parallel": 2,
                        "max_tool_calls": 4,
                        "total_timeout_ms": 30_000,
                    },
                },
            )

        assert tampered_replay.status_code == 503
        assert (
            tampered_replay.json()["error"]["code"]
            == "analysis_planning_unavailable"
        )
    finally:
        async with await psycopg.AsyncConnection.connect(dsn) as connection:
            await connection.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                    sql.Identifier(schema)
                )
            )
            await connection.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                    sql.Identifier(checkpoint_schema)
                )
            )


@pytest.mark.asyncio
@pytest.mark.db
async def test_two_runtime_workers_publish_one_terminal_record(monkeypatch) -> None:
    dsn = _postgres_test_dsn()
    suffix = uuid4().hex[:12]
    schema = f"fva_workers_{suffix}"
    checkpoint_schema = f"fva_workers_cp_{suffix}"
    monkeypatch.setenv("FULL_VIEW_DATABASE_URL", dsn)
    monkeypatch.setenv("FULL_VIEW_POSTGRES_SCHEMA", schema)
    monkeypatch.setenv(
        "FULL_VIEW_ANALYSIS_LANGGRAPH_POSTGRES_SCHEMA", checkpoint_schema
    )
    monkeypatch.setenv("FULL_VIEW_ANALYSIS_EXECUTION_ENABLED", "true")
    monkeypatch.setenv("FULL_VIEW_RUNTIME_PROFILE", "test")
    monkeypatch.setenv("FULL_VIEW_GOVERNANCE_ADAPTER", "memory")
    monkeypatch.setenv("FULL_VIEW_MODEL_PROVIDER", "deterministic")
    monkeypatch.setenv(
        "FULL_VIEW_CREDENTIAL_KEY",
        urlsafe_b64encode(b"worker-race-test-key-32-bytes!!!").decode("ascii"),
    )
    token = f"worker-race-{suffix}"
    first_runtime = RuntimeContainer(identity_port=HashedLegacyIdentityAdapter())
    second_runtime = RuntimeContainer(identity_port=HashedLegacyIdentityAdapter())
    first_app = create_app(first_runtime)
    second_app = create_app(second_runtime)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=first_app), base_url="http://first"
        ) as first_client:
            run = await _create_run(first_client, token)
            plan = await _create_plan(first_client, token, str(run["run_id"]))
            path = (
                f"/agent-api/v1/runs/{run['run_id']}/analysis-plans/"
                f"{plan['plan_id']}/executions"
            )
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=second_app),
                base_url="http://second",
            ) as second_client:
                first, second = await asyncio.gather(
                    first_client.post(
                        path,
                        headers={"geoToken": token},
                        json={"request_id": plan["request_id"]},
                    ),
                    second_client.post(
                        path,
                        headers={"geoToken": token},
                        json={"request_id": plan["request_id"]},
                    ),
                )

        assert first.status_code == 200, first.json()
        assert second.status_code == 200, second.json()
        identity = await first_runtime.identity_port.resolve(SecretStr(token))
        messages = await first_runtime.store.list_messages(
            user_id=identity.principal.user_id,
            session_id=str(run["session_id"]),
        )
        events = await first_runtime.events.list_events(run_id=str(run["run_id"]))
        assert [message.role for message in messages].count("assistant") == 1
        assert [event.type for event in events].count("assistant.message.completed") == 1
        assert [event.type for event in events].count("run.completed") == 1
    finally:
        async with await psycopg.AsyncConnection.connect(dsn) as connection:
            await connection.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                    sql.Identifier(schema)
                )
            )
            await connection.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                    sql.Identifier(checkpoint_schema)
                )
            )


@pytest.mark.asyncio
@pytest.mark.db
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
    assert first_runtime.auth_contexts is not None
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
            mode="analysis",
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
    await first_runtime.auth_contexts.put(auth)

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
    assert restarted.store is not None
    assert await restarted.recover_runs() == 1
    recovered_run = await restarted.store.get_run(
        user_id=auth.principal.user_id,
        run_id=auth.run_id,
    )
    replayed = await restarted.analysis_orchestrator.run(
        user_id=auth.principal.user_id,
        session_id=auth.session_id,
        analysis_run_id=auth.run_id,
        plan_id=plan.plan_id,
        request_id=plan.request_id,
        auth_context=auth,
    )

    assert first.status == "completed"
    assert recovered_run.status == "completed"
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
