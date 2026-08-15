from __future__ import annotations

import pytest

from full_view_agent.application.context_builder import AgentContextBuilder
from full_view_agent.application.harness import HarnessState
from full_view_agent.application.prompt_catalog import build_runtime_safety_kernel
from full_view_agent.application.prompt_template_service import PromptTemplateService
from full_view_agent.application.run_capability_snapshot import (
    RunCapabilitySnapshotService,
)
from full_view_agent.application.run_capability_snapshot_store import (
    InMemoryRunCapabilitySnapshotStore,
)
from full_view_agent.application.runtime_prompt_registry import RuntimePromptRegistry
from full_view_agent.application.session_run_service import SessionRunService
from full_view_agent.application.tool_registry import ToolRegistry
from full_view_agent.domain.agent_definition import (
    AgentModelVersionRef,
    AgentReleaseSnapshot,
)
from full_view_agent.domain.application import AgentApplicationDefinition
from full_view_agent.domain.capability import ToolSemanticContract
from full_view_agent.domain.prompt_template import RuntimePromptSnapshot
from full_view_agent.infrastructure.application_registry import (
    InMemoryApplicationRegistry,
)
from full_view_agent.infrastructure.capability_repository import (
    InMemoryCapabilityRepository,
)
from full_view_agent.infrastructure.memory_store import InMemoryAgentStore
from full_view_agent.infrastructure.prompt_template_repository import (
    InMemoryPromptTemplateRepository,
)

from .test_policy import population_auth_context
from .test_session_run_service import run_request


async def _publish(
    service: PromptTemplateService,
    *,
    prompt_id: str,
    layer: str,
    content: str,
) -> None:
    item = await service.create(
        prompt_id=prompt_id,
        app_id="full_information_view",
        layer=layer,
        name=prompt_id,
        version="1.0.0",
        content=content,
        actor="admin",
        reason="test",
    )
    for status in ("testing", "pending_approval", "published"):
        item = await service.transition(
            prompt_id=item.prompt_id,
            version=item.version,
            to_status=status,
            expected_etag=item.etag,
            actor="admin",
            reason="test",
        )


@pytest.mark.asyncio
async def test_application_and_agent_prompt_layers_publish_independently() -> None:
    service = PromptTemplateService(InMemoryPromptTemplateRepository())

    await _publish(
        service,
        prompt_id="full-view.application-policy",
        layer="application",
        content="APPLICATION_POLICY_SENTINEL",
    )
    await _publish(
        service,
        prompt_id="full-view.agent-instruction",
        layer="agent",
        content="AGENT_INSTRUCTION_SENTINEL",
    )

    application_prompt = await service.get_effective(
        app_id="full_information_view", layer="application"
    )
    agent_prompt = await service.load_snapshot(
        prompt_id="full-view.agent-instruction", version="1.0.0"
    )

    assert application_prompt is not None
    assert application_prompt.layer == "application"
    assert agent_prompt is not None
    assert agent_prompt.layer == "agent"
    assert [
        item.prompt_id
        for item in await service.list(
            app_id="full_information_view", layer="agent"
        )
    ] == ["full-view.agent-instruction"]
    with pytest.raises(ValueError, match="application"):
        await service.get_effective(
            app_id="full_information_view", layer="agent"
        )


def test_runtime_prompt_registry_isolates_effective_prompt_by_application() -> None:
    app_a = RuntimePromptSnapshot(
        prompt_id="app-a.policy",
        app_id="app-a",
        layer="application",
        version="1.0.0",
        content="APP_A_POLICY",
    )
    app_b = RuntimePromptSnapshot(
        prompt_id="app-b.policy",
        app_id="app-b",
        layer="application",
        version="2.0.0",
        content="APP_B_POLICY",
    )
    registry = RuntimePromptRegistry()

    registry.activate(app_a)
    registry.activate(app_b)

    assert registry.snapshot(app_id="app-a") == app_a
    assert registry.snapshot(app_id="app-b") == app_b
    with pytest.raises(RuntimeError, match="application-scoped"):
        registry.snapshot()


@pytest.mark.asyncio
async def test_exact_prompt_loader_rejects_unpublished_version() -> None:
    service = PromptTemplateService(InMemoryPromptTemplateRepository())
    await service.create(
        prompt_id="full-view.agent-draft",
        app_id="full_information_view",
        layer="agent",
        name="draft",
        version="1.0.0",
        content="DRAFT_MUST_NOT_RUN",
        actor="admin",
        reason="test",
    )

    assert (
        await service.load_snapshot(
            prompt_id="full-view.agent-draft", version="1.0.0"
        )
        is None
    )


@pytest.mark.asyncio
async def test_historical_prompt_loader_accepts_disabled_but_not_never_published() -> None:
    service = PromptTemplateService(InMemoryPromptTemplateRepository())
    await _publish(
        service,
        prompt_id="full-view.application-history",
        layer="application",
        content="HISTORICAL_APPLICATION_POLICY",
    )
    published = await service.get_template(
        "full-view.application-history", "1.0.0"
    )
    assert published is not None
    await service.transition(
        prompt_id=published.prompt_id,
        version=published.version,
        to_status="disabled",
        expected_etag=published.etag,
        actor="admin",
        reason="superseded",
    )
    await service.create(
        prompt_id="full-view.never-published",
        app_id="full_information_view",
        layer="application",
        name="draft",
        version="1.0.0",
        content="DRAFT_MUST_NOT_RUN",
        actor="admin",
        reason="test",
    )

    historical = await service.load_historical_snapshot(
        prompt_id="full-view.application-history", version="1.0.0"
    )

    assert historical is not None
    assert historical.content == "HISTORICAL_APPLICATION_POLICY"
    assert (
        await service.load_historical_snapshot(
            prompt_id="full-view.never-published", version="1.0.0"
        )
        is None
    )


@pytest.mark.asyncio
async def test_disabled_application_and_agent_prompts_restore_for_old_run() -> None:
    prompt_service = PromptTemplateService(InMemoryPromptTemplateRepository())
    await _publish(
        prompt_service,
        prompt_id="full-view.application-old",
        layer="application",
        content="APPLICATION_OLD",
    )
    await _publish(
        prompt_service,
        prompt_id="full-view.agent-old",
        layer="agent",
        content="AGENT_OLD",
    )
    application_prompt = await prompt_service.load_snapshot(
        prompt_id="full-view.application-old", version="1.0.0"
    )
    assert application_prompt is not None
    store = InMemoryRunCapabilitySnapshotStore()
    applications = InMemoryApplicationRegistry(
        applications=[
            AgentApplicationDefinition(
                app_id="full_information_view",
                name="Full view",
                default_agent_id="governance_general_agent",
                identity_adapter_id="identity.legacy_geo",
            )
        ]
    )
    release = AgentReleaseSnapshot(
        release_id="release-old-prompt",
        app_id="full_information_view",
        agent_id="governance_general_agent",
        agent_version="1.0.0",
        prompt_ref="full-view.agent-old@1.0.0",
        model_refs=(
            AgentModelVersionRef(
                model_config_id="model-primary",
                config_version=1,
                role="primary",
                order=0,
            ),
        ),
        published_by="admin",
        reason="test",
    )
    first = RunCapabilitySnapshotService(
        InMemoryCapabilityRepository(),
        store=store,
        application_registry=applications,
        runtime_prompt_registry=RuntimePromptRegistry(application_prompt),
        prompt_snapshot_loader=prompt_service.load_snapshot,
        prompt_history_snapshot_loader=prompt_service.load_historical_snapshot,
    )
    await first.create_snapshot_for_run(
        "run-old-prompts",
        ToolRegistry.default(),
        app_id="full_information_view",
        agent_release=release,
    )
    for prompt_id in ("full-view.application-old", "full-view.agent-old"):
        current = await prompt_service.get_template(prompt_id, "1.0.0")
        assert current is not None
        await prompt_service.transition(
            prompt_id=prompt_id,
            version="1.0.0",
            to_status="disabled",
            expected_etag=current.etag,
            actor="admin",
            reason="superseded",
        )

    restarted = RunCapabilitySnapshotService(
        InMemoryCapabilityRepository(),
        store=store,
        application_registry=applications,
        prompt_snapshot_loader=prompt_service.load_snapshot,
        prompt_history_snapshot_loader=prompt_service.load_historical_snapshot,
    )
    restored = await restarted.create_snapshot_for_run(
        "run-old-prompts",
        ToolRegistry.default(),
        app_id="full_information_view",
    )

    assert restored.application_prompt_snapshot is not None
    assert restored.application_prompt_snapshot.content == "APPLICATION_OLD"
    assert restored.agent_prompt_snapshot is not None
    assert restored.agent_prompt_snapshot.content == "AGENT_OLD"


@pytest.mark.asyncio
async def test_old_run_rejects_same_version_prompt_content_drift() -> None:
    prompt_repository = InMemoryPromptTemplateRepository()
    prompt_service = PromptTemplateService(prompt_repository)
    await _publish(
        prompt_service,
        prompt_id="full-view.application-integrity",
        layer="application",
        content="ORIGINAL_CONTENT",
    )
    application_prompt = await prompt_service.load_snapshot(
        prompt_id="full-view.application-integrity", version="1.0.0"
    )
    assert application_prompt is not None
    store = InMemoryRunCapabilitySnapshotStore()
    first = RunCapabilitySnapshotService(
        InMemoryCapabilityRepository(),
        store=store,
        runtime_prompt_registry=RuntimePromptRegistry(application_prompt),
        prompt_snapshot_loader=prompt_service.load_snapshot,
        prompt_history_snapshot_loader=prompt_service.load_historical_snapshot,
    )
    await first.create_snapshot_for_run("run-prompt-integrity", ToolRegistry.default())
    persisted_prompt = await prompt_service.get_template(
        "full-view.application-integrity", "1.0.0"
    )
    assert persisted_prompt is not None
    await prompt_repository.save(
        persisted_prompt.model_copy(update={"content": "TAMPERED_CONTENT"})
    )

    restarted = RunCapabilitySnapshotService(
        InMemoryCapabilityRepository(),
        store=store,
        prompt_history_snapshot_loader=prompt_service.load_historical_snapshot,
    )
    with pytest.raises(RuntimeError, match="integrity validation"):
        await restarted.create_snapshot_for_run(
            "run-prompt-integrity", ToolRegistry.default()
        )


@pytest.mark.asyncio
async def test_context_layers_safety_application_agent_and_registry_capabilities() -> None:
    store = InMemoryAgentStore()
    sessions = SessionRunService(store)
    session = await sessions.create_session(user_id="user-01", title="prompt layers")
    run = await sessions.create_run(
        user_id="user-01", session_id=session.session_id, request=run_request()
    )
    auth = population_auth_context().model_copy(
        update={
            "session_id": session.session_id,
            "run_id": run.run_id,
            "entitlements": [
                *population_auth_context().entitlements,
                "governance.area.read",
            ],
            "data_scopes": population_auth_context().data_scopes.model_copy(
                update={
                    "datasets": [
                        *population_auth_context().data_scopes.datasets,
                        "administrative_area",
                    ]
                }
            ),
        }
    )
    default_registry = ToolRegistry.default()
    tool_id = "governance.query_population_metrics"
    contract = ToolSemanticContract.model_validate(
        {
            "schema_version": "1.0",
            "subject": "population",
            "metrics": [
                {
                    "metric_id": "person_count",
                    "label": "population",
                    "unit": "people",
                    "value_type": "integer",
                }
            ],
            "dimensions": [
                {
                    "dimension_id": "descendant_street",
                    "label": "street",
                    "kind": "administrative_area",
                }
            ],
            "filters": [],
            "operators": ["top"],
            "sort": {
                "allowed_fields": ["person_count"],
                "tie_policy": "include_all",
            },
            "completeness": {"mode": "complete", "statement": "all streets"},
            "output_forms": ["table"],
            "examples": [],
            "limitations": [],
            "query_shapes": [
                {
                    "shape_id": "street_top",
                    "metric_selection": ["person_count"],
                    "dimension_selection": ["descendant_street"],
                    "operator_selection": ["top"],
                    "scope_levels": [4],
                    "allowed_filters": [],
                    "output_forms": ["table"],
                    "completeness": {
                        "mode": "complete",
                        "statement": "all streets",
                    },
                    "result_schema_ref": "schema://population-ranking/1.0.0",
                    "result_row_fields": ["area_code", "person_count"],
                    "result_fingerprint_domain": "population-ranking",
                }
            ],
        }
    )
    registry = ToolRegistry(
        manifests=[
            default_registry.get_manifest(tool_id).model_copy(
                update={"semantic_contract": contract}
            )
        ],
        descriptors=[default_registry.get_model_descriptor(tool_id)],
    )
    builder = AgentContextBuilder(
        store=store,
        registry=registry,
        application_prompt_snapshot=RuntimePromptSnapshot(
            prompt_id="full-view.application-policy",
            app_id="full_information_view",
            layer="application",
            version="1.0.0",
            content="APPLICATION_POLICY_SENTINEL",
        ),
        agent_prompt_snapshot=RuntimePromptSnapshot(
            prompt_id="full-view.agent-instruction",
            app_id="full_information_view",
            layer="agent",
            version="2.0.0",
            content="AGENT_INSTRUCTION_SENTINEL",
        ),
    )

    request = await builder.build(
        user_id="user-01", auth_context=auth, state=HarnessState()
    )
    system_text = "\n".join(
        message.content or "" for message in request.messages if message.role == "system"
    )
    descriptor = registry.get_model_descriptor(
        tool_id
    ).description

    assert "RUNTIME_SAFETY_KERNEL" in system_text
    assert system_text.index("APPLICATION_POLICY_SENTINEL") < system_text.index(
        "AGENT_INSTRUCTION_SENTINEL"
    )
    assert descriptor in system_text
    assert "当前没有可用" not in system_text
    assert request.prompt_version.endswith(
        "+full-view.application-policy@1.0.0+full-view.agent-instruction@2.0.0"
    )


def test_runtime_safety_kernel_preserves_execution_and_evidence_guards() -> None:
    kernel = build_runtime_safety_kernel({})

    for required_guard in (
        "full_view.finish_answer",
        "reference_only",
        "upstream_timeout",
        "candidate_count=0",
        "resolve_area",
    ):
        assert required_guard in kernel


@pytest.mark.asyncio
async def test_context_advertises_authorized_non_semantic_tool_from_registry() -> None:
    store = InMemoryAgentStore()
    sessions = SessionRunService(store)
    session = await sessions.create_session(user_id="user-02", title="resolve")
    run = await sessions.create_run(
        user_id="user-02", session_id=session.session_id, request=run_request()
    )
    auth = population_auth_context().model_copy(
        update={
            "session_id": session.session_id,
            "run_id": run.run_id,
            "entitlements": [
                *population_auth_context().entitlements,
                "governance.area.read",
            ],
            "data_scopes": population_auth_context().data_scopes.model_copy(
                update={
                    "datasets": [
                        *population_auth_context().data_scopes.datasets,
                        "administrative_area",
                    ]
                }
            ),
        }
    )
    registry = ToolRegistry.default().subset({"governance.resolve_area"})

    request = await AgentContextBuilder(store=store, registry=registry).build(
        user_id="user-02", auth_context=auth, state=HarnessState()
    )

    assert registry.get_model_descriptor("governance.resolve_area").description in (
        request.messages[0].content or ""
    )
