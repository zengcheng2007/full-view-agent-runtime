"""HTTP contract for explicit server-side analysis plan execution."""

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Literal

import httpx
import pytest
from pydantic import SecretStr

from full_view_agent.api.app import RuntimeContainer, create_app
from full_view_agent.application.analysis_graph import AnalysisRunOutcome
from full_view_agent.application.errors import ReauthenticationRequired
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


class _ReauthenticationOrchestrator:
    def __init__(
        self,
        *,
        reauth_on_first_resume: bool = False,
        terminal_status: Literal["completed", "failed"] = "completed",
    ) -> None:
        self.runtime: RuntimeContainer | None = None
        self.resume_calls = 0
        self.reauth_on_first_resume = reauth_on_first_resume
        self.terminal_status = terminal_status

    async def run(
        self,
        *,
        user_id: str,
        analysis_run_id: str,
        **_values: object,
    ) -> AnalysisRunOutcome:
        assert self.runtime is not None
        await self.runtime.service.wait_for_reauthentication(
            user_id=user_id,
            run_id=analysis_run_id,
        )
        raise ReauthenticationRequired("analysis credential expired")

    async def resume(
        self,
        *,
        user_id: str,
        analysis_run_id: str,
        plan_id: str,
        request_id: str,
        input_request_id: str,
        run_state_version: int,
        **_values: object,
    ) -> AnalysisRunOutcome:
        assert self.runtime is not None
        self.resume_calls += 1
        await self.runtime.service.resume_from_input(
            user_id=user_id,
            run_id=analysis_run_id,
            input_request_id=input_request_id,
            run_state_version=run_state_version,
        )
        if self.reauth_on_first_resume and self.resume_calls == 1:
            await self.runtime.service.wait_for_reauthentication(
                user_id=user_id,
                run_id=analysis_run_id,
            )
            raise ReauthenticationRequired("analysis credential expired again")
        return AnalysisRunOutcome(
            analysis_run_id=analysis_run_id,
            plan_id=plan_id,
            request_id=request_id,
            status=self.terminal_status,
            reason_code=(
                "all_steps_completed"
                if self.terminal_status == "completed"
                else "analysis_failed"
            ),
            report_result_id=(
                "res_report_after_reauth"
                if self.terminal_status == "completed"
                else None
            ),
        )


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
    assert messages[-1].content[0].text == "区域研判已完成，详细结果请查看研判报告。"  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_recovery_exposes_running_analysis_as_resumable_reauthentication() -> None:
    runtime = _runtime(orchestrator=_RecordingOrchestrator())
    app = create_app(runtime)
    token = "analysis-recovery"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        run = await _create_run(client, token)
        plan = await _create_plan(client, token, str(run["run_id"]))
        identity = await runtime.identity_port.resolve(SecretStr(token))
        await runtime.service.start_run(
            user_id=identity.principal.user_id,
            run_id=str(run["run_id"]),
        )
        await runtime.analysis_binding_store.ensure_binding(  # type: ignore[union-attr]
            tenant_id=identity.principal.tenant_id,
            user_id=identity.principal.user_id,
            session_id=str(run["session_id"]),
            run_id=str(run["run_id"]),
            plan_id=str(plan["plan_id"]),
            request_id=str(plan["request_id"]),
            invocation_fingerprint=f"sha256:{'a' * 64}",
        )

        recovered = await runtime.recover_runs()
        discovered = await client.get(
            f"/agent-api/v1/runs/{run['run_id']}/pending-input",
            headers={"geoToken": token},
        )

    assert recovered == 1
    assert discovered.status_code == 200
    assert discovered.json()["data"]["analysis_plan_id"] == plan["plan_id"]
    assert discovered.json()["data"]["analysis_request_id"] == plan["request_id"]
    events = await runtime.events.list_events(run_id=str(run["run_id"]))  # type: ignore[union-attr]
    assert [event.type for event in events] == [
        "run.waiting",
        "input.required",
        "reauth_required",
    ]


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
async def test_concurrent_analysis_execution_has_one_terminal_message() -> None:
    orchestrator = _RecordingOrchestrator()
    runtime = _runtime(orchestrator=orchestrator)
    app = create_app(runtime)
    token = "execution-concurrent"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        run = await _create_run(client, token)
        plan = await _create_plan(client, token, str(run["run_id"]))
        path = (
            f"/agent-api/v1/runs/{run['run_id']}/analysis-plans/"
            f"{plan['plan_id']}/executions"
        )
        first, second = await asyncio.gather(
            client.post(
                path,
                headers={"geoToken": token},
                json={"request_id": plan["request_id"]},
            ),
            client.post(
                path,
                headers={"geoToken": token},
                json={"request_id": plan["request_id"]},
            ),
        )

    assert [first.status_code, second.status_code] == [200, 200]
    identity = await runtime.identity_port.resolve(SecretStr(token))
    messages = await runtime.store.list_messages(
        user_id=identity.principal.user_id,
        session_id=str(run["session_id"]),
    )
    assert [message.role for message in messages].count("assistant") == 1
    events = await runtime.events.list_events(run_id=str(run["run_id"]))  # type: ignore[union-attr]
    assert [event.type for event in events].count("assistant.message.completed") == 1
    assert [event.type for event in events].count("run.completed") == 1


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
        response = await client.post(
            f"/agent-api/v1/runs/{run['run_id']}/analysis-plans/plan-not-used/executions",
            headers={"geoToken": "general-run"},
            json={"request_id": "request-not-used"},
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


@pytest.mark.asyncio
async def test_analysis_reauthentication_is_exposed_and_resumes_to_terminal() -> None:
    orchestrator = _ReauthenticationOrchestrator()
    runtime = _runtime(orchestrator=orchestrator)  # type: ignore[arg-type]
    orchestrator.runtime = runtime
    app = create_app(runtime)
    token = "execution-reauth"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        run = await _create_run(client, token)
        plan = await _create_plan(client, token, str(run["run_id"]))
        execution = await client.post(
            f"/agent-api/v1/runs/{run['run_id']}/analysis-plans/{plan['plan_id']}/executions",
            headers={"geoToken": token},
            json={"request_id": plan["request_id"]},
        )
        assert execution.status_code == 409
        assert execution.json()["error"]["code"] == "reauthentication_required"

        events = await runtime.events.list_events(run_id=str(run["run_id"]))  # type: ignore[union-attr]
        assert [event.type for event in events] == [
            "run.waiting",
            "input.required",
            "reauth_required",
        ]
        required = events[1].data
        assert required["analysis_plan_id"] == plan["plan_id"]
        assert required["analysis_request_id"] == plan["request_id"]
        discovered = await client.get(
            f"/agent-api/v1/runs/{run['run_id']}/pending-input",
            headers={"geoToken": token},
        )
        assert discovered.status_code == 200
        assert discovered.json()["data"]["input_request_id"] == required[
            "input_request_id"
        ]
        assert discovered.json()["data"]["analysis_plan_id"] == plan["plan_id"]

        replay = await client.post(
            f"/agent-api/v1/runs/{run['run_id']}/analysis-plans/{plan['plan_id']}/executions",
            headers={"geoToken": token},
            json={"request_id": plan["request_id"]},
        )
        assert replay.status_code == 409
        replay_events = await runtime.events.list_events(run_id=str(run["run_id"]))  # type: ignore[union-attr]
        assert len(replay_events) == 3

        resumed = await client.post(
            f"/agent-api/v1/runs/{run['run_id']}/inputs",
            headers={
                "geoToken": token,
                "Idempotency-Key": "resume-execution-reauth",
            },
            json={
                "input_request_id": required["input_request_id"],
                "client_instance_id": "client-execution-reauth",
                "run_state_version": required["run_state_version"],
                "response": {"type": "reauthenticated"},
            },
        )

    assert resumed.status_code == 202
    assert resumed.json()["data"]["status"] == "completed"
    assert orchestrator.resume_calls == 1
    terminal_events = await runtime.events.list_events(run_id=str(run["run_id"]))  # type: ignore[union-attr]
    event_types = [event.type for event in terminal_events]
    assert event_types.index("input.received") < event_types.index(
        "assistant.message.completed"
    )
    assert event_types[-1] == "run.completed"


@pytest.mark.asyncio
async def test_forged_analysis_input_never_emits_received_event() -> None:
    orchestrator = _ReauthenticationOrchestrator()
    runtime = _runtime(orchestrator=orchestrator)  # type: ignore[arg-type]
    orchestrator.runtime = runtime
    app = create_app(runtime)
    token = "execution-forged-input"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        run = await _create_run(client, token)
        plan = await _create_plan(client, token, str(run["run_id"]))
        execution = await client.post(
            f"/agent-api/v1/runs/{run['run_id']}/analysis-plans/{plan['plan_id']}/executions",
            headers={"geoToken": token},
            json={"request_id": plan["request_id"]},
        )
        assert execution.status_code == 409
        pending_events = await runtime.events.list_events(run_id=str(run["run_id"]))  # type: ignore[union-attr]
        required = pending_events[1].data

        forged = await client.post(
            f"/agent-api/v1/runs/{run['run_id']}/inputs",
            headers={"geoToken": token, "Idempotency-Key": "forged-input"},
            json={
                "input_request_id": "inreq-forged",
                "client_instance_id": "client-forged-input",
                "run_state_version": required["run_state_version"],
                "response": {"type": "reauthenticated"},
                "analysis_plan_id": required["analysis_plan_id"],
                "analysis_request_id": required["analysis_request_id"],
            },
        )

    assert forged.status_code == 409
    events = await runtime.events.list_events(run_id=str(run["run_id"]))  # type: ignore[union-attr]
    assert "input.received" not in [event.type for event in events]


@pytest.mark.asyncio
async def test_second_analysis_reauthentication_exposes_fresh_pending_refs() -> None:
    orchestrator = _ReauthenticationOrchestrator(reauth_on_first_resume=True)
    runtime = _runtime(orchestrator=orchestrator)  # type: ignore[arg-type]
    orchestrator.runtime = runtime
    app = create_app(runtime)
    token = "execution-second-reauth"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        run = await _create_run(client, token)
        plan = await _create_plan(client, token, str(run["run_id"]))
        first_execution = await client.post(
            f"/agent-api/v1/runs/{run['run_id']}/analysis-plans/{plan['plan_id']}/executions",
            headers={"geoToken": token},
            json={"request_id": plan["request_id"]},
        )
        assert first_execution.status_code == 409
        first_events = await runtime.events.list_events(run_id=str(run["run_id"]))  # type: ignore[union-attr]
        first_pending = first_events[1].data

        first_resume = await client.post(
            f"/agent-api/v1/runs/{run['run_id']}/inputs",
            headers={"geoToken": token, "Idempotency-Key": "first-resume"},
            json={
                "input_request_id": first_pending["input_request_id"],
                "client_instance_id": "client-second-reauth",
                "run_state_version": first_pending["run_state_version"],
                "response": {"type": "reauthenticated"},
                "analysis_plan_id": first_pending["analysis_plan_id"],
                "analysis_request_id": first_pending["analysis_request_id"],
            },
        )
        assert first_resume.status_code == 409
        second_events = await runtime.events.list_events(run_id=str(run["run_id"]))  # type: ignore[union-attr]
        required_events = [event for event in second_events if event.type == "input.required"]
        assert len(required_events) == 2
        second_pending = required_events[-1].data
        assert second_pending["input_request_id"] != first_pending["input_request_id"]

        second_resume = await client.post(
            f"/agent-api/v1/runs/{run['run_id']}/inputs",
            headers={"geoToken": token, "Idempotency-Key": "second-resume"},
            json={
                "input_request_id": second_pending["input_request_id"],
                "client_instance_id": "client-second-reauth",
                "run_state_version": second_pending["run_state_version"],
                "response": {"type": "reauthenticated"},
                "analysis_plan_id": second_pending["analysis_plan_id"],
                "analysis_request_id": second_pending["analysis_request_id"],
            },
        )

    assert second_resume.status_code == 202
    assert second_resume.json()["data"]["status"] == "completed"
    assert orchestrator.resume_calls == 2


@pytest.mark.asyncio
async def test_failed_terminal_analysis_resume_replays_after_lost_response() -> None:
    orchestrator = _ReauthenticationOrchestrator(terminal_status="failed")
    runtime = _runtime(orchestrator=orchestrator)  # type: ignore[arg-type]
    orchestrator.runtime = runtime
    app = create_app(runtime)
    token = "execution-failed-replay"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        run = await _create_run(client, token)
        plan = await _create_plan(client, token, str(run["run_id"]))
        execution = await client.post(
            f"/agent-api/v1/runs/{run['run_id']}/analysis-plans/{plan['plan_id']}/executions",
            headers={"geoToken": token},
            json={"request_id": plan["request_id"]},
        )
        assert execution.status_code == 409
        events = await runtime.events.list_events(run_id=str(run["run_id"]))  # type: ignore[union-attr]
        pending = events[1].data
        payload = {
            "input_request_id": pending["input_request_id"],
            "client_instance_id": "client-failed-replay",
            "run_state_version": pending["run_state_version"],
            "response": {"type": "reauthenticated"},
            "analysis_plan_id": pending["analysis_plan_id"],
            "analysis_request_id": pending["analysis_request_id"],
        }

        first = await client.post(
            f"/agent-api/v1/runs/{run['run_id']}/inputs",
            headers={"geoToken": token, "Idempotency-Key": "failed-first"},
            json=payload,
        )
        replay = await client.post(
            f"/agent-api/v1/runs/{run['run_id']}/inputs",
            headers={"geoToken": token, "Idempotency-Key": "failed-replay"},
            json=payload,
        )

    assert first.status_code == 202
    assert first.json()["data"]["status"] == "failed"
    assert replay.status_code == 202
    assert replay.json()["data"]["status"] == "failed"
    terminal_events = await runtime.events.list_events(run_id=str(run["run_id"]))  # type: ignore[union-attr]
    assert [event.type for event in terminal_events].count("run.failed") == 1
