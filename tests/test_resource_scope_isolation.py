from datetime import UTC, datetime

import httpx
import pytest
from pydantic import SecretStr

from full_view_agent.api.app import RuntimeContainer, create_app
from full_view_agent.application.errors import ResourceNotFound
from full_view_agent.application.ports import AgentStore
from full_view_agent.application.session_run_service import SessionRunService
from full_view_agent.domain.models import (
    Evidence,
    PopulationMetricTable,
    RunCreateRequest,
    TableDataResult,
)
from full_view_agent.infrastructure.credential_broker import InMemoryCredentialBroker
from full_view_agent.infrastructure.legacy_identity import HashedLegacyIdentityAdapter
from full_view_agent.infrastructure.memory_store import InMemoryAgentStore
from full_view_agent.infrastructure.postgres_persistence import PostgresAgentPersistence


def _request() -> RunCreateRequest:
    return RunCreateRequest.model_validate(
        {
            "input": {
                "client_message_id": "message-resource-scope",
                "content": [{"type": "text", "text": "查询地址"}],
            },
            "client": {
                "client_instance_id": "client-resource-scope",
                "frontend_command_schema_versions": ["1.1"],
                "supported_commands": ["panel.show_table"],
            },
            "mode": "agent",
        }
    )


def _result(result_id: str) -> TableDataResult:
    return TableDataResult(
        result_id=result_id,
        data_schema_ref="schema://data/population-metric-table/1.0.0",
        result_fingerprint=f"sha256:{result_id}",
        data=PopulationMetricTable(rows=[]),
        row_count=0,
    )


def _evidence(result: TableDataResult, evidence_id: str) -> Evidence:
    now = datetime.now(UTC)
    return Evidence.model_validate(
        {
            "evidence_id": evidence_id,
            "result_id": result.result_id,
            "result_fingerprint": result.result_fingerprint,
            "source_system": "test",
            "dataset_id": "dataset-test",
            "retrieved_at": now,
            "query_fingerprint": "sha256:query",
            "policy_fingerprint": "sha256:policy",
            "tool": {"tool_id": "test.query", "tool_version": "1.0.0"},
            "freshness": {"status": "current"},
        }
    )


async def _seed_resources(
    store: AgentStore,
    *,
    tenant_id: str,
    app_id: str,
    user_id: str,
):
    service = SessionRunService(store)
    session = await service.create_session(
        tenant_id=tenant_id,
        app_id=app_id,
        user_id=user_id,
        title=f"{tenant_id}/{app_id}",
    )
    run = await service.create_run(
        tenant_id=tenant_id,
        app_id=app_id,
        user_id=user_id,
        session_id=session.session_id,
        request=_request(),
    )
    await service.start_run(user_id=user_id, run_id=run.run_id)
    result = _result(f"res-{tenant_id}-{app_id}")
    await store.save_result(user_id=user_id, run_id=run.run_id, result=result)
    evidence = _evidence(result, f"ev-{tenant_id}-{app_id}")
    await store.save_evidence(user_id=user_id, run_id=run.run_id, evidence=evidence)
    return session, run, result, evidence


@pytest.mark.asyncio
async def test_memory_data_resources_fail_closed_across_tenant_and_application() -> None:
    store = InMemoryAgentStore()
    address = await _seed_resources(
        store,
        tenant_id="tenant-a",
        app_id="unified_address",
        user_id="shared-user",
    )
    other_tenant = await _seed_resources(
        store,
        tenant_id="tenant-b",
        app_id="full_information_view",
        user_id="shared-user",
    )

    for _session, run, result, evidence in (address, other_tenant):
        with pytest.raises(ResourceNotFound):
            await store.get_run(
                tenant_id="tenant-a",
                app_id="full_information_view",
                user_id="shared-user",
                run_id=run.run_id,
            )
        with pytest.raises(ResourceNotFound):
            await store.get_result(
                tenant_id="tenant-a",
                app_id="full_information_view",
                user_id="shared-user",
                result_id=result.result_id,
            )
        with pytest.raises(ResourceNotFound):
            await store.get_result_for_run(
                tenant_id="tenant-a",
                app_id="full_information_view",
                user_id="shared-user",
                run_id=run.run_id,
                result_id=result.result_id,
            )
        with pytest.raises(ResourceNotFound):
            await store.get_evidence(
                tenant_id="tenant-a",
                app_id="full_information_view",
                user_id="shared-user",
                evidence_id=evidence.evidence_id,
            )
        with pytest.raises(ResourceNotFound):
            await store.get_evidence_for_run(
                tenant_id="tenant-a",
                app_id="full_information_view",
                user_id="shared-user",
                run_id=run.run_id,
                evidence_id=evidence.evidence_id,
            )


@pytest.mark.asyncio
async def test_full_view_data_api_ignores_untrusted_application_hints() -> None:
    runtime = RuntimeContainer(
        identity_port=HashedLegacyIdentityAdapter(),
        credentials=InMemoryCredentialBroker(),
    )
    token = "same-user-resource-token"
    identity = await runtime.identity_port.resolve(SecretStr(token))
    assert runtime.store is not None
    session, run, result, evidence = await _seed_resources(
        runtime.store,
        tenant_id=identity.principal.tenant_id,
        app_id="unified_address",
        user_id=identity.principal.user_id,
    )
    app = create_app(runtime)
    headers = {"geoToken": token, "X-Application-Id": "unified_address"}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        responses = [
            await client.get(f"/agent-api/v1/runs/{run.run_id}", headers=headers),
            await client.get(
                f"/agent-api/v1/sessions/{session.session_id}/messages", headers=headers
            ),
            await client.get(
                f"/agent-api/v1/results/{result.result_id}", headers=headers
            ),
            await client.get(
                f"/agent-api/v1/results/{result.result_id}/items", headers=headers
            ),
            await client.get(
                f"/agent-api/v1/evidence/{evidence.evidence_id}", headers=headers
            ),
        ]

    assert [response.status_code for response in responses] == [404] * len(responses)


@pytest.mark.asyncio
async def test_full_view_mutations_cannot_target_another_application_run() -> None:
    runtime = RuntimeContainer(
        identity_port=HashedLegacyIdentityAdapter(),
        credentials=InMemoryCredentialBroker(),
    )
    token = "same-user-mutation-token"
    identity = await runtime.identity_port.resolve(SecretStr(token))
    assert runtime.store is not None
    _session, run, _result_value, _evidence_value = await _seed_resources(
        runtime.store,
        tenant_id=identity.principal.tenant_id,
        app_id="unified_address",
        user_id=identity.principal.user_id,
    )
    app = create_app(runtime)
    headers = {"geoToken": token, "X-Application-Id": "unified_address"}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        cancelled = await client.post(
            f"/agent-api/v1/runs/{run.run_id}/cancel", headers=headers
        )
        steered = await client.post(
            f"/agent-api/v1/runs/{run.run_id}/steers",
            headers={**headers, "Idempotency-Key": "cross-app-steer"},
            json={
                "client_instance_id": "client-resource-scope",
                "content": "越权修改",
            },
        )

    assert cancelled.status_code == 404
    assert steered.status_code == 404
    stored = await runtime.store.get_run(
        user_id=identity.principal.user_id,
        run_id=run.run_id,
    )
    assert stored.status == "running"


@pytest.mark.db
@pytest.mark.asyncio
async def test_postgres_data_resources_fail_closed_across_application(pg_schema) -> None:
    store = PostgresAgentPersistence(
        dsn=pg_schema["dsn"],
        schema=pg_schema["schema"],
    )
    service = SessionRunService(store)
    session = await service.create_session(
        tenant_id="tenant-a",
        app_id="unified_address",
        user_id="shared-user",
        title="统一地址",
    )
    run = await service.create_run(
        tenant_id="tenant-a",
        app_id="unified_address",
        user_id="shared-user",
        session_id=session.session_id,
        request=_request(),
    )
    await service.start_run(user_id="shared-user", run_id=run.run_id)
    result = _result("res-postgres-cross-app")
    await store.save_result(user_id="shared-user", run_id=run.run_id, result=result)
    evidence = _evidence(result, "ev-postgres-cross-app")
    await store.save_evidence(
        user_id="shared-user", run_id=run.run_id, evidence=evidence
    )

    with pytest.raises(ResourceNotFound):
        await store.list_messages(
            tenant_id="tenant-a",
            app_id="full_information_view",
            user_id="shared-user",
            session_id=session.session_id,
        )
    with pytest.raises(ResourceNotFound):
        await store.get_run(
            tenant_id="tenant-a",
            app_id="full_information_view",
            user_id="shared-user",
            run_id=run.run_id,
        )
    with pytest.raises(ResourceNotFound):
        await store.get_result(
            tenant_id="tenant-a",
            app_id="full_information_view",
            user_id="shared-user",
            result_id=result.result_id,
        )
    with pytest.raises(ResourceNotFound):
        await store.get_result_for_run(
            tenant_id="tenant-a",
            app_id="full_information_view",
            user_id="shared-user",
            run_id=run.run_id,
            result_id=result.result_id,
        )
    with pytest.raises(ResourceNotFound):
        await store.get_evidence(
            tenant_id="tenant-a",
            app_id="full_information_view",
            user_id="shared-user",
            evidence_id=evidence.evidence_id,
        )
    with pytest.raises(ResourceNotFound):
        await store.get_evidence_for_run(
            tenant_id="tenant-a",
            app_id="full_information_view",
            user_id="shared-user",
            run_id=run.run_id,
            evidence_id=evidence.evidence_id,
        )
