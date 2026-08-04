"""HTTP contract for trusted server-created AnalysisPlan resources."""

import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr

from full_view_agent.api.app import RuntimeContainer, create_app
from full_view_agent.domain.models import LegacyIdentitySnapshot, Principal
from full_view_agent.infrastructure.analysis_plan_repository import (
    PostgresAnalysisPlanRepository,
)
from full_view_agent.infrastructure.credential_broker import InMemoryCredentialBroker
from full_view_agent.infrastructure.legacy_identity import HashedLegacyIdentityAdapter


class _MutableIdentityAdapter:
    def __init__(self) -> None:
        self.tenant_id = "tenant-a"
        self.authorized = True

    async def resolve(self, raw_token: SecretStr) -> LegacyIdentitySnapshot:
        del raw_token
        return LegacyIdentitySnapshot(
            principal=Principal(
                tenant_id=self.tenant_id,
                user_id="analysis-user",
                org_id="analysis-org",
                roles=["governance_analyst"] if self.authorized else [],
            ),
            source=(
                "legacy_geo_user_fixture"
                if self.authorized
                else "legacy_geo_gateway"
            ),
            source_session_expires_at=datetime.now(UTC) + timedelta(hours=1),
            base_area_codes=["330106"],
        )


class _UnavailablePlanRepository:
    async def save(self, **_kwargs):
        raise RuntimeError("database connection lost")

    async def get(self, **_kwargs):
        return None


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
            "mode": "analysis",
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
async def test_create_analysis_plan_persists_through_real_postgres_api_path() -> None:
    dsn = os.getenv("FULL_VIEW_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("FULL_VIEW_TEST_DATABASE_URL is not configured")
    schema = f"fva_analysis_api_{uuid4().hex}"
    repository = PostgresAnalysisPlanRepository(dsn=dsn, schema=schema)
    runtime = RuntimeContainer(
        identity_port=HashedLegacyIdentityAdapter(),
        credentials=InMemoryCredentialBroker(),
        analysis_plan_repository=repository,
    )
    token = "analysis-postgres-owner"
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(runtime)),
            base_url="http://test",
        ) as client:
            run_id = await _create_run(client, token)
            response = await client.post(
                f"/agent-api/v1/runs/{run_id}/analysis-plans",
                headers={
                    "geoToken": token,
                    "Idempotency-Key": "analysis-postgres-plan",
                },
                json=_analysis_request(),
            )

        assert response.status_code == 201
        identity = await runtime.identity_port.resolve(SecretStr(token))
        restarted = PostgresAnalysisPlanRepository(dsn=dsn, schema=schema)
        stored = await restarted.get(
            tenant_id=identity.principal.tenant_id,
            user_id=identity.principal.user_id,
            run_id=run_id,
            plan_id=response.json()["data"]["plan_id"],
        )
        assert stored is not None
        assert stored.request_id == "analysis-request-01"
    finally:
        await repository.drop_schema()


@pytest.mark.asyncio
async def test_analysis_plan_refreshes_authorization_from_current_request() -> None:
    runtime = _runtime()
    app = create_app(runtime)
    token = "analysis-refresh"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        run_id = await _create_run(client, token)
        assert runtime.auth_contexts is not None
        before = await runtime.auth_contexts.get(
            user_id=(await runtime.identity_port.resolve(SecretStr(token))).principal.user_id,
            run_id=run_id,
        )
        assert before is not None
        response = await client.post(
            f"/agent-api/v1/runs/{run_id}/analysis-plans",
            headers={"geoToken": token, "Idempotency-Key": "analysis-refresh-plan"},
            json=_analysis_request(),
        )
        after = await runtime.auth_contexts.get(
            user_id=before.principal.user_id,
            run_id=run_id,
        )

    assert response.status_code == 201
    assert after.auth_context_id != before.auth_context_id
    assert after.principal == before.principal


@pytest.mark.asyncio
async def test_analysis_plan_rejects_same_user_from_another_tenant() -> None:
    identity = _MutableIdentityAdapter()
    runtime = RuntimeContainer(
        identity_port=identity,
        credentials=InMemoryCredentialBroker(),
    )
    app = create_app(runtime)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        run_id = await _create_run(client, "tenant-switch")
        identity.tenant_id = "tenant-b"
        response = await client.post(
            f"/agent-api/v1/runs/{run_id}/analysis-plans",
            headers={"geoToken": "tenant-switch", "Idempotency-Key": "tenant-b-plan"},
            json=_analysis_request(),
        )

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_analysis_plan_replay_rechecks_current_tenant_before_cache_hit() -> None:
    identity = _MutableIdentityAdapter()
    runtime = RuntimeContainer(
        identity_port=identity,
        credentials=InMemoryCredentialBroker(),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(runtime)),
        base_url="http://test",
    ) as client:
        run_id = await _create_run(client, "tenant-replay")
        headers = {
            "geoToken": "tenant-replay",
            "Idempotency-Key": "tenant-replay-plan",
        }
        first = await client.post(
            f"/agent-api/v1/runs/{run_id}/analysis-plans",
            headers=headers,
            json=_analysis_request(),
        )
        identity.tenant_id = "tenant-b"
        replay = await client.post(
            f"/agent-api/v1/runs/{run_id}/analysis-plans",
            headers=headers,
            json=_analysis_request(),
        )

    assert first.status_code == 201
    assert replay.status_code == 404


@pytest.mark.asyncio
async def test_analysis_plan_uses_current_revoked_permissions() -> None:
    identity = _MutableIdentityAdapter()
    runtime = RuntimeContainer(
        identity_port=identity,
        credentials=InMemoryCredentialBroker(),
    )
    app = create_app(runtime)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        run_id = await _create_run(client, "permission-revoked")
        identity.authorized = False
        response = await client.post(
            f"/agent-api/v1/runs/{run_id}/analysis-plans",
            headers={
                "geoToken": "permission-revoked",
                "Idempotency-Key": "permission-revoked-plan",
            },
            json=_analysis_request(),
        )

    assert response.status_code == 201
    assert response.json()["data"]["steps"] == []
    assert response.json()["data"]["omissions"][0]["reason_code"] == "NOT_ENTITLED"


@pytest.mark.asyncio
async def test_analysis_plan_replay_cannot_return_plan_after_permission_revocation() -> None:
    identity = _MutableIdentityAdapter()
    runtime = RuntimeContainer(
        identity_port=identity,
        credentials=InMemoryCredentialBroker(),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(runtime)),
        base_url="http://test",
    ) as client:
        run_id = await _create_run(client, "revoked-replay")
        headers = {
            "geoToken": "revoked-replay",
            "Idempotency-Key": "revoked-replay-plan",
        }
        first = await client.post(
            f"/agent-api/v1/runs/{run_id}/analysis-plans",
            headers=headers,
            json=_analysis_request(),
        )
        identity.authorized = False
        replay = await client.post(
            f"/agent-api/v1/runs/{run_id}/analysis-plans",
            headers=headers,
            json=_analysis_request(),
        )

    assert first.status_code == 201
    assert replay.status_code == 409
    assert replay.json()["error"]["code"] == "idempotency_conflict"


@pytest.mark.asyncio
async def test_analysis_plan_store_outage_is_retryable_503() -> None:
    runtime = RuntimeContainer(
        identity_port=HashedLegacyIdentityAdapter(),
        credentials=InMemoryCredentialBroker(),
        analysis_plan_repository=_UnavailablePlanRepository(),
    )
    app = create_app(runtime)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        run_id = await _create_run(client, "analysis-store-outage")
        response = await client.post(
            f"/agent-api/v1/runs/{run_id}/analysis-plans",
            headers={
                "geoToken": "analysis-store-outage",
                "Idempotency-Key": "analysis-store-outage-plan",
            },
            json=_analysis_request(),
        )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "analysis_planning_unavailable"
    assert response.json()["error"]["retryable"] is True


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
