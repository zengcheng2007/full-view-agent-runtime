"""HTTP contract for trusted server-created AnalysisPlan resources."""

import httpx
import pytest
from pydantic import SecretStr

from full_view_agent.api.app import RuntimeContainer, create_app
from full_view_agent.infrastructure.credential_broker import InMemoryCredentialBroker
from full_view_agent.infrastructure.legacy_identity import HashedLegacyIdentityAdapter


def _runtime() -> RuntimeContainer:
    return RuntimeContainer(
        identity_port=HashedLegacyIdentityAdapter(),
        credentials=InMemoryCredentialBroker(),
    )


async def _create_run(client: httpx.AsyncClient, token: str) -> str:
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
            "mode": "agent",
        },
    )
    assert run.status_code == 202
    return str(run.json()["data"]["run_id"])


def _analysis_request() -> dict[str, object]:
    return {
        "request_id": "analysis-request-01",
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
    }


@pytest.mark.asyncio
async def test_create_analysis_plan_is_server_authored_persisted_and_idempotent() -> None:
    runtime = _runtime()
    app = create_app(runtime)
    token = "analysis-owner"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        run_id = await _create_run(client, token)
        headers = {"geoToken": token, "Idempotency-Key": "analysis-plan-01"}
        first = await client.post(
            f"/agent-api/v1/runs/{run_id}/analysis-plans",
            headers=headers,
            json=_analysis_request(),
        )
        replay = await client.post(
            f"/agent-api/v1/runs/{run_id}/analysis-plans",
            headers=headers,
            json=_analysis_request(),
        )

    assert first.status_code == 201
    assert replay.status_code == 201
    assert replay.json()["meta"]["idempotency_replayed"] is True
    plan = first.json()["data"]
    assert plan["request_id"] == "analysis-request-01"
    assert plan["goals"] == ["housing"]
    assert plan["scope_ref"]["scope"]["area_code"] == "330106"
    assert plan["steps"][0]["capability_id"] == "governance.query_housing_metrics"
    assert runtime.analysis_plan_repository is not None
    identity = await runtime.identity_port.resolve(SecretStr(token))
    stored = await runtime.analysis_plan_repository.get(
        tenant_id=identity.principal.tenant_id,
        user_id=identity.principal.user_id,
        run_id=run_id,
        plan_id=plan["plan_id"],
    )
    assert stored is not None
    assert stored.plan_id == plan["plan_id"]


@pytest.mark.asyncio
async def test_analysis_plan_is_hidden_from_another_user() -> None:
    app = create_app(_runtime())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        run_id = await _create_run(client, "analysis-owner")
        response = await client.post(
            f"/agent-api/v1/runs/{run_id}/analysis-plans",
            headers={
                "geoToken": "analysis-other",
                "Idempotency-Key": "analysis-other-plan",
            },
            json=_analysis_request(),
        )

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_analysis_plan_endpoint_rejects_client_supplied_plan_steps() -> None:
    app = create_app(_runtime())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        run_id = await _create_run(client, "analysis-owner-contract")
        payload = _analysis_request()
        payload["steps"] = [{"step_id": "client-forged"}]
        response = await client.post(
            f"/agent-api/v1/runs/{run_id}/analysis-plans",
            headers={
                "geoToken": "analysis-owner-contract",
                "Idempotency-Key": "analysis-forged-plan",
            },
            json=payload,
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
