from __future__ import annotations

import asyncio
import threading
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from full_view_agent.api.app import RuntimeContainer, create_app
from full_view_agent.application.dynamic_skill_workflow_bridge import (
    PublishedRuntimeCapabilityLoader,
    build_runtime_skill_contract,
    build_runtime_workflow_snapshot,
)
from full_view_agent.application.dynamic_tool_bridge import (
    build_dynamic_input_schemas,
    build_dynamic_tool_registry_entries,
)
from full_view_agent.application.errors import WorkflowNotAvailable
from full_view_agent.application.run_capability_snapshot import (
    RunCapabilitySnapshotService,
)
from full_view_agent.application.run_capability_snapshot_store import (
    InMemoryRunCapabilitySnapshotStore,
    PersistedRunCapabilitySnapshot,
)
from full_view_agent.application.runtime_skill_registry import RuntimeSkillRegistry
from full_view_agent.application.runtime_workflow_registry import RuntimeWorkflowRegistry
from full_view_agent.application.tool_registry import ToolRegistry
from full_view_agent.domain.agent_definition import (
    AgentModelVersionRef,
    AgentReleaseSnapshot,
)
from full_view_agent.domain.application import (
    AgentApplicationDefinition,
    ApplicationCapabilityBinding,
)
from full_view_agent.domain.capability import (
    SkillCapability,
    ToolCapability,
    WorkflowCapability,
    WorkflowEdgeDefinition,
    WorkflowNodeDefinition,
)
from full_view_agent.domain.models import LegacyIdentitySnapshot, Principal, WorkflowRef
from full_view_agent.infrastructure.application_registry import (
    InMemoryApplicationRegistry,
)
from full_view_agent.infrastructure.capability_repository import (
    InMemoryCapabilityRepository,
)
from full_view_agent.infrastructure.credential_broker import InMemoryCredentialBroker


def _tool(*, capability_id: str = "tool.live", version: str = "1.0.0") -> ToolCapability:
    return ToolCapability(
        capability_id=capability_id,
        name=capability_id,
        owner="test",
        version=version,
        status="published",
        connector_ref="test.connector",
        resource_path="/query",
        input_schema={"type": "object"},
    )


def _skill(*, tool_id: str = "tool.live") -> SkillCapability:
    return SkillCapability(
        capability_id="skill.live",
        name="skill.live",
        owner="test",
        version="1.0.0",
        status="published",
        guidance="Use the live Tool.",
        allowed_tool_ids=[tool_id],
    )


def _workflow(
    *, tool_id: str = "tool.live", capability_id: str = "workflow.live"
) -> WorkflowCapability:
    return WorkflowCapability(
        capability_id=capability_id,
        name=capability_id,
        owner="test",
        version="1.0.0",
        status="published",
        nodes=[
            WorkflowNodeDefinition(node_id="start", node_type="start"),
            WorkflowNodeDefinition(
                node_id="query",
                node_type="tool",
                tool_capability_id=tool_id,
                tool_version="1.0.0",
                config={"arguments": {}},
            ),
            WorkflowNodeDefinition(node_id="end", node_type="end"),
        ],
        edges=[
            WorkflowEdgeDefinition(source_node_id="start", target_node_id="query"),
            WorkflowEdgeDefinition(source_node_id="query", target_node_id="end"),
        ],
    )


def test_runtime_uses_explicit_dynamic_tool_dependencies() -> None:
    repository = InMemoryCapabilityRepository()
    adapter = AsyncMock()

    runtime = RuntimeContainer(
        credentials=InMemoryCredentialBroker(),
        capability_repository=repository,
        dynamic_tool_adapter=adapter,
    )

    assert runtime.capability_repository is repository
    assert runtime.dynamic_tool_adapter is adapter
    assert runtime.semantic_stack.capability._dynamic_tool_adapter is adapter


def test_tool_registry_replaces_dynamic_tools_through_existing_reference() -> None:
    registry = ToolRegistry.default()
    held_reference = registry
    tool = _tool()
    manifests, descriptors = build_dynamic_tool_registry_entries(
        [tool], base_registry=registry
    )

    registry.replace_dynamic(
        manifests=manifests,
        descriptors=descriptors,
        dynamic_input_schemas=build_dynamic_input_schemas([tool]),
    )

    assert held_reference is registry
    assert "tool.live" in held_reference.list_tool_ids()

    run_snapshot = registry.snapshot()
    registry.replace_dynamic(manifests=[], descriptors=[], dynamic_input_schemas={})

    assert "tool.live" not in held_reference.list_tool_ids()
    assert "tool.live" in run_snapshot.list_tool_ids()


@pytest.mark.asyncio
async def test_runtime_reload_updates_new_run_registries_and_keeps_old_snapshot() -> None:
    runtime = RuntimeContainer(credentials=InMemoryCredentialBroker())
    assert runtime.capability_repository is not None
    assert runtime.tool_registry is not None
    held_reference = runtime.tool_registry
    old_run_registry = held_reference.snapshot()

    await runtime.capability_repository.save_tool(_tool())
    await runtime.capability_repository.save_skill(_skill())
    await runtime.capability_repository.save_workflow(_workflow())

    await runtime.reload_runtime_capabilities()

    assert runtime.tool_registry is held_reference
    assert "tool.live" in held_reference.list_tool_ids()
    assert "tool.live" not in old_run_registry.list_tool_ids()
    assert [item.skill_id for item in runtime.runtime_skill_registry.list()] == [
        "skill.live"
    ]
    assert runtime.runtime_workflow_registry.get(
        WorkflowRef(workflow_id="workflow.live", workflow_version="1.0.0")
    ).workflow_id == "workflow.live"


@pytest.mark.asyncio
async def test_application_binding_filters_tool_skill_and_workflow_run_snapshot() -> None:
    """An application grant is the authority boundary for every capability kind."""

    repository = InMemoryCapabilityRepository()
    tool = _tool()
    skill = _skill()
    workflow = _workflow()
    await repository.save_tool(tool)
    await repository.save_skill(skill)
    await repository.save_workflow(workflow)
    loader = PublishedRuntimeCapabilityLoader(repository)
    application_registry = InMemoryApplicationRegistry(
        applications=[
            AgentApplicationDefinition(
                app_id="full_information_view",
                name="全量信息视图",
                default_agent_id="governance_general_agent",
                identity_adapter_id="identity.legacy_geo",
            )
        ],
        bindings=[
            ApplicationCapabilityBinding(
                app_id="full_information_view",
                capability_id=tool.capability_id,
                capability_version=tool.version,
                enabled=True,
            )
        ],
    )
    snapshots = RunCapabilitySnapshotService(
        repository,
        application_registry=application_registry,
        runtime_skill_registry=RuntimeSkillRegistry(await loader.load_skills()),
        runtime_workflow_registry=RuntimeWorkflowRegistry(
            await loader.load_workflows()
        ),
    )

    tool_only = await snapshots.create_snapshot_for_run(
        "run-tool-only",
        ToolRegistry.default(),
        app_id="full_information_view",
    )

    assert tool.capability_id in tool_only.tool_registry.list_tool_ids()
    assert tool_only.runtime_skill_registry.list() == ()
    assert tool_only.runtime_workflow_registry.list() == ()

    await application_registry.bind_capability(
        ApplicationCapabilityBinding(
            app_id="full_information_view",
            capability_id=skill.capability_id,
            capability_version=skill.version,
            enabled=True,
        )
    )
    await application_registry.bind_capability(
        ApplicationCapabilityBinding(
            app_id="full_information_view",
            capability_id=workflow.capability_id,
            capability_version=workflow.version,
            enabled=True,
        )
    )

    fully_bound = await snapshots.create_snapshot_for_run(
        "run-fully-bound",
        ToolRegistry.default(),
        app_id="full_information_view",
    )

    assert [item.skill_id for item in fully_bound.runtime_skill_registry.list()] == [
        skill.capability_id
    ]
    assert [
        item.workflow_id for item in fully_bound.runtime_workflow_registry.list()
    ] == [workflow.capability_id]


@pytest.mark.asyncio
async def test_agent_release_limits_each_run_and_restart_keeps_exact_resource_set() -> None:
    repository = InMemoryCapabilityRepository()
    store = InMemoryRunCapabilitySnapshotStore()
    tools = (_tool(capability_id="tool.alpha"), _tool(capability_id="tool.beta"))
    for tool in tools:
        await repository.save_tool(tool)
    skills = tuple(
        SkillCapability(
            capability_id=f"skill.{suffix}",
            name=f"skill.{suffix}",
            owner="test",
            version="1.0.0",
            status="published",
            guidance=f"Use tool.{suffix}",
            allowed_tool_ids=[f"tool.{suffix}"],
        )
        for suffix in ("alpha", "beta")
    )
    workflows = tuple(
        _workflow(
            tool_id=f"tool.{suffix}",
            capability_id=f"workflow.{suffix}",
        )
        for suffix in ("alpha", "beta")
    )
    for item in (*skills, *workflows):
        if isinstance(item, SkillCapability):
            await repository.save_skill(item)
        else:
            await repository.save_workflow(item)
    bindings = [
        ApplicationCapabilityBinding(
            app_id="full_information_view",
            capability_id=item.capability_id,
            capability_version=item.version,
        )
        for item in (*tools, *skills, *workflows)
    ]
    applications = InMemoryApplicationRegistry(
        applications=[
            AgentApplicationDefinition(
                app_id="full_information_view",
                name="Full view",
                default_agent_id="agent_alpha",
                identity_adapter_id="identity.legacy_geo",
            )
        ],
        bindings=bindings,
    )
    prompt_versions = {
        ("prompt.alpha", "1.0.0"): "alpha guidance",
        ("prompt.beta", "1.0.0"): "beta guidance",
    }

    async def load_prompt(*, prompt_id: str, version: str):
        from full_view_agent.domain.prompt_template import RuntimePromptSnapshot

        content = prompt_versions.get((prompt_id, version))
        if content is None:
            return None
        return RuntimePromptSnapshot(
            prompt_id=prompt_id,
            app_id="full_information_view",
            version=version,
            content=content,
        )

    service = RunCapabilitySnapshotService(
        repository,
        store=store,
        application_registry=applications,
        runtime_skill_registry=RuntimeSkillRegistry(
            tuple(build_runtime_skill_contract(item) for item in skills)
        ),
        runtime_workflow_registry=RuntimeWorkflowRegistry(
            tuple(
                build_runtime_workflow_snapshot(
                    item,
                    allowed_tool_refs={
                        (node.tool_capability_id, node.tool_version)
                        for node in item.nodes
                        if node.node_type == "tool"
                        and node.tool_capability_id is not None
                        and node.tool_version is not None
                    },
                )
                for item in workflows
            )
        ),
        prompt_snapshot_loader=load_prompt,
    )

    def release(suffix: str) -> AgentReleaseSnapshot:
        return AgentReleaseSnapshot(
            release_id=f"release-{suffix}",
            app_id="full_information_view",
            agent_id=f"agent_{suffix}",
            agent_version="1.0.0",
            prompt_ref=f"prompt.{suffix}@1.0.0",
            capability_refs=(f"tool.{suffix}@1.0.0",),
            skill_refs=(f"skill.{suffix}@1.0.0",),
            workflow_refs=(f"workflow.{suffix}@1.0.0",),
            knowledge_base_refs=(f"kb.{suffix}@1.0.0",),
            model_refs=(
                AgentModelVersionRef(
                    model_config_id="model",
                    config_version=1,
                    role="primary",
                    order=0,
                ),
            ),
            published_by="admin",
            reason="test",
        )

    alpha = await service.create_snapshot_for_run(
        "run-alpha",
        ToolRegistry.default(),
        app_id="full_information_view",
        agent_release=release("alpha"),
    )
    beta = await service.create_snapshot_for_run(
        "run-beta",
        ToolRegistry.default(),
        app_id="full_information_view",
        agent_release=release("beta"),
    )

    assert alpha.tool_versions == {"tool.alpha": "1.0.0"}
    assert [item.skill_id for item in alpha.runtime_skill_registry.list()] == [
        "skill.alpha"
    ]
    assert [item.workflow_id for item in alpha.runtime_workflow_registry.list()] == [
        "workflow.alpha"
    ]
    assert alpha.runtime_prompt_snapshot.composite_version == "prompt.alpha@1.0.0"  # type: ignore[union-attr]
    assert alpha.knowledge_base_versions == {"kb.alpha": 1}
    assert beta.tool_versions == {"tool.beta": "1.0.0"}

    restarted = RunCapabilitySnapshotService(
        repository,
        store=store,
        application_registry=applications,
        runtime_skill_registry=service._runtime_skill_registry,
        runtime_workflow_registry=service._runtime_workflow_registry,
        prompt_snapshot_loader=load_prompt,
    )
    recovered = await restarted.create_snapshot_for_run(
        "run-alpha",
        ToolRegistry.default(),
        app_id="full_information_view",
        agent_release=release("beta"),
    )
    assert recovered.tool_versions == {"tool.alpha": "1.0.0"}
    assert recovered.knowledge_base_versions == {"kb.alpha": 1}


@pytest.mark.asyncio
async def test_application_static_tool_grant_survives_snapshot_rebuild() -> None:
    """Restart recovery must not widen the app's pinned static Tool grant."""

    repository = InMemoryCapabilityRepository()
    store = InMemoryRunCapabilitySnapshotStore()
    application_registry = InMemoryApplicationRegistry(
        applications=[
            AgentApplicationDefinition(
                app_id="full_information_view",
                name="全量信息视图",
                default_agent_id="governance_general_agent",
                identity_adapter_id="identity.legacy_geo",
            )
        ],
        bindings=[
            ApplicationCapabilityBinding(
                app_id="full_information_view",
                capability_id="governance.resolve_area",
                capability_version="1.0.0",
                enabled=True,
            )
        ],
    )
    first = RunCapabilitySnapshotService(
        repository,
        store=store,
        application_registry=application_registry,
    )
    created = await first.create_snapshot_for_run(
        "run-static-grant",
        ToolRegistry.default(),
        app_id="full_information_view",
    )
    assert created.tool_registry.list_tool_ids() == ["governance.resolve_area"]

    restarted = RunCapabilitySnapshotService(
        repository,
        store=store,
        application_registry=application_registry,
    )
    rebuilt = await restarted.create_snapshot_for_run(
        "run-static-grant",
        ToolRegistry.default(),
        app_id="full_information_view",
    )

    assert rebuilt.tool_registry.list_tool_ids() == ["governance.resolve_area"]


@pytest.mark.asyncio
async def test_snapshot_service_caches_the_persistent_first_writer() -> None:
    """A cross-process winner must also replace the losing local cache."""

    class _ConcurrentWinnerStore(InMemoryRunCapabilitySnapshotStore):
        async def load(self, run_id: str):
            return None

        async def store_if_absent(self, snapshot):
            return PersistedRunCapabilitySnapshot(
                run_id=snapshot.run_id,
                tool_versions={},
                static_tool_versions={"governance.resolve_area": "1.0.0"},
                application_scoped=True,
                captured_at=datetime(2026, 8, 11, tzinfo=UTC),
            )

    service = RunCapabilitySnapshotService(
        InMemoryCapabilityRepository(),
        store=_ConcurrentWinnerStore(),
    )
    returned = await service.create_snapshot_for_run(
        "run-concurrent-winner",
        ToolRegistry.default(),
    )

    assert returned.tool_registry.list_tool_ids() == ["governance.resolve_area"]
    assert service.get_snapshot_for_run("run-concurrent-winner") is returned


@pytest.mark.asyncio
async def test_pinned_skill_and_workflow_rebuild_after_disable_and_restart() -> None:
    repository = InMemoryCapabilityRepository()
    store = InMemoryRunCapabilitySnapshotStore()
    tool = _tool()
    skill = _skill()
    workflow = _workflow()
    await repository.save_tool(tool)
    await repository.save_skill(skill)
    await repository.save_workflow(workflow)
    loader = PublishedRuntimeCapabilityLoader(repository)
    skills = RuntimeSkillRegistry(await loader.load_skills())
    workflows = RuntimeWorkflowRegistry(await loader.load_workflows())
    first = RunCapabilitySnapshotService(
        repository,
        store=store,
        runtime_skill_registry=skills,
        runtime_workflow_registry=workflows,
    )
    await first.create_snapshot_for_run("run-pinned", ToolRegistry.default())

    await repository.save_tool(
        tool.model_copy(update={"status": "disabled", "etag": tool.etag + 1})
    )
    await repository.save_skill(
        skill.model_copy(update={"status": "disabled", "etag": skill.etag + 1})
    )
    await repository.save_workflow(
        workflow.model_copy(
            update={"status": "disabled", "etag": workflow.etag + 1}
        )
    )
    restarted = RunCapabilitySnapshotService(repository, store=store)

    rebuilt = await restarted.create_snapshot_for_run(
        "run-pinned", ToolRegistry.default()
    )

    assert [item.skill_id for item in rebuilt.runtime_skill_registry.list()] == [
        "skill.live"
    ]
    assert rebuilt.runtime_workflow_registry.get(
        WorkflowRef(workflow_id="workflow.live", workflow_version="1.0.0")
    ).workflow_id == "workflow.live"


@pytest.mark.asyncio
async def test_runtime_reload_is_fail_closed_without_partial_updates() -> None:
    runtime = RuntimeContainer(credentials=InMemoryCredentialBroker())
    assert runtime.capability_repository is not None
    assert runtime.tool_registry is not None
    await runtime.capability_repository.save_tool(_tool())
    await runtime.capability_repository.save_skill(_skill())
    await runtime.reload_runtime_capabilities()
    before_tool_ids = runtime.tool_registry.list_tool_ids()
    before_skills = runtime.runtime_skill_registry.list()

    await runtime.capability_repository.save_skill(
        _skill(tool_id="tool.not_published").model_copy(
            update={"capability_id": "skill.invalid"}
        )
    )

    with pytest.raises(ValueError, match="unavailable Tool"):
        await runtime.reload_runtime_capabilities()

    assert runtime.tool_registry.list_tool_ids() == before_tool_ids
    assert runtime.runtime_skill_registry.list() == before_skills
    with pytest.raises(WorkflowNotAvailable):
        runtime.runtime_workflow_registry.get(
            WorkflowRef(workflow_id="workflow.live", workflow_version="1.0.0")
        )


@pytest.mark.asyncio
async def test_concurrent_reload_cannot_commit_a_mixed_generation() -> None:
    runtime = RuntimeContainer(credentials=InMemoryCredentialBroker())
    assert runtime.capability_repository is not None
    await runtime.capability_repository.save_tool(_tool())
    await runtime.capability_repository.save_skill(_skill())
    await runtime.capability_repository.save_workflow(_workflow())

    await asyncio.gather(
        runtime.reload_runtime_capabilities(),
        runtime.reload_runtime_capabilities(),
    )

    assert runtime.runtime_capability_generation == 2
    assert runtime.tool_registry is not None
    assert "tool.live" in runtime.tool_registry.list_tool_ids()
    assert len(runtime.runtime_skill_registry.list()) == 1
    assert len(runtime.runtime_workflow_registry.snapshot().list()) == 1


def test_composite_commit_blocks_readers_until_every_registry_is_updated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = RuntimeContainer(credentials=InMemoryCredentialBroker())
    assert runtime.capability_repository is not None
    assert runtime.tool_registry is not None
    registry = runtime.tool_registry
    asyncio.run(runtime.capability_repository.save_tool(_tool()))
    asyncio.run(runtime.capability_repository.save_skill(_skill()))
    asyncio.run(runtime.capability_repository.save_workflow(_workflow()))

    skill_commit_entered = threading.Event()
    allow_skill_commit = threading.Event()
    reader_completed = threading.Event()
    original_replace = runtime.runtime_skill_registry.replace

    def blocking_skill_replace(skills):
        skill_commit_entered.set()
        assert allow_skill_commit.wait(timeout=2)
        original_replace(skills)

    monkeypatch.setattr(runtime.runtime_skill_registry, "replace", blocking_skill_replace)
    reload_thread = threading.Thread(
        target=lambda: asyncio.run(runtime.reload_runtime_capabilities())
    )
    reload_thread.start()
    assert skill_commit_entered.wait(timeout=2)

    reader = threading.Thread(
        target=lambda: (
            registry.list_tool_ids(),
            reader_completed.set(),
        )
    )
    reader.start()
    reader_was_blocked = not reader_completed.wait(timeout=0.1)

    allow_skill_commit.set()
    reload_thread.join(timeout=2)
    reader.join(timeout=2)
    assert reader_was_blocked
    assert reader_completed.is_set()


@pytest.mark.asyncio
async def test_publish_triggers_reload_but_testing_transition_does_not() -> None:
    runtime = RuntimeContainer(credentials=InMemoryCredentialBroker())
    assert runtime.capability_repository is not None
    pending = _skill(tool_id="governance.resolve_area").model_copy(
        update={"status": "pending_approval"}
    )
    testing = _skill(tool_id="governance.resolve_area").model_copy(
        update={
            "capability_id": "skill.testing",
            "status": "draft",
        }
    )
    await runtime.capability_repository.save_skill(pending)
    await runtime.capability_repository.save_skill(testing)
    reload_spy = AsyncMock(wraps=runtime.reload_runtime_capabilities)
    runtime.reload_runtime_capabilities = reload_spy
    runtime.identity_port = AsyncMock()
    runtime.identity_port.resolve = AsyncMock(
        return_value=LegacyIdentitySnapshot(
            principal=Principal(
                tenant_id="test",
                user_id="admin",
                org_id="admins",
                roles=["admin"],
            ),
            source="test",
            source_session_expires_at=datetime.now(UTC) + timedelta(hours=1),
            base_area_codes=[],
        )
    )
    app = create_app(runtime)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"geoToken": SecretStr("token").get_secret_value()},
    ) as client:
        testing_response = await client.post(
            "/capability-api/v1/skills/skill.testing/1.0.0/testing",
            json={"reason": "test", "expected_etag": testing.etag},
        )
        publish_response = await client.post(
            "/capability-api/v1/skills/skill.live/1.0.0/publish",
            json={"reason": "approved", "expected_etag": pending.etag},
        )

    assert testing_response.status_code == 200
    assert publish_response.status_code == 200
    reload_spy.assert_awaited_once()


@pytest.mark.asyncio
async def test_invalid_skill_publish_leaves_repository_and_runtime_unchanged() -> None:
    runtime = RuntimeContainer(credentials=InMemoryCredentialBroker())
    assert runtime.capability_repository is not None
    invalid = _skill(tool_id="tool.not_published").model_copy(
        update={"capability_id": "skill.invalid", "status": "pending_approval"}
    )
    await runtime.capability_repository.save_skill(invalid)
    runtime.identity_port = AsyncMock()
    runtime.identity_port.resolve = AsyncMock(
        return_value=LegacyIdentitySnapshot(
            principal=Principal(
                tenant_id="test",
                user_id="admin",
                org_id="admins",
                roles=["admin"],
            ),
            source="test",
            source_session_expires_at=datetime.now(UTC) + timedelta(hours=1),
            base_area_codes=[],
        )
    )
    generation_before = runtime.runtime_capability_generation
    app = create_app(runtime)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"geoToken": SecretStr("token").get_secret_value()},
    ) as client:
        response = await client.post(
            "/capability-api/v1/skills/skill.invalid/1.0.0/publish",
            json={"reason": "must remain atomic", "expected_etag": invalid.etag},
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "runtime_capability_definition_invalid"
    persisted = await runtime.capability_repository.get("skill.invalid", "1.0.0")
    assert persisted is not None
    assert persisted.status == "pending_approval"
    assert persisted.etag == invalid.etag
    assert await runtime.capability_repository.get_active_snapshot("skill.invalid") is None
    assert await runtime.capability_repository.list_lifecycle_events("skill.invalid") == []
    assert runtime.runtime_capability_generation == generation_before
    assert runtime.runtime_skill_registry.list() == ()


@pytest.mark.asyncio
async def test_invalid_workflow_publish_leaves_repository_and_runtime_unchanged() -> None:
    runtime = RuntimeContainer(credentials=InMemoryCredentialBroker())
    assert runtime.capability_repository is not None
    await runtime.capability_repository.save_tool(_tool())
    invalid = WorkflowCapability(
        capability_id="workflow.invalid-graph",
        name="workflow.invalid-graph",
        owner="test",
        version="1.0.0",
        status="pending_approval",
        nodes=[
            WorkflowNodeDefinition(node_id="start", node_type="start"),
            WorkflowNodeDefinition(
                node_id="query",
                node_type="tool",
                tool_capability_id="tool.live",
                tool_version="1.0.0",
                config={"arguments": {}},
            ),
            WorkflowNodeDefinition(
                node_id="condition",
                node_type="condition",
                condition_expression="__import__('os').system('x')",
            ),
            WorkflowNodeDefinition(node_id="yes", node_type="summary"),
            WorkflowNodeDefinition(node_id="no", node_type="summary"),
            WorkflowNodeDefinition(
                node_id="merge",
                node_type="join",
                config={"mode": "selected"},
            ),
            WorkflowNodeDefinition(node_id="end", node_type="end"),
        ],
        edges=[
            WorkflowEdgeDefinition(source_node_id="start", target_node_id="query"),
            WorkflowEdgeDefinition(source_node_id="query", target_node_id="condition"),
            WorkflowEdgeDefinition(
                source_node_id="condition", target_node_id="yes", condition="true"
            ),
            WorkflowEdgeDefinition(
                source_node_id="condition", target_node_id="no", condition="false"
            ),
            WorkflowEdgeDefinition(source_node_id="yes", target_node_id="merge"),
            WorkflowEdgeDefinition(source_node_id="no", target_node_id="merge"),
            WorkflowEdgeDefinition(source_node_id="merge", target_node_id="end"),
        ],
    )
    await runtime.capability_repository.save_workflow(invalid)
    runtime.identity_port = AsyncMock()
    runtime.identity_port.resolve = AsyncMock(
        return_value=LegacyIdentitySnapshot(
            principal=Principal(
                tenant_id="test",
                user_id="admin",
                org_id="admins",
                roles=["admin"],
            ),
            source="test",
            source_session_expires_at=datetime.now(UTC) + timedelta(hours=1),
            base_area_codes=[],
        )
    )
    generation_before = runtime.runtime_capability_generation
    app = create_app(runtime)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"geoToken": SecretStr("token").get_secret_value()},
    ) as client:
        response = await client.post(
            "/capability-api/v1/workflows/workflow.invalid-graph/1.0.0/publish",
            json={"reason": "must remain atomic", "expected_etag": invalid.etag},
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == (
        "runtime_capability_definition_invalid"
    )
    persisted = await runtime.capability_repository.get(
        "workflow.invalid-graph", "1.0.0"
    )
    assert persisted is not None
    assert persisted.status == "pending_approval"
    assert persisted.etag == invalid.etag
    assert (
        await runtime.capability_repository.get_active_snapshot(
            "workflow.invalid-graph"
        )
        is None
    )
    assert (
        await runtime.capability_repository.list_lifecycle_events(
            "workflow.invalid-graph"
        )
        == []
    )
    assert runtime.runtime_capability_generation == generation_before
    assert runtime.runtime_workflow_registry.list() == ()
