from __future__ import annotations

import pytest
from fastapi import FastAPI
from pydantic import ValidationError

from full_view_agent.api.agent_routes import create_agent_router
from full_view_agent.application.agent_management_service import (
    AgentManagementService,
)
from full_view_agent.application.harness import AgentHarness
from full_view_agent.application.model_config_service import (
    InMemoryModelConfigKeyStore,
    ModelConfigService,
)
from full_view_agent.application.run_capability_snapshot import (
    RunCapabilitySnapshotService,
)
from full_view_agent.application.run_capability_snapshot_store import (
    InMemoryRunCapabilitySnapshotStore,
)
from full_view_agent.application.tool_registry import ToolRegistry
from full_view_agent.domain.agent_definition import (
    AgentDefinition,
    AgentExecutionPolicy,
    AgentModelPolicy,
    AgentModelVersionRef,
    AgentReleaseSnapshot,
    AgentVersion,
)
from full_view_agent.domain.application import AgentApplicationDefinition
from full_view_agent.domain.capability import ModelConfig
from full_view_agent.domain.models import AuthContext, ToolResult
from full_view_agent.infrastructure.agent_repository import InMemoryAgentRepository
from full_view_agent.infrastructure.application_registry import (
    InMemoryApplicationRegistry,
)
from full_view_agent.infrastructure.capability_repository import (
    InMemoryCapabilityRepository,
    InMemoryModelConfigRepository,
)


def test_agent_execution_policy_is_typed_and_bounded() -> None:
    policy = AgentExecutionPolicy(
        max_model_turns=6,
        max_tool_calls=9,
        max_elapsed_seconds=180,
    )

    assert policy.max_model_turns == 6
    assert policy.max_tool_calls == 9

    with pytest.raises(ValidationError):
        AgentExecutionPolicy(max_tool_calls=0)
    with pytest.raises(ValidationError):
        AgentExecutionPolicy(max_elapsed_seconds=3601)


class _UnusedExecutor:
    async def execute(
        self,
        *,
        tool_call_id: str,
        tool_id: str,
        raw_arguments: dict[str, object],
        auth_context: AuthContext,
    ) -> ToolResult:
        raise AssertionError("executor must not be called")


def test_harness_uses_agent_execution_policy_limits() -> None:
    policy = AgentExecutionPolicy(
        max_model_turns=4,
        max_tool_calls=6,
        max_consecutive_failures=2,
        max_no_progress=2,
        max_elapsed_seconds=75,
        repeated_call_limit=1,
    )

    harness = AgentHarness(tool_executor=_UnusedExecutor()).with_execution_policy(
        policy
    )

    assert harness.model_turn_limit == 4
    assert harness.limits.max_tool_calls == 6
    assert harness.limits.max_elapsed_seconds == 75


def test_agent_version_openapi_exposes_typed_execution_policy() -> None:
    app = FastAPI()
    app.include_router(create_agent_router(None))  # type: ignore[arg-type]

    schema = app.openapi()
    operation = schema["paths"][
        "/capability-api/v1/applications/{app_id}/agents/{agent_id}/versions"
    ]["post"]
    request_ref = operation["requestBody"]["content"]["application/json"]["schema"][
        "$ref"
    ]
    response_ref = operation["responses"]["201"]["content"]["application/json"][
        "schema"
    ]["$ref"]

    assert request_ref.endswith("/AgentVersionCreateBody")
    assert response_ref.endswith("/AgentVersionResponse")
    policy_ref = schema["components"]["schemas"]["AgentVersion"]["properties"][
        "execution_policy"
    ]["$ref"]
    assert policy_ref.endswith("/AgentExecutionPolicy")


@pytest.mark.asyncio
async def test_agent_release_pins_execution_policy_from_exact_version() -> None:
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
    model_repository = InMemoryModelConfigRepository()
    service = AgentManagementService(
        repository=agents,
        application_registry=applications,
        model_config_service=ModelConfigService(
            repository=model_repository,
            key_store=InMemoryModelConfigKeyStore(),
        ),
        capability_repository=InMemoryCapabilityRepository(),
    )
    await service.create_agent(
        AgentDefinition(
            app_id="full_information_view",
            agent_id="governance_general_agent",
            name="治理智能体",
        )
    )
    await model_repository.save(
        ModelConfig(
            config_id="primary-model",
            name="Primary model",
            api_base_url="https://models.example/v1",
            model_name="primary-model",
            is_enabled=True,
        )
    )
    policy = AgentExecutionPolicy(
        max_model_turns=5,
        max_tool_calls=7,
        max_elapsed_seconds=90,
    )
    await service.create_version(
        AgentVersion(
            app_id="full_information_view",
            agent_id="governance_general_agent",
            version="1.0.0",
            execution_policy=policy,
        )
    )
    await service.set_model_policy(
        app_id="full_information_view",
        agent_id="governance_general_agent",
        version="1.0.0",
        policy=AgentModelPolicy(primary_model_config_id="primary-model"),
    )

    release = await service.publish_version(
        app_id="full_information_view",
        agent_id="governance_general_agent",
        version="1.0.0",
        published_by="admin",
        reason="publish execution policy",
    )

    assert release.execution_policy == policy
    persisted = await service.get_active_release(
        "full_information_view", "governance_general_agent"
    )
    assert persisted.execution_policy == policy


@pytest.mark.asyncio
async def test_run_snapshot_restores_execution_policy_after_restart() -> None:
    repository = InMemoryCapabilityRepository()
    store = InMemoryRunCapabilitySnapshotStore()
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
    policy = AgentExecutionPolicy(
        max_model_turns=4,
        max_tool_calls=6,
        max_elapsed_seconds=75,
    )
    release = AgentReleaseSnapshot(
        release_id="arel_policy",
        app_id="full_information_view",
        agent_id="governance_general_agent",
        agent_version="1.0.0",
        execution_policy=policy,
        model_refs=(
            AgentModelVersionRef(
                model_config_id="primary-model",
                config_version=1,
                role="primary",
                order=0,
            ),
        ),
        published_by="admin",
        reason="pin policy",
    )
    first_service = RunCapabilitySnapshotService(
        repository,
        store=store,
        application_registry=applications,
    )
    first = await first_service.create_snapshot_for_run(
        "run-policy",
        ToolRegistry.default(),
        app_id="full_information_view",
        agent_release=release,
    )
    restarted_service = RunCapabilitySnapshotService(
        repository,
        store=store,
        application_registry=applications,
    )
    restored = await restarted_service.create_snapshot_for_run(
        "run-policy",
        ToolRegistry.default(),
        app_id="full_information_view",
    )

    assert first.execution_policy == policy
    assert restored.execution_policy == policy
