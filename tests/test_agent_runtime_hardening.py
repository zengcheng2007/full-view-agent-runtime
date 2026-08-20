from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import cast
from unittest.mock import AsyncMock

import httpx
import pytest

from full_view_agent.api.app import RuntimeContainer, create_app
from full_view_agent.application.agent_management_service import AgentManagementService
from full_view_agent.application.errors import ResourceNotFound, RunStateConflict
from full_view_agent.application.model_config_service import (
    EncryptedModelConfigKeyStore,
    InMemoryModelConfigKeyStore,
    ModelConfigService,
)
from full_view_agent.domain.agent_definition import (
    AgentDefinition,
    AgentModelPolicy,
    AgentVersion,
    RunAgentReleaseSnapshot,
)
from full_view_agent.domain.application import (
    AgentApplicationDefinition,
    ApplicationCapabilityBinding,
)
from full_view_agent.domain.capability import (
    SkillCapability,
    ToolCapability,
    WorkflowCapability,
    WorkflowNodeDefinition,
)
from full_view_agent.domain.models import LegacyIdentitySnapshot, Principal
from full_view_agent.infrastructure.agent_repository import InMemoryAgentRepository
from full_view_agent.infrastructure.application_registry import InMemoryApplicationRegistry
from full_view_agent.infrastructure.capability_repository import (
    InMemoryCapabilityRepository,
    InMemoryModelConfigRepository,
)
from full_view_agent.infrastructure.credential_broker import InMemoryCredentialBroker
from full_view_agent.infrastructure.legacy_identity import HashedLegacyIdentityAdapter
from full_view_agent.infrastructure.memory_store import InMemoryAgentStore
from tests.model_resource_helpers import publish_tested_model

APP_ID = "full_information_view"


def _run_body(message_id: str) -> dict[str, object]:
    return {
        "input": {
            "client_message_id": message_id,
            "content": [{"type": "text", "text": "测试 Agent 绑定"}],
        },
        "client": {
            "client_instance_id": "hardening-client",
            "frontend_command_schema_versions": ["1.1"],
            "supported_commands": ["panel.show_table"],
        },
        "mode": "agent",
    }


async def _management_service() -> tuple[
    AgentManagementService,
    InMemoryApplicationRegistry,
    InMemoryModelConfigRepository,
    InMemoryCapabilityRepository,
]:
    applications = InMemoryApplicationRegistry(
        applications=[
            AgentApplicationDefinition(
                app_id=APP_ID,
                name="全量信息视图",
                default_agent_id="governance_general_agent",
                identity_adapter_id="identity.legacy_geo",
            )
        ]
    )
    models = InMemoryModelConfigRepository()
    capabilities = InMemoryCapabilityRepository()
    service = AgentManagementService(
        repository=InMemoryAgentRepository(),
        application_registry=applications,
        model_config_service=ModelConfigService(
            repository=models,
            key_store=InMemoryModelConfigKeyStore(),
        ),
        capability_repository=capabilities,
    )
    return service, applications, models, capabilities


async def _create_ready_agent(
    service: AgentManagementService,
    models: InMemoryModelConfigRepository,
    *,
    agent_id: str = "analysis_agent",
    version: AgentVersion | None = None,
) -> AgentVersion:
    del models
    model = await publish_tested_model(
        service._models,  # type: ignore[attr-defined]
        name=f"主模型-{agent_id}",
        model_name="model-primary",
    )
    await service.create_agent(
        AgentDefinition(app_id=APP_ID, agent_id=agent_id, name="研判智能体")
    )
    item = version or AgentVersion(
        app_id=APP_ID,
        agent_id=agent_id,
        version="1.0.0",
    )
    await service.create_version(item)
    await service.set_model_policy(
        app_id=APP_ID,
        agent_id=agent_id,
        version=item.version,
        policy=AgentModelPolicy(primary_model_config_id=model.config_id),
    )
    return item


@pytest.mark.asyncio
async def test_release_keeps_model_version_available_before_first_run() -> None:
    """A release must remain runnable when its model is edited before its first Run."""
    models = InMemoryModelConfigRepository()
    keys = EncryptedModelConfigKeyStore(encryption_key=b"1" * 32)
    model_service = ModelConfigService(repository=models, key_store=keys)
    created = await publish_tested_model(
        model_service,
        name="主模型",
        model_name="model-v1",
    )
    released_version = created.version

    await model_service.create_version(
        config_id=created.config_id,
        expected_etag=created.etag,
        actor="admin",
        reason="prepare v2 without mutating the published v1",
        changes={"model_name": "model-v2"},
    )

    frozen = await model_service.capture_snapshot_by_id(
        created.config_id, released_version
    )
    assert frozen is not None
    assert frozen.config_version == released_version
    assert frozen.model_name == "model-v1"
    assert model_service.materialise_snapshot(frozen).api_key_secret == "key-主模型"


@pytest.mark.asyncio
async def test_unpublished_agent_cannot_be_application_default() -> None:
    service, applications, models, _ = await _management_service()
    await _create_ready_agent(service, models, agent_id="unpublished_agent")

    with pytest.raises(RunStateConflict, match="active release"):
        await service.set_default_agent(
            app_id=APP_ID,
            agent_id="unpublished_agent",
            expected_application_etag=1,
            changed_by="admin",
            reason="must not widen to legacy application grants",
        )

    application = await applications.get_application(APP_ID)
    assert application is not None
    assert application.default_agent_id == "governance_general_agent"


@pytest.mark.asyncio
async def test_agent_validation_rejects_duplicate_resource_references() -> None:
    service, applications, models, capabilities = await _management_service()
    duplicate = AgentVersion(
        app_id=APP_ID,
        agent_id="duplicate_agent",
        version="1.0.0",
        capability_refs=(
            "governance.resolve_area@1.0.0",
            "governance.resolve_area@1.0.0",
        ),
    )
    await _create_ready_agent(
        service, models, agent_id="duplicate_agent", version=duplicate
    )
    await capabilities.save_tool(
        ToolCapability(
            capability_id="governance.resolve_area",
            name="解析区划",
            owner="platform",
            version="1.0.0",
            status="published",
            guidance="Test guidance for governance.resolve_area",
            connector_ref="governance",
            resource_path="/area/resolve",
        )
    )
    await applications.bind_capability(
        ApplicationCapabilityBinding(
            app_id=APP_ID,
            capability_id="governance.resolve_area",
            capability_version="1.0.0",
        )
    )

    report = await service.validate_version(
        app_id=APP_ID, agent_id="duplicate_agent", version="1.0.0"
    )

    assert "DUPLICATE_RESOURCE_REFERENCE" in {issue.code for issue in report.issues}


@pytest.mark.asyncio
@pytest.mark.parametrize("resource_kind", ["skill", "workflow"])
async def test_agent_validation_requires_transitive_tool_dependencies(
    resource_kind: str,
) -> None:
    service, applications, models, capabilities = await _management_service()
    resource_id = f"{resource_kind}.analysis"
    version = AgentVersion(
        app_id=APP_ID,
        agent_id=f"{resource_kind}_agent",
        version="1.0.0",
        skill_refs=(f"{resource_id}@1.0.0",) if resource_kind == "skill" else (),
        workflow_refs=(f"{resource_id}@1.0.0",) if resource_kind == "workflow" else (),
        # Deliberately omits governance.resolve_area@1.0.0.
        capability_refs=(),
    )
    await _create_ready_agent(
        service, models, agent_id=version.agent_id, version=version
    )
    if resource_kind == "skill":
        await capabilities.save_skill(
            SkillCapability(
                capability_id=resource_id,
                name="区域研判技能",
                owner="platform",
                version="1.0.0",
                status="published",
                guidance="Test skill guidance",
                allowed_tool_ids=["governance.resolve_area"],
            )
        )
    else:
        await capabilities.save_workflow(
            WorkflowCapability(
                capability_id=resource_id,
                name="区域研判工作流",
                owner="platform",
                version="1.0.0",
                status="published",
                guidance="Test workflow guidance",
                nodes=[
                    WorkflowNodeDefinition(
                        node_id="resolve",
                        node_type="tool",
                        tool_capability_id="governance.resolve_area",
                        tool_version="1.0.0",
                    )
                ],
            )
        )
    await applications.bind_capability(
        ApplicationCapabilityBinding(
            app_id=APP_ID,
            capability_id=resource_id,
            capability_version="1.0.0",
        )
    )

    report = await service.validate_version(
        app_id=APP_ID, agent_id=version.agent_id, version=version.version
    )

    assert "DEPENDENCY_TOOL_NOT_REFERENCED" in {
        issue.code for issue in report.issues
    }


@pytest.mark.asyncio
async def test_legacy_model_resolution_rejects_ambiguous_active_models() -> None:
    """Legacy execution must not pick an arbitrary row when several models are active."""
    repository = InMemoryModelConfigRepository()
    keys = InMemoryModelConfigKeyStore()
    service = ModelConfigService(
        repository=repository,
        key_store=keys,
    )
    for suffix in ("a", "b"):
        await publish_tested_model(
            service,
            name=f"模型 {suffix}",
            model_name=f"model-{suffix}",
        )

    with pytest.raises(RunStateConflict, match="explicit .*default"):
        await service.resolve_for_runtime()

    with pytest.raises(RunStateConflict, match="explicit .*default"):
        await service.capture_snapshot_for_runtime()


@pytest.mark.asyncio
async def test_disabled_model_is_not_available_to_a_new_run_snapshot() -> None:
    repository = InMemoryModelConfigRepository()
    keys = EncryptedModelConfigKeyStore(encryption_key=b"2" * 32)
    service = ModelConfigService(repository=repository, key_store=keys)
    created = await publish_tested_model(
        service,
        name="将停用模型",
        model_name="disabled-model",
    )
    version = created.version
    await service.disable_config(
        config_id=created.config_id,
        expected_etag=created.etag,
        actor="admin",
        reason="verify new Runs cannot select disabled resources",
    )

    assert await service.capture_snapshot_by_id(created.config_id, version) is None


@pytest.mark.asyncio
async def test_create_run_passes_admitted_agent_id_to_release_binding() -> None:
    """Admission and release binding must share one resolved default-Agent value."""
    runtime = RuntimeContainer(
        identity_port=HashedLegacyIdentityAdapter(),
        credentials=InMemoryCredentialBroker(),
    )
    await runtime.initialize()
    assert runtime.agent_management_service is not None
    original_bind = runtime.agent_management_service.bind_run
    runtime.agent_management_service.bind_run = AsyncMock(wraps=original_bind)  # type: ignore[method-assign]
    app = create_app(runtime)
    headers = {"geoToken": "single-resolution-user"}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        session_response = await client.post(
            "/agent-api/v1/sessions",
            headers={**headers, "Idempotency-Key": "hardening-session-agent"},
            json={"title": "单次 Agent 解析"},
        )
        session_id = session_response.json()["data"]["session_id"]
        run_response = await client.post(
            f"/agent-api/v1/sessions/{session_id}/runs",
            headers={**headers, "Idempotency-Key": "hardening-run-agent"},
            json=_run_body("hardening-message-agent"),
        )

    assert run_response.status_code == 202, run_response.text
    call = runtime.agent_management_service.bind_run.await_args  # type: ignore[union-attr]
    assert call is not None
    assert call.kwargs.get("agent_id") == "governance_general_agent"


@pytest.mark.asyncio
async def test_create_run_snapshot_failure_revokes_admission_artifacts() -> None:
    credentials = InMemoryCredentialBroker()
    runtime = RuntimeContainer(
        identity_port=HashedLegacyIdentityAdapter(),
        credentials=credentials,
    )
    await runtime.initialize()

    class _FailingSnapshotService:
        async def create_snapshot_for_run(self, **_kwargs):
            raise RuntimeError("injected resource snapshot failure")

    runtime.run_capability_snapshot_service = _FailingSnapshotService()  # type: ignore[assignment]
    app = create_app(runtime)
    headers = {"geoToken": "compensation-user"}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        session_response = await client.post(
            "/agent-api/v1/sessions",
            headers={**headers, "Idempotency-Key": "hardening-session-compensation"},
            json={"title": "失败补偿"},
        )
        session_id = session_response.json()["data"]["session_id"]
        run_response = await client.post(
            f"/agent-api/v1/sessions/{session_id}/runs",
            headers={**headers, "Idempotency-Key": "hardening-run-compensation"},
            json=_run_body("hardening-message-compensation"),
        )

    assert run_response.status_code >= 400
    store = cast(InMemoryAgentStore, runtime.store)
    created_run = next(iter(store.runs.values()))
    assert created_run.status == "failed"
    assert credentials._credentials == {}
    assert runtime.auth_contexts is not None
    with pytest.raises(ResourceNotFound):
        await runtime.auth_contexts.get(
            user_id=store.sessions[session_id].owner_user_id,
            run_id=created_run.run_id,
        )


@pytest.mark.asyncio
async def test_run_release_snapshot_rejects_cross_tenant_admin() -> None:
    identity = AsyncMock()
    identity.resolve = AsyncMock(
        return_value=LegacyIdentitySnapshot(
            principal=Principal(
                tenant_id="tenant-beta",
                user_id="beta-admin",
                org_id="beta-admins",
                roles=["admin"],
            ),
            source="hardening-test",
            source_session_expires_at=datetime.now(UTC) + timedelta(hours=1),
            base_area_codes=[],
        )
    )
    runtime = RuntimeContainer(identity_port=identity)
    runtime.capability_identity_port = identity
    await runtime.initialize()
    assert runtime.agent_repository is not None
    await runtime.agent_repository.bind_run(
        RunAgentReleaseSnapshot(
            run_id="run-owned-by-alpha",
            release_id="release-alpha",
            app_id=APP_ID,
            agent_id="governance_general_agent",
            agent_version="1.0.0",
            model_refs=(),
            published_by="alpha-admin",
            reason="tenant alpha release",
        )
    )
    app = create_app(runtime)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(
            "/capability-api/v1/runs/run-owned-by-alpha/agent-release-snapshot",
            headers={"geoToken": "beta-token"},
        )

    # The unscoped endpoint is intentionally not exposed. Callers must address
    # a Run through its owning application so same-tenant cross-app reads are
    # impossible as well.
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_run_release_snapshot_is_scoped_by_application_path() -> None:
    """Same-tenant admins must address a Run through its owning application."""
    identity = AsyncMock()
    identity.resolve = AsyncMock(
        return_value=LegacyIdentitySnapshot(
            principal=Principal(
                tenant_id="tenant-alpha",
                user_id="alpha-admin",
                org_id="alpha-admins",
                roles=["admin"],
            ),
            source="hardening-test",
            source_session_expires_at=datetime.now(UTC) + timedelta(hours=1),
            base_area_codes=[],
        )
    )
    runtime = RuntimeContainer(identity_port=identity)
    runtime.capability_identity_port = identity
    await runtime.initialize()
    assert runtime.agent_repository is not None
    await runtime.agent_repository.bind_run(
        RunAgentReleaseSnapshot(
            run_id="run-full-view-alpha",
            release_id="release-full-view-alpha",
            app_id=APP_ID,
            agent_id="governance_general_agent",
            agent_version="1.0.0",
            model_refs=(),
            tenant_id="tenant-alpha",
            published_by="alpha-admin",
            reason="application-scoped release",
        )
    )
    app = create_app(runtime)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        matching = await client.get(
            "/capability-api/v1/applications/full_information_view/"
            "runs/run-full-view-alpha/agent-release-snapshot",
            headers={"geoToken": "alpha-token"},
        )
        cross_app = await client.get(
            "/capability-api/v1/applications/unified_address/"
            "runs/run-full-view-alpha/agent-release-snapshot",
            headers={"geoToken": "alpha-token"},
        )
        legacy_unscoped = await client.get(
            "/capability-api/v1/runs/run-full-view-alpha/agent-release-snapshot",
            headers={"geoToken": "alpha-token"},
        )

    assert matching.status_code == 200, matching.text
    assert cross_app.status_code in {403, 404}, cross_app.text
    assert legacy_unscoped.status_code == 404, legacy_unscoped.text
