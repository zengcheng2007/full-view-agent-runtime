from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from full_view_agent.application.errors import ResourceNotFound, RunStateConflict
from full_view_agent.domain.application import (
    AgentApplicationDefinition,
    ApplicationCapabilityBinding,
)
from full_view_agent.domain.capability import ToolCapability
from full_view_agent.infrastructure.application_registry import (
    InMemoryApplicationRegistry,
    default_application_registry,
)
from full_view_agent.infrastructure.auth_context_store import (
    InMemoryRunAuthContextStore,
)
from full_view_agent.infrastructure.capability_repository import (
    InMemoryCapabilityRepository,
)
from full_view_agent.infrastructure.credential_broker import InMemoryCredentialBroker
from full_view_agent.infrastructure.legacy_identity import HashedLegacyIdentityAdapter
from full_view_agent.infrastructure.memory_store import InMemoryAgentStore


def _application(
    *,
    app_id: str = "full_information_view",
    agent_id: str = "governance_general_agent",
) -> AgentApplicationDefinition:
    return AgentApplicationDefinition(
        app_id=app_id,
        name=app_id,
        default_agent_id=agent_id,
        identity_adapter_id=f"identity.{app_id}",
    )


@pytest.mark.asyncio
async def test_registry_keeps_full_view_and_unified_address_as_peer_apps() -> None:
    registry = InMemoryApplicationRegistry()

    await registry.save_application(_application())
    await registry.save_application(
        _application(
            app_id="unified_address",
            agent_id="unified_address_agent",
        )
    )

    applications = await registry.list_applications(active_only=True)

    assert [item.app_id for item in applications] == [
        "full_information_view",
        "unified_address",
    ]
    assert applications[1].default_agent_id == "unified_address_agent"


@pytest.mark.asyncio
async def test_default_registry_seeds_full_view_and_reserves_unified_address() -> None:
    registry = default_application_registry(dsn=None)

    full_view = await registry.get_application("full_information_view")
    address = await registry.get_application("unified_address")

    assert full_view is not None and full_view.status == "active"
    assert address is not None and address.status == "disabled"
    full_view_tools = {
        binding.capability_id
        for binding in await registry.list_capability_bindings(
            app_id="full_information_view",
            enabled_only=True,
        )
    }
    assert "governance.get_governance_overview" in full_view_tools
    assert "governance.query_enterprise_metrics" in full_view_tools


@pytest.mark.asyncio
async def test_capability_binding_is_scoped_to_one_application() -> None:
    registry = InMemoryApplicationRegistry()
    await registry.save_application(_application())
    await registry.save_application(
        _application(
            app_id="unified_address",
            agent_id="unified_address_agent",
        )
    )
    await registry.bind_capability(
        ApplicationCapabilityBinding(
            app_id="full_information_view",
            capability_id="governance.resolve_area",
            capability_version="1.0.0",
        )
    )

    assert [
        binding.capability_id
        for binding in await registry.list_capability_bindings(
            app_id="full_information_view",
            enabled_only=True,
        )
    ] == ["governance.resolve_area"]
    assert await registry.list_capability_bindings(
        app_id="unified_address",
        enabled_only=True,
    ) == []


@pytest.mark.asyncio
async def test_binding_unknown_application_fails_closed() -> None:
    registry = InMemoryApplicationRegistry()

    with pytest.raises(ResourceNotFound, match="application is not registered"):
        await registry.bind_capability(
            ApplicationCapabilityBinding(
                app_id="missing_app",
                capability_id="governance.resolve_area",
                capability_version="1.0.0",
            )
        )


@pytest.mark.asyncio
async def test_run_admission_uses_registered_application_instead_of_hardcoded_app() -> None:
    from full_view_agent.application.run_admission import RunAdmissionService

    registry = InMemoryApplicationRegistry()
    await registry.save_application(
        _application(
            app_id="unified_address",
            agent_id="unified_address_agent",
        )
    )
    raw_token = SecretStr("unified-address-user-token")
    identity = await HashedLegacyIdentityAdapter().resolve(raw_token)
    admission = RunAdmissionService(
        credential_broker=InMemoryCredentialBroker(),
        auth_context_store=InMemoryRunAuthContextStore(),
        application_registry=registry,
    )

    auth_context = await admission.admit(
        identity=identity,
        raw_token=raw_token,
        session_id="session-address",
        run_id="run-address",
        app_id="unified_address",
    )

    assert auth_context.application.app_id == "unified_address"
    assert auth_context.application.agent_id == "unified_address_agent"


@pytest.mark.asyncio
async def test_auth_context_refresh_preserves_the_original_application() -> None:
    from full_view_agent.application.auth_context_refresh import (
        RunAuthContextRefresher,
    )
    from full_view_agent.application.run_admission import RunAdmissionService

    registry = InMemoryApplicationRegistry()
    await registry.save_application(
        _application(
            app_id="unified_address",
            agent_id="unified_address_agent",
        )
    )
    raw_token = SecretStr("unified-address-refresh-token")
    identity_port = HashedLegacyIdentityAdapter()
    identity = await identity_port.resolve(raw_token)
    broker = InMemoryCredentialBroker()
    admission = RunAdmissionService(
        credential_broker=broker,
        auth_context_store=InMemoryRunAuthContextStore(),
        application_registry=registry,
    )
    original = await admission.admit(
        identity=identity,
        raw_token=raw_token,
        session_id="session-address-refresh",
        run_id="run-address-refresh",
        app_id="unified_address",
    )
    refresher = RunAuthContextRefresher(
        credential_broker=broker,
        identity_port=identity_port,
        admission=admission,
    )

    refreshed = await refresher.refresh(original)

    assert refreshed.application.app_id == "unified_address"
    assert refreshed.application.agent_id == "unified_address_agent"


@pytest.mark.asyncio
async def test_run_admission_rejects_unknown_or_disabled_application() -> None:
    from full_view_agent.application.run_admission import RunAdmissionService

    registry = InMemoryApplicationRegistry()
    await registry.save_application(
        _application(app_id="disabled_app").model_copy(update={"status": "disabled"})
    )
    raw_token = SecretStr("unknown-app-user-token")
    identity = await HashedLegacyIdentityAdapter().resolve(raw_token)
    admission = RunAdmissionService(
        credential_broker=InMemoryCredentialBroker(),
        auth_context_store=InMemoryRunAuthContextStore(),
        application_registry=registry,
    )

    for app_id in ("missing_app", "disabled_app"):
        with pytest.raises(ResourceNotFound, match="application is not available"):
            await admission.admit(
                identity=identity,
                raw_token=raw_token,
                session_id=f"session-{app_id}",
                run_id=f"run-{app_id}",
                app_id=app_id,
            )


def test_application_ids_are_controlled_identifiers() -> None:
    with pytest.raises(ValidationError):
        _application(app_id="../../full-view")


def test_v013_migration_defines_applications_and_scoped_bindings() -> None:
    migration = (
        Path(__file__).parents[1]
        / "scripts/migrations/V013_application_registry.sql"
    ).read_text(encoding="utf-8")

    assert "agent_applications" in migration
    assert "application_capability_bindings" in migration
    assert "PRIMARY KEY (app_id, capability_id, capability_version)" in migration
    assert "REFERENCES full_view_agent.agent_applications(app_id)" in migration
    assert "SELECT 13" in migration


@pytest.mark.asyncio
async def test_postgres_registry_persists_applications_and_bindings(pg_schema) -> None:
    from full_view_agent.infrastructure.application_registry import (
        PostgresApplicationRegistry,
    )

    first = PostgresApplicationRegistry(
        dsn=pg_schema["dsn"],
        schema=pg_schema["schema"],
    )
    await first.save_application(_application())
    await first.bind_capability(
        ApplicationCapabilityBinding(
            app_id="full_information_view",
            capability_id="governance.resolve_area",
            capability_version="1.0.0",
        )
    )

    reopened = PostgresApplicationRegistry(
        dsn=pg_schema["dsn"],
        schema=pg_schema["schema"],
    )
    assert (await reopened.get_application("full_information_view")) is not None
    bindings = await reopened.list_capability_bindings(
        app_id="full_information_view",
        enabled_only=True,
    )
    assert ("governance.resolve_area", "1.0.0") in {
        (item.capability_id, item.capability_version) for item in bindings
    }


@pytest.mark.asyncio
async def test_session_is_created_inside_the_selected_application() -> None:
    from full_view_agent.application.session_run_service import SessionRunService

    service = SessionRunService(InMemoryAgentStore())

    session = await service.create_session(
        user_id="user-address",
        app_id="unified_address",
        title="地址治理",
    )

    assert session.app_id == "unified_address"


@pytest.mark.asyncio
async def test_full_view_identity_cannot_run_a_unified_address_session() -> None:
    from full_view_agent.api.app import RuntimeContainer, create_app

    registry = InMemoryApplicationRegistry()
    await registry.save_application(_application())
    await registry.save_application(
        _application(
            app_id="unified_address",
            agent_id="unified_address_agent",
        )
    )
    token = "address-session-run-token"
    identity_port = HashedLegacyIdentityAdapter()
    identity = await identity_port.resolve(SecretStr(token))
    runtime = RuntimeContainer(
        identity_port=identity_port,
        credentials=InMemoryCredentialBroker(),
        application_registry=registry,
    )
    session = await runtime.service.create_session(
        tenant_id=identity.principal.tenant_id,
        user_id=identity.principal.user_id,
        app_id="unified_address",
        title="地址治理",
    )
    app = create_app(runtime)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            f"/agent-api/v1/sessions/{session.session_id}/runs",
            headers={"geoToken": token, "Idempotency-Key": "address-session-run"},
            json={
                "input": {
                    "client_message_id": "address-message",
                    "content": [{"type": "text", "text": "查询地址"}],
                },
                "client": {
                    "client_instance_id": "address-web",
                    "frontend_command_schema_versions": ["1.1"],
                    "supported_commands": [],
                },
                "mode": "agent",
            },
        )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "resource_not_found"


@pytest.mark.asyncio
async def test_management_service_binds_only_published_capabilities() -> None:
    from full_view_agent.application.application_management_service import (
        ApplicationManagementService,
    )

    registry = InMemoryApplicationRegistry()
    capabilities = InMemoryCapabilityRepository()
    service = ApplicationManagementService(
        application_registry=registry,
        capability_repository=capabilities,
    )
    await registry.save_application(_application())
    draft = ToolCapability(
        capability_id="governance.address_lookup",
        name="Address lookup",
        owner="platform",
        version="1.0.0",
        status="draft",
        connector_ref="connector.address",
        resource_path="/v1/address/lookup",
    )
    await capabilities.save_tool(draft)

    with pytest.raises(RunStateConflict, match="published capability"):
        await service.bind_capability(
            app_id="full_information_view",
            capability_id=draft.capability_id,
            capability_version=draft.version,
        )

    await capabilities.save_tool(
        draft.model_copy(update={"status": "published", "guidance": "Test guidance for address_lookup", "etag": 2})
    )
    binding = await service.bind_capability(
        app_id="full_information_view",
        capability_id=draft.capability_id,
        capability_version=draft.version,
    )
    assert binding.app_id == "full_information_view"


@pytest.mark.asyncio
async def test_run_snapshot_discovers_only_capabilities_bound_to_its_application() -> None:
    from full_view_agent.application.run_capability_snapshot import (
        RunCapabilitySnapshotService,
    )
    from full_view_agent.application.tool_registry import ToolRegistry

    registry = InMemoryApplicationRegistry()
    capabilities = InMemoryCapabilityRepository()
    await registry.save_application(_application())
    await registry.save_application(
        _application(
            app_id="unified_address",
            agent_id="unified_address_agent",
        )
    )
    for tool_id in ("tool.full_view_only", "tool.address_only"):
        await capabilities.save_tool(
            ToolCapability(
                capability_id=tool_id,
                name=tool_id,
                owner="platform",
                version="1.0.0",
                status="published",
                guidance=f"Test guidance for {tool_id}",
                connector_ref="connector.shared",
                resource_path=f"/v1/{tool_id.replace('.', '/')}",
            )
        )
    await registry.bind_capability(
        ApplicationCapabilityBinding(
            app_id="full_information_view",
            capability_id="tool.full_view_only",
            capability_version="1.0.0",
        )
    )
    await registry.bind_capability(
        ApplicationCapabilityBinding(
            app_id="unified_address",
            capability_id="tool.address_only",
            capability_version="1.0.0",
        )
    )
    service = RunCapabilitySnapshotService(
        repository=capabilities,
        application_registry=registry,
    )

    snapshot = await service.create_snapshot_for_run(
        "run-full-view",
        ToolRegistry.default(),
        app_id="full_information_view",
    )

    assert snapshot.tool_versions == {"tool.full_view_only": "1.0.0"}
