import httpx
import pytest
from pydantic import SecretStr

from full_view_agent.api.app import RuntimeContainer, create_app
from full_view_agent.application.errors import ResourceNotFound
from full_view_agent.application.session_run_service import SessionRunService
from full_view_agent.domain.models import RunCreateRequest
from full_view_agent.infrastructure.credential_broker import InMemoryCredentialBroker
from full_view_agent.infrastructure.legacy_identity import HashedLegacyIdentityAdapter
from full_view_agent.infrastructure.memory_store import InMemoryAgentStore


def _run_request() -> RunCreateRequest:
    return RunCreateRequest.model_validate(
        {
            "input": {
                "client_message_id": "message-cross-app",
                "content": [{"type": "text", "text": "查询地址"}],
            },
            "client": {
                "client_instance_id": "client-cross-app",
                "frontend_command_schema_versions": ["1.1"],
                "supported_commands": ["panel.show_table"],
            },
            "mode": "agent",
        }
    )


@pytest.mark.asyncio
async def test_session_service_isolates_same_user_by_tenant_and_application() -> None:
    service = SessionRunService(InMemoryAgentStore())
    full_view = await service.create_session(
        tenant_id="tenant-a",
        user_id="shared-user",
        app_id="full_information_view",
        title="全息图会话",
    )
    address = await service.create_session(
        tenant_id="tenant-a",
        user_id="shared-user",
        app_id="unified_address",
        title="统一地址会话",
    )
    other_tenant = await service.create_session(
        tenant_id="tenant-b",
        user_id="shared-user",
        app_id="full_information_view",
        title="其他租户会话",
    )

    assert await service.list_sessions(
        tenant_id="tenant-a",
        user_id="shared-user",
        app_id="full_information_view",
    ) == [full_view]
    assert await service.list_sessions(
        tenant_id="tenant-a",
        user_id="shared-user",
        app_id="unified_address",
    ) == [address]

    for hidden in (address, other_tenant):
        with pytest.raises(ResourceNotFound):
            await service.get_session(
                tenant_id="tenant-a",
                user_id="shared-user",
                app_id="full_information_view",
                session_id=hidden.session_id,
            )
        with pytest.raises(ResourceNotFound):
            await service.update_session(
                tenant_id="tenant-a",
                user_id="shared-user",
                app_id="full_information_view",
                session_id=hidden.session_id,
                title="越权修改",
            )
        with pytest.raises(ResourceNotFound):
            await service.create_run(
                tenant_id="tenant-a",
                user_id="shared-user",
                app_id="full_information_view",
                session_id=hidden.session_id,
                request=_run_request(),
            )


@pytest.mark.asyncio
async def test_full_view_session_api_ignores_untrusted_application_hints() -> None:
    runtime = RuntimeContainer(
        identity_port=HashedLegacyIdentityAdapter(),
        credentials=InMemoryCredentialBroker(),
    )
    token = "same-user-token"
    identity = await runtime.identity_port.resolve(SecretStr(token))
    address = await runtime.service.create_session(
        tenant_id=identity.principal.tenant_id,
        user_id=identity.principal.user_id,
        app_id="unified_address",
        title="统一地址私有会话",
    )
    app = create_app(runtime)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        headers = {
            "geoToken": token,
            "X-Application-Id": "unified_address",
        }
        listed = await client.get(
            "/agent-api/v1/sessions?app_id=unified_address",
            headers=headers,
        )
        detail = await client.get(
            f"/agent-api/v1/sessions/{address.session_id}",
            headers=headers,
        )
        updated = await client.patch(
            f"/agent-api/v1/sessions/{address.session_id}",
            headers={**headers, "Idempotency-Key": "cross-app-update"},
            json={"title": "不应修改"},
        )
        run = await client.post(
            f"/agent-api/v1/sessions/{address.session_id}/runs",
            headers={**headers, "Idempotency-Key": "cross-app-run"},
            json=_run_request().model_dump(mode="json"),
        )

    assert listed.status_code == 200
    assert listed.json()["data"] == []
    assert detail.status_code == 404
    assert updated.status_code == 404
    assert run.status_code == 404
