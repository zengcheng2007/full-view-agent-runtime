from __future__ import annotations

from pathlib import Path

import pytest

from full_view_agent.application.agent_management_service import (
    AgentManagementService,
)
from full_view_agent.application.errors import ResourceNotFound, RunStateConflict
from full_view_agent.application.model_config_service import (
    InMemoryModelConfigKeyStore,
    ModelConfigService,
)
from full_view_agent.application.run_capability_snapshot import (
    RunCapabilitySnapshotService,
)
from full_view_agent.application.tool_registry import ToolRegistry
from full_view_agent.domain.agent_definition import (
    AgentDefinition,
    AgentModelPolicy,
    AgentVersion,
)
from full_view_agent.domain.application import (
    AgentApplicationDefinition,
    ApplicationCapabilityBinding,
)
from full_view_agent.domain.capability import (
    SkillCapability,
    ToolCapability,
    WorkflowCapability,
)
from full_view_agent.domain.prompt_template import PromptTemplate
from full_view_agent.infrastructure.agent_repository import InMemoryAgentRepository
from full_view_agent.infrastructure.application_registry import (
    InMemoryApplicationRegistry,
)
from full_view_agent.infrastructure.capability_repository import (
    InMemoryCapabilityRepository,
    InMemoryModelConfigRepository,
)
from tests.model_resource_helpers import publish_tested_model


async def _service() -> tuple[
    AgentManagementService,
    InMemoryAgentRepository,
    InMemoryApplicationRegistry,
    InMemoryModelConfigRepository,
    InMemoryCapabilityRepository,
]:
    applications = InMemoryApplicationRegistry(
        applications=[
            AgentApplicationDefinition(
                app_id="full_information_view",
                name="全量信息视图",
                default_agent_id="governance_general_agent",
                identity_adapter_id="identity.legacy_geo",
            )
        ]
    )
    agents = InMemoryAgentRepository()
    models = InMemoryModelConfigRepository()
    capabilities = InMemoryCapabilityRepository()
    service = AgentManagementService(
        repository=agents,
        application_registry=applications,
        model_config_service=ModelConfigService(
            repository=models,
            key_store=InMemoryModelConfigKeyStore(),
        ),
        capability_repository=capabilities,
    )
    return service, agents, applications, models, capabilities


@pytest.mark.asyncio
async def test_one_application_can_own_multiple_agents_and_change_default() -> None:
    service, _, applications, models, _ = await _service()

    await service.create_agent(
        AgentDefinition(
            app_id="full_information_view",
            agent_id="governance_general_agent",
            name="治理问答智能体",
        )
    )
    await service.create_agent(
        AgentDefinition(
            app_id="full_information_view",
            agent_id="regional_analysis_agent",
            name="区域研判智能体",
        )
    )
    regional_model = await publish_tested_model(
        service._models,  # type: ignore[attr-defined]
        name="Regional model",
        model_name="regional-model",
    )
    await service.create_version(
        AgentVersion(
            app_id="full_information_view",
            agent_id="regional_analysis_agent",
            version="1.0.0",
        )
    )
    await service.set_model_policy(
        app_id="full_information_view",
        agent_id="regional_analysis_agent",
        version="1.0.0",
        policy=AgentModelPolicy(primary_model_config_id=regional_model.config_id),
    )
    await service.publish_version(
        app_id="full_information_view",
        agent_id="regional_analysis_agent",
        version="1.0.0",
        published_by="admin",
        reason="ready as default",
    )
    changed = await service.set_default_agent(
        app_id="full_information_view",
        agent_id="regional_analysis_agent",
        expected_application_etag=1,
        changed_by="admin",
        reason="区域研判作为默认入口",
    )

    assert [item.agent_id for item in await service.list_agents("full_information_view")] == [
        "governance_general_agent",
        "regional_analysis_agent",
    ]
    assert changed.default_agent_id == "regional_analysis_agent"
    assert (await applications.get_application("full_information_view")).etag == 2  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_legacy_default_agent_gets_idempotent_trusted_baseline_release() -> None:
    service, _, applications, models, capabilities = await _service()
    app_id = "full_information_view"
    agent_id = "governance_general_agent"
    await service.create_agent(
        AgentDefinition(app_id=app_id, agent_id=agent_id, name="Legacy governance")
    )
    trusted_model = await publish_tested_model(
        service._models,  # type: ignore[attr-defined]
        name="Trusted model",
        model_name="trusted-model",
        legacy_default=True,
    )
    tool = ToolCapability(
        capability_id="governance.resolve_area",
        name="Resolve area",
        owner="governance",
        version="1.0.0",
        status="published",
        guidance="Test guidance for Resolve area",
        connector_ref="governance-gateway",
        resource_path="/areas/resolve",
    )
    await capabilities.save_tool(tool)
    await applications.bind_capability(
        ApplicationCapabilityBinding(
            app_id=app_id,
            capability_id=tool.capability_id,
            capability_version=tool.version,
        )
    )

    first = await service.ensure_legacy_baseline_release(
        app_id=app_id,
        agent_id=agent_id,
    )
    second = await service.ensure_legacy_baseline_release(
        app_id=app_id,
        agent_id=agent_id,
    )

    assert first is not None
    assert second == first
    assert first.capability_refs == ("governance.resolve_area@1.0.0",)
    assert first.model_refs[0].model_config_id == trusted_model.config_id
    application = await applications.get_application(app_id)
    assert application is not None
    restored = await service.set_default_agent(
        app_id=app_id,
        agent_id=agent_id,
        expected_application_etag=application.etag,
        changed_by="system",
        reason="restore legacy default after migration",
    )
    assert restored.default_agent_id == agent_id


@pytest.mark.asyncio
async def test_managed_legacy_baseline_rolls_forward_new_application_grants() -> None:
    service, agents, applications, models, capabilities = await _service()
    app_id = "full_information_view"
    agent_id = "governance_general_agent"
    await service.create_agent(
        AgentDefinition(app_id=app_id, agent_id=agent_id, name="Legacy governance")
    )
    await publish_tested_model(
        service._models,  # type: ignore[attr-defined]
        name="Trusted model",
        model_name="trusted-model",
        legacy_default=True,
    )
    resolve = ToolCapability(
        capability_id="governance.resolve_area",
        name="Resolve area",
        owner="governance",
        version="1.0.0",
        status="published",
        guidance="Test guidance for Resolve area",
        connector_ref="governance-gateway",
        resource_path="/areas/resolve",
    )
    power = ToolCapability(
        capability_id="governance.query_governance_power_metrics",
        name="Governance power",
        owner="governance",
        version="1.0.0",
        status="published",
        guidance="Test guidance for Governance power",
        connector_ref="governance-gateway",
        resource_path="/governance-power",
    )
    custom = ToolCapability(
        capability_id="custom.operator_tool",
        name="Operator tool",
        owner="operator",
        version="1.0.0",
        status="published",
        guidance="Test guidance for Operator tool",
        connector_ref="operator-gateway",
        resource_path="/custom",
    )
    await capabilities.save_tool(resolve)
    await applications.bind_capability(
        ApplicationCapabilityBinding(
            app_id=app_id,
            capability_id=resolve.capability_id,
            capability_version=resolve.version,
        )
    )
    baseline = await service.ensure_legacy_baseline_release(
        app_id=app_id,
        agent_id=agent_id,
    )
    assert baseline is not None
    old_run = await service.bind_run(
        run_id="run-before-v025",
        app_id=app_id,
        agent_id=agent_id,
    )

    await capabilities.save_tool(power)
    await applications.bind_capability(
        ApplicationCapabilityBinding(
            app_id=app_id,
            capability_id=power.capability_id,
            capability_version=power.version,
        )
    )
    await capabilities.save_tool(custom)
    await applications.bind_capability(
        ApplicationCapabilityBinding(
            app_id=app_id,
            capability_id=custom.capability_id,
            capability_version=custom.version,
        )
    )
    upgraded = await service.ensure_legacy_baseline_release(
        app_id=app_id,
        agent_id=agent_id,
    )
    repeated = await service.ensure_legacy_baseline_release(
        app_id=app_id,
        agent_id=agent_id,
    )
    restarted_service = AgentManagementService(
        repository=agents,
        application_registry=applications,
        model_config_service=ModelConfigService(
            repository=models,
            key_store=InMemoryModelConfigKeyStore(),
        ),
        capability_repository=capabilities,
    )
    after_restart = await restarted_service.ensure_legacy_baseline_release(
        app_id=app_id,
        agent_id=agent_id,
    )
    new_run = await service.bind_run(
        run_id="run-after-v025",
        app_id=app_id,
        agent_id=agent_id,
    )
    snapshots = RunCapabilitySnapshotService(
        repository=capabilities,
        application_registry=applications,
    )
    old_capabilities = await snapshots.create_snapshot_for_run(
        "snapshot-before-v025",
        ToolRegistry.default(),
        app_id=app_id,
        agent_release=baseline,
    )
    new_capabilities = await snapshots.create_snapshot_for_run(
        "snapshot-after-v025",
        ToolRegistry.default(),
        app_id=app_id,
        agent_release=upgraded,
    )

    assert upgraded is not None
    assert upgraded.release_id != baseline.release_id
    assert upgraded.agent_version == "0.0.2"
    assert upgraded.capability_refs == (
        "governance.query_governance_power_metrics@1.0.0",
        "governance.resolve_area@1.0.0",
    )
    assert old_run.release_id == baseline.release_id
    assert old_run.capability_refs == ("governance.resolve_area@1.0.0",)
    assert new_run.release_id == upgraded.release_id
    assert new_run.capability_refs == upgraded.capability_refs
    assert "governance.query_governance_power_metrics" not in (
        old_capabilities.tool_registry.list_tool_ids()
    )
    assert "governance.query_governance_power_metrics" in (
        new_capabilities.tool_registry.list_tool_ids()
    )
    assert "custom.operator_tool" not in new_capabilities.tool_registry.list_tool_ids()
    assert repeated == upgraded
    assert after_restart == upgraded
    assert len(await agents.list_versions(app_id, agent_id)) == 2


@pytest.mark.asyncio
async def test_non_system_agent_release_is_not_expanded_from_application_grants() -> None:
    service, _, applications, models, capabilities = await _service()
    app_id = "full_information_view"
    agent_id = "governance_general_agent"
    await service.create_agent(
        AgentDefinition(app_id=app_id, agent_id=agent_id, name="Operator managed")
    )
    trusted_model = await publish_tested_model(
        service._models,  # type: ignore[attr-defined]
        name="Trusted model",
        model_name="trusted-model",
    )
    resolve = ToolCapability(
        capability_id="governance.resolve_area",
        name="Resolve area",
        owner="governance",
        version="1.0.0",
        status="published",
        guidance="Test guidance for Resolve area",
        connector_ref="governance-gateway",
        resource_path="/areas/resolve",
    )
    power = ToolCapability(
        capability_id="governance.query_governance_power_metrics",
        name="Governance power",
        owner="governance",
        version="1.0.0",
        status="published",
        guidance="Test guidance for Governance power",
        connector_ref="governance-gateway",
        resource_path="/governance-power",
    )
    for tool in (resolve, power):
        await capabilities.save_tool(tool)
        await applications.bind_capability(
            ApplicationCapabilityBinding(
                app_id=app_id,
                capability_id=tool.capability_id,
                capability_version=tool.version,
            )
        )
    await service.create_version(
        AgentVersion(
            app_id=app_id,
            agent_id=agent_id,
            version="1.0.0",
            capability_refs=("governance.resolve_area@1.0.0",),
        )
    )
    await service.set_model_policy(
        app_id=app_id,
        agent_id=agent_id,
        version="1.0.0",
        policy=AgentModelPolicy(primary_model_config_id=trusted_model.config_id),
    )
    operator_release = await service.publish_version(
        app_id=app_id,
        agent_id=agent_id,
        version="1.0.0",
        published_by="admin",
        reason="operator-owned release",
    )

    result = await service.ensure_legacy_baseline_release(
        app_id=app_id,
        agent_id=agent_id,
    )

    assert result == operator_release
    assert result.capability_refs == ("governance.resolve_area@1.0.0",)


@pytest.mark.asyncio
async def test_agent_release_requires_real_model_and_published_capabilities() -> None:
    service, _, applications, models, capabilities = await _service()
    await service.create_agent(
        AgentDefinition(
            app_id="full_information_view",
            agent_id="governance_general_agent",
            name="治理问答智能体",
        )
    )
    await service.create_version(
        AgentVersion(
            app_id="full_information_view",
            agent_id="governance_general_agent",
            version="1.0.0",
            capability_refs=("governance.resolve_area@1.0.0",),
        )
    )
    await service.set_model_policy(
        app_id="full_information_view",
        agent_id="governance_general_agent",
        version="1.0.0",
        policy=AgentModelPolicy(
            primary_model_config_id="missing-model",
            fallback_model_config_ids=("fallback-model",),
        ),
    )

    invalid = await service.validate_version(
        app_id="full_information_view",
        agent_id="governance_general_agent",
        version="1.0.0",
    )
    assert invalid.is_valid is False
    assert {issue.code for issue in invalid.issues} == {
        "MODEL_CONFIG_NOT_FOUND",
        "CAPABILITY_NOT_PUBLISHED",
    }

    published_models = [
        await publish_tested_model(
            service._models,  # type: ignore[attr-defined]
            name=name,
            model_name=name,
        )
        for name in ("primary-model", "fallback-model")
    ]
    primary_model, fallback_model = published_models
    await capabilities.save_tool(
        ToolCapability(
            capability_id="governance.resolve_area",
            name="解析区划",
            owner="platform",
            version="1.0.0",
            status="published",
            guidance="Test guidance for 解析区划",
            connector_ref="governance",
            resource_path="/area/resolve",
        )
    )
    await service.set_model_policy(
        app_id="full_information_view",
        agent_id="governance_general_agent",
        version="1.0.0",
        policy=AgentModelPolicy(
            primary_model_config_id=primary_model.config_id,
            fallback_model_config_ids=(fallback_model.config_id,),
        ),
    )
    unauthorized = await service.validate_version(
        app_id="full_information_view",
        agent_id="governance_general_agent",
        version="1.0.0",
    )
    assert {issue.code for issue in unauthorized.issues} == {
        "CAPABILITY_NOT_AUTHORIZED"
    }
    await applications.bind_capability(
        ApplicationCapabilityBinding(
            app_id="full_information_view",
            capability_id="governance.resolve_area",
            capability_version="1.0.0",
        )
    )
    valid = await service.validate_version(
        app_id="full_information_view",
        agent_id="governance_general_agent",
        version="1.0.0",
    )
    assert valid.is_valid is True

    await service._models.disable_config(  # type: ignore[attr-defined]
        config_id=fallback_model.config_id,
        expected_etag=fallback_model.etag,
        actor="admin",
        reason="verify Agent rejects a disabled model resource",
    )
    disabled = await service.validate_version(
        app_id="full_information_view",
        agent_id="governance_general_agent",
        version="1.0.0",
    )
    assert {issue.code for issue in disabled.issues} == {"MODEL_VERSION_NOT_ELIGIBLE"}


@pytest.mark.asyncio
async def test_publish_freezes_release_and_new_runs_do_not_change_old_runs() -> None:
    service, repository, _, models, _ = await _service()
    model_a = await publish_tested_model(
        service._models,  # type: ignore[attr-defined]
        name="模型 A",
        model_name="model-a",
    )
    model_b = await publish_tested_model(
        service._models,  # type: ignore[attr-defined]
        name="模型 B",
        model_name="model-b",
    )
    await service.create_agent(
        AgentDefinition(
            app_id="full_information_view",
            agent_id="governance_general_agent",
            name="治理问答智能体",
        )
    )
    await service.create_version(
        AgentVersion(
            app_id="full_information_view",
            agent_id="governance_general_agent",
            version="1.0.0",
        )
    )
    await service.set_model_policy(
        app_id="full_information_view",
        agent_id="governance_general_agent",
        version="1.0.0",
        policy=AgentModelPolicy(
            primary_model_config_id=model_a.config_id,
            fallback_model_config_ids=(model_b.config_id,),
        ),
    )

    release_v1 = await service.publish_version(
        app_id="full_information_view",
        agent_id="governance_general_agent",
        version="1.0.0",
        published_by="admin",
        reason="验收通过",
    )
    old_run = await service.bind_run(
        run_id="run-old",
        app_id="full_information_view",
        agent_id="governance_general_agent",
    )

    await service._models.create_version(  # type: ignore[attr-defined]
        config_id=model_a.config_id,
        expected_etag=model_a.etag,
        actor="admin",
        reason="prepare a later model version without changing the old release",
        changes={"model_name": "model-a-v2"},
    )
    await service.create_version(
        AgentVersion(
            app_id="full_information_view",
            agent_id="governance_general_agent",
            version="2.0.0",
        )
    )
    await service.set_model_policy(
        app_id="full_information_view",
        agent_id="governance_general_agent",
        version="2.0.0",
        policy=AgentModelPolicy(primary_model_config_id=model_b.config_id),
    )
    release_v2 = await service.publish_version(
        app_id="full_information_view",
        agent_id="governance_general_agent",
        version="2.0.0",
        published_by="admin",
        reason="升级模型",
    )
    new_run = await service.bind_run(
        run_id="run-new",
        app_id="full_information_view",
        agent_id="governance_general_agent",
    )
    old_run_reloaded = await repository.get_run_snapshot("run-old")

    assert release_v1.model_refs[0].model_config_id == model_a.config_id
    assert release_v1.model_refs[0].config_version == model_a.version
    assert release_v2.release_id != release_v1.release_id
    assert new_run.agent_version == "2.0.0"
    assert new_run.model_refs[0].model_config_id == model_b.config_id
    assert old_run_reloaded == old_run
    assert old_run_reloaded.agent_version == "1.0.0"  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_publish_rejects_incomplete_agent_version() -> None:
    service, _, _, _, _ = await _service()
    await service.create_agent(
        AgentDefinition(
            app_id="full_information_view",
            agent_id="governance_general_agent",
            name="治理问答智能体",
        )
    )
    await service.create_version(
        AgentVersion(
            app_id="full_information_view",
            agent_id="governance_general_agent",
            version="1.0.0",
        )
    )

    with pytest.raises(RunStateConflict, match="MODEL_POLICY_MISSING"):
        await service.publish_version(
            app_id="full_information_view",
            agent_id="governance_general_agent",
            version="1.0.0",
            published_by="admin",
            reason="不完整配置",
        )


@pytest.mark.asyncio
async def test_run_binding_fails_closed_when_application_default_agent_is_missing() -> None:
    service, _, _, _, _ = await _service()

    with pytest.raises(ResourceNotFound, match="default agent is not registered"):
        await service.bind_run(
            run_id="run-with-missing-default",
            app_id="full_information_view",
        )


@pytest.mark.asyncio
async def test_release_validates_every_nonempty_versioned_resource_reference() -> None:
    service, agents, applications, models, capabilities = await _service()
    ready_model = await publish_tested_model(
        service._models,  # type: ignore[attr-defined]
        name="可用模型",
        model_name="model-ready",
    )
    await service.create_agent(
        AgentDefinition(
            app_id="full_information_view",
            agent_id="resource_agent",
            name="资源装配智能体",
        )
    )
    version = AgentVersion(
        app_id="full_information_view",
        agent_id="resource_agent",
        version="1.0.0",
        prompt_ref="prompt.full-view@1.0.0",
        capability_refs=("governance.resolve_area@1.0.0",),
        skill_refs=("skill.analysis@1.0.0",),
        workflow_refs=("workflow.analysis@1.0.0",),
        knowledge_base_refs=("kb.governance@2.0.0",),
    )
    await service.create_version(version)
    await service.set_model_policy(
        app_id=version.app_id,
        agent_id=version.agent_id,
        version=version.version,
        policy=AgentModelPolicy(primary_model_config_id=ready_model.config_id),
    )

    unavailable = await service.validate_version(
        app_id=version.app_id,
        agent_id=version.agent_id,
        version=version.version,
    )
    assert {issue.code for issue in unavailable.issues} == {
        "CAPABILITY_NOT_PUBLISHED",
        "PROMPT_VALIDATOR_UNAVAILABLE",
        "SKILL_NOT_PUBLISHED",
        "WORKFLOW_NOT_PUBLISHED",
        "KNOWLEDGE_VALIDATOR_UNAVAILABLE",
    }

    await capabilities.save_skill(
        SkillCapability(
            capability_id="skill.analysis",
            name="研判技能",
            owner="platform",
            version="1.0.0",
            status="published",
            guidance="Test skill guidance",
            allowed_tool_ids=["governance.resolve_area"],
        )
    )
    await capabilities.save_tool(
        ToolCapability(
            capability_id="governance.resolve_area",
            name="Resolve area",
            owner="platform",
            version="1.0.0",
            status="published",
            guidance="Test guidance for Resolve area",
            connector_ref="governance",
            resource_path="/area/resolve",
        )
    )
    await capabilities.save_workflow(
        WorkflowCapability(
            capability_id="workflow.analysis",
            name="研判工作流",
            owner="platform",
            version="1.0.0",
            status="published",
            guidance="Test workflow guidance",
        )
    )
    for capability_id in (
        "governance.resolve_area",
        "skill.analysis",
        "workflow.analysis",
    ):
        await applications.bind_capability(
            ApplicationCapabilityBinding(
                app_id="full_information_view",
                capability_id=capability_id,
                capability_version="1.0.0",
            )
        )

    class _PromptReader:
        layer = "application"

        async def get_template(self, prompt_id: str, version: str):
            return PromptTemplate(
                prompt_id=prompt_id,
                app_id="full_information_view",
                layer=self.layer,
                name="全量信息视图提示词",
                version=version,
                content="只使用已授权能力回答。",
                status="published",
                created_by="admin",
                updated_by="admin",
            )

    class _KnowledgeReader:
        async def is_ready_version(
            self, *, app_id: str, knowledge_base_id: str, version: int
        ) -> bool:
            return (
                app_id == "full_information_view"
                and knowledge_base_id == "kb.governance"
                and version == 2
            )

    checked = AgentManagementService(
        repository=agents,
        application_registry=applications,
        model_config_service=ModelConfigService(
            repository=models,
            key_store=InMemoryModelConfigKeyStore(),
        ),
        capability_repository=capabilities,
        prompt_reader=_PromptReader(),
        knowledge_reader=_KnowledgeReader(),
    )
    wrong_layer = await checked.validate_version(
        app_id=version.app_id,
        agent_id=version.agent_id,
        version=version.version,
    )
    assert {issue.code for issue in wrong_layer.issues} == {"PROMPT_LAYER_INVALID"}

    _PromptReader.layer = "agent"
    valid = await checked.validate_version(
        app_id=version.app_id,
        agent_id=version.agent_id,
        version=version.version,
    )
    assert valid.is_valid is True


@pytest.mark.asyncio
async def test_agent_validation_requires_workflow_skill_dependency_closure() -> None:
    service, _, applications, models, capabilities = await _service()
    ready_model = await publish_tested_model(
        service._models,  # type: ignore[attr-defined]
        name="Ready model",
        model_name="ready",
    )
    await service.create_agent(
        AgentDefinition(
            app_id="full_information_view",
            agent_id="workflow_agent",
            name="Workflow Agent",
        )
    )
    version = AgentVersion(
        app_id="full_information_view",
        agent_id="workflow_agent",
        version="1.0.0",
        workflow_refs=("workflow.skill-chain@1.0.0",),
    )
    await service.create_version(version)
    await service.set_model_policy(
        app_id=version.app_id,
        agent_id=version.agent_id,
        version=version.version,
        policy=AgentModelPolicy(primary_model_config_id=ready_model.config_id),
    )
    await capabilities.save_skill(
        SkillCapability(
            capability_id="skill.area",
            name="Area Skill",
            owner="platform",
            version="1.0.0",
            status="published",
            guidance="Test skill guidance",
            allowed_tool_ids=["governance.resolve_area"],
        )
    )
    await capabilities.save_workflow(
        WorkflowCapability(
            capability_id="workflow.skill-chain",
            name="Skill chain",
            owner="platform",
            version="1.0.0",
            status="published",
            guidance="Test workflow guidance",
            nodes=[
                {"node_id": "start", "node_type": "start"},
                {
                    "node_id": "skill",
                    "node_type": "skill",
                    "skill_capability_id": "skill.area",
                    "skill_version": "1.0.0",
                },
                {"node_id": "end", "node_type": "end"},
            ],
            edges=[
                {"source_node_id": "start", "target_node_id": "skill"},
                {"source_node_id": "skill", "target_node_id": "end"},
            ],
        )
    )
    await applications.bind_capability(
        ApplicationCapabilityBinding(
            app_id="full_information_view",
            capability_id="workflow.skill-chain",
            capability_version="1.0.0",
        )
    )

    report = await service.validate_version(
        app_id=version.app_id,
        agent_id=version.agent_id,
        version=version.version,
    )

    assert "DEPENDENCY_SKILL_NOT_REFERENCED" in {
        issue.code for issue in report.issues
    }


def test_v024_migration_defines_agent_release_and_run_snapshot_tables() -> None:
    migration = (
        Path(__file__).parents[1] / "scripts/migrations/V024_application_agents.sql"
    ).read_text(encoding="utf-8")

    for table in (
        "agent_definitions",
        "agent_versions",
        "agent_model_policies",
        "agent_release_snapshots",
        "run_agent_release_snapshots",
    ):
        assert table in migration
    assert "SELECT 24" in migration


@pytest.mark.asyncio
async def test_postgres_agent_repository_survives_restart(pg_schema) -> None:
    from full_view_agent.infrastructure.agent_repository import (
        PostgresAgentRepository,
    )

    first = PostgresAgentRepository(dsn=pg_schema["dsn"], schema=pg_schema["schema"])
    agent = AgentDefinition(
        app_id="full_information_view",
        agent_id="regional_analysis_agent",
        name="区域研判智能体",
    )
    version = AgentVersion(
        app_id=agent.app_id,
        agent_id=agent.agent_id,
        version="1.0.0",
    )
    await first.save_agent(agent)
    await first.save_version(version)
    await first.save_model_policy(
        agent.app_id,
        agent.agent_id,
        version.version,
        AgentModelPolicy(primary_model_config_id="model-a"),
    )

    reopened = PostgresAgentRepository(dsn=pg_schema["dsn"], schema=pg_schema["schema"])
    assert await reopened.get_agent(agent.app_id, agent.agent_id) == agent
    assert await reopened.get_version(agent.app_id, agent.agent_id, version.version) == version
    assert (
        await reopened.get_model_policy(agent.app_id, agent.agent_id, version.version)
    ).primary_model_config_id == "model-a"  # type: ignore[union-attr]
