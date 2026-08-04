"""HTTP contract for explicit server-side analysis plan execution."""

import asyncio
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from pydantic import SecretStr

from full_view_agent.api.app import RuntimeContainer, create_app
from full_view_agent.application.analysis_graph import AnalysisRunOutcome
from full_view_agent.domain.models import (
    AuthContext,
    LegacyIdentitySnapshot,
    Principal,
)
from full_view_agent.infrastructure.credential_broker import InMemoryCredentialBroker
from full_view_agent.infrastructure.legacy_identity import HashedLegacyIdentityAdapter


class _RecordingOrchestrator:
    """Fake analysis_orchestrator port that records the trusted invocation."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def run(
        self,
        *,
        user_id: str,
        session_id: str,
        analysis_run_id: str,
        plan_id: str,
        request_id: str,
        auth_context: AuthContext,
    ) -> AnalysisRunOutcome:
        self.calls.append(
            {
                "user_id": user_id,
                "session_id": session_id,
                "analysis_run_id": analysis_run_id,
                "plan_id": plan_id,
                "request_id": request_id,
                "auth_context": auth_context,
            }
        )
        return AnalysisRunOutcome(
            analysis_run_id=analysis_run_id,
            plan_id=plan_id,
            request_id=request_id,
            status="completed",
            reason_code="all_steps_completed",
            report_result_id="res_report_analysis_01",
        )

    async def resume(self, **_values) -> AnalysisRunOutcome:
        raise AssertionError("resume was not expected")


class _MutableIdentityAdapter:
    def __init__(self) -> None:
        self.tenant_id = "tenant-a"

    async def resolve(self, raw_token: SecretStr) -> LegacyIdentitySnapshot:
        del raw_token
        return LegacyIdentitySnapshot(
            principal=Principal(
                tenant_id=self.tenant_id,
                user_id="execution-user",
                org_id="execution-org",
                roles=["governance_analyst"],
            ),
            source="legacy_geo_user_fixture",
            source_session_expires_at=datetime.now(UTC) + timedelta(hours=1),
            base_area_codes=["330106"],
        )


def _runtime(
    *, orchestrator: _RecordingOrchestrator | None = None
) -> RuntimeContainer:
    return RuntimeContainer(
        identity_port=HashedLegacyIdentityAdapter(),
        credentials=InMemoryCredentialBroker(),
        analysis_orchestrator=orchestrator,
    )


def test_analysis_orchestrator_is_composed_when_enabled(monkeypatch) -> None:
    monkeypatch.setenv("FULL_VIEW_ANALYSIS_EXECUTION_ENABLED", "true")

    runtime = _runtime(orchestrator=None)

    assert runtime.analysis_orchestrator is not None


@pytest.mark.asyncio
async def test_public_analysis_run_is_exclusive_and_reaches_terminal(monkeypatch) -> None:
    monkeypatch.setenv("FULL_VIEW_ANALYSIS_EXECUTION_ENABLED", "true")
    runtime = _runtime(orchestrator=None)
    app = create_app(runtime)
    token = "analysis-exclusive"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        run = await _create_run(client, token)
        identity = await runtime.identity_port.resolve(SecretStr(token))
        await asyncio.sleep(0.05)
        queued = await runtime.store.get_run(
            user_id=identity.principal.user_id, run_id=str(run["run_id"])
        )
        assert queued.status == "queued"
        plan = await _create_plan(client, token, str(run["run_id"]))
        response = await client.post(
            f"/agent-api/v1/runs/{run['run_id']}/analysis-plans/{plan['plan_id']}/executions",
            headers={"geoToken": token},
            json={"request_id": plan["request_id"]},
        )

    assert response.status_code == 200
    terminal = await runtime.store.get_run(
        user_id=identity.principal.user_id, run_id=str(run["run_id"])
    )
    assert terminal.status == "completed"
    messages = await runtime.store.list_messages(
        user_id=identity.principal.user_id, session_id=str(run["session_id"])
    )
    assert [message.role for message in messages] == ["user", "assistant"]


async def _create_run(
    client: httpx.AsyncClient, token: str, *, mode: str = "analysis"
) -> dict[str, object]:
    session = await client.post(
        "/agent-api/v1/sessions",
        headers={"geoToken": token, "Idempotency-Key": f"session-{token}"},
        json={"title": "区域研判"},
    )
    assert session.status_code == 201
    run = await client.post(
        f"/agent-api/v1/sessions/{session.json()['data']['session_id']}/runs",
        headers={"geoToken": token, "Idempotency-Key": f"run-{token}"},
        json={
            "input": {
                "client_message_id": f"message-{token}",
                "content": [{"type": "text", "text": "分析西湖区住房情况"}],
            },
            "client": {
                "client_instance_id": f"client-{token}",
                "frontend_command_schema_versions": ["1.0"],
                "supported_commands": ["panel.show_table"],
            },
            "mode": mode,
        },
    )
    assert run.status_code == 202
    return dict(run.json()["data"])


async def _create_plan(
    client: httpx.AsyncClient, token: str, run_id: str
) -> dict[str, object]:
    response = await client.post(
        f"/agent-api/v1/runs/{run_id}/analysis-plans",
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
    assert response.status_code == 201
    return dict(response.json()["data"])


@pytest.mark.asyncio
async def test_execution_passes_trusted_boundaries_to_orchestrator() -> None:
    orchestrator = _RecordingOrchestrator()
    runtime = _runtime(orchestrator=orchestrator)
    app = create_app(runtime)
    token = "execution-owner"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        run = await _create_run(client, token)
        plan = await _create_plan(client, token, str(run["run_id"]))
        response = await client.post(
            f"/agent-api/v1/runs/{run['run_id']}/analysis-plans/{plan['plan_id']}/executions",
            headers={"geoToken": token},
            json={"request_id": plan["request_id"]},
        )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["analysis_run_id"] == run["run_id"]
    assert data["plan_id"] == plan["plan_id"]
    assert data["request_id"] == plan["request_id"]
    assert data["status"] == "completed"
    # The report is only referenced through the Result Store; the response
    # must never inline report payloads.
    assert data["report_result_id"] == "res_report_analysis_01"
    assert "report" not in data and "sections" not in data
    assert response.json()["meta"]["request_id"].startswith("req_")

    identity = await runtime.identity_port.resolve(SecretStr(token))
    assert len(orchestrator.calls) == 1
    call = orchestrator.calls[0]
    assert call["user_id"] == identity.principal.user_id
    assert call["session_id"] == run["session_id"]
    assert call["analysis_run_id"] == run["run_id"]
    assert call["plan_id"] == plan["plan_id"]
    assert call["request_id"] == plan["request_id"]
    auth_context = call["auth_context"]
    assert isinstance(auth_context, AuthContext)
    assert auth_context.principal == identity.principal
    assert auth_context.session_id == run["session_id"]
    assert auth_context.run_id == run["run_id"]


@pytest.mark.asyncio
async def test_execution_rejects_another_user_run() -> None:
    orchestrator = _RecordingOrchestrator()
    app = create_app(_runtime(orchestrator=orchestrator))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        run = await _create_run(client, "execution-owner")
        plan = await _create_plan(client, "execution-owner", str(run["run_id"]))
        response = await client.post(
            f"/agent-api/v1/runs/{run['run_id']}/analysis-plans/{plan['plan_id']}/executions",
            headers={"geoToken": "execution-intruder"},
            json={"request_id": plan["request_id"]},
        )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "resource_not_found"
    assert orchestrator.calls == []


@pytest.mark.asyncio
async def test_execution_rejects_a_general_agent_run() -> None:
    orchestrator = _RecordingOrchestrator()
    app = create_app(_runtime(orchestrator=orchestrator))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        run = await _create_run(client, "general-run", mode="agent")
        plan = await _create_plan(client, "general-run", str(run["run_id"]))
        response = await client.post(
            f"/agent-api/v1/runs/{run['run_id']}/analysis-plans/{plan['plan_id']}/executions",
            headers={"geoToken": "general-run"},
            json={"request_id": plan["request_id"]},
        )

    assert response.status_code == 409
    assert orchestrator.calls == []


@pytest.mark.asyncio
async def test_execution_rejects_identity_that_no_longer_matches_run_auth() -> None:
    identity = _MutableIdentityAdapter()
    orchestrator = _RecordingOrchestrator()
    runtime = RuntimeContainer(
        identity_port=identity,
        credentials=InMemoryCredentialBroker(),
        analysis_orchestrator=orchestrator,
    )
    app = create_app(runtime)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        run = await _create_run(client, "execution-tenant-switch")
        plan = await _create_plan(
            client, "execution-tenant-switch", str(run["run_id"])
        )
        identity.tenant_id = "tenant-b"
        response = await client.post(
            f"/agent-api/v1/runs/{run['run_id']}/analysis-plans/{plan['plan_id']}/executions",
            headers={"geoToken": "execution-tenant-switch"},
            json={"request_id": plan["request_id"]},
        )

    assert response.status_code == 404
    assert orchestrator.calls == []


@pytest.mark.asyncio
async def test_execution_without_orchestrator_is_unavailable_and_fail_closed() -> None:
    runtime = _runtime(orchestrator=None)
    app = create_app(runtime)
    assert runtime.analysis_orchestrator is None
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        run = await _create_run(client, "execution-missing-port")
        plan = await _create_plan(
            client, "execution-missing-port", str(run["run_id"])
        )
        response = await client.post(
            f"/agent-api/v1/runs/{run['run_id']}/analysis-plans/{plan['plan_id']}/executions",
            headers={"geoToken": "execution-missing-port"},
            json={"request_id": plan["request_id"]},
        )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "analysis_execution_unavailable"
    assert response.json()["error"]["retryable"] is True


@pytest.mark.asyncio
async def test_execution_rejects_client_supplied_plan_or_result() -> None:
    orchestrator = _RecordingOrchestrator()
    app = create_app(_runtime(orchestrator=orchestrator))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        run = await _create_run(client, "execution-injection")
        plan = await _create_plan(
            client, "execution-injection", str(run["run_id"])
        )
        response = await client.post(
            f"/agent-api/v1/runs/{run['run_id']}/analysis-plans/{plan['plan_id']}/executions",
            headers={"geoToken": "execution-injection"},
            json={
                "request_id": plan["request_id"],
                "plan": {"plan_id": "client-forged-plan", "steps": []},
                "execution": {"status": "completed"},
                "result": {"report_result_id": "client-forged-result"},
            },
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    assert orchestrator.calls == []


@pytest.mark.asyncio
async def test_execution_unknown_plan_is_not_found() -> None:
    orchestrator = _RecordingOrchestrator()
    app = create_app(_runtime(orchestrator=orchestrator))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        run = await _create_run(client, "execution-unknown-plan")
        response = await client.post(
            f"/agent-api/v1/runs/{run['run_id']}/analysis-plans/plan-does-not-exist/executions",
            headers={"geoToken": "execution-unknown-plan"},
            json={"request_id": "analysis-request-exec-01"},
        )

    assert response.status_code == 404
    assert orchestrator.calls == []


@pytest.mark.asyncio
async def test_execution_rejects_request_id_that_does_not_match_plan() -> None:
    orchestrator = _RecordingOrchestrator()
    app = create_app(_runtime(orchestrator=orchestrator))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        run = await _create_run(client, "execution-request-mismatch")
        plan = await _create_plan(
            client, "execution-request-mismatch", str(run["run_id"])
        )
        response = await client.post(
            f"/agent-api/v1/runs/{run['run_id']}/analysis-plans/{plan['plan_id']}/executions",
            headers={"geoToken": "execution-request-mismatch"},
            json={"request_id": "client-forged-request"},
        )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "run_state_conflict"
    assert orchestrator.calls == []
