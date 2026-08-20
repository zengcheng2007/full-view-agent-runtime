from __future__ import annotations

import pytest
from pydantic import ValidationError

from full_view_agent.api.app import RuntimeContainer
from full_view_agent.application.dynamic_skill_workflow_bridge import (
    PublishedRuntimeCapabilityLoader,
    RuntimeCapabilityDefinitionInvalid,
)
from full_view_agent.domain.capability import (
    CapabilityStatus,
    SkillCapability,
    ToolCapability,
    WorkflowCapability,
    WorkflowEdgeDefinition,
    WorkflowNodeDefinition,
)
from full_view_agent.domain.models import WorkflowRef
from full_view_agent.infrastructure.capability_repository import (
    InMemoryCapabilityRepository,
)
from full_view_agent.infrastructure.credential_broker import InMemoryCredentialBroker


def _skill(
    *,
    capability_id: str,
    status: CapabilityStatus,
    guidance: str = "Use the area tool",
) -> SkillCapability:
    return SkillCapability(
        capability_id=capability_id,
        name=capability_id,
        owner="test",
        version="1.0.0",
        status=status,
        guidance=guidance,
        allowed_tool_ids=["tool.resolve_area"],
    )


def _workflow(
    *,
    capability_id: str = "workflow.area_review",
    status: CapabilityStatus = "published",
    nodes: list[WorkflowNodeDefinition] | None = None,
    edges: list[WorkflowEdgeDefinition] | None = None,
) -> WorkflowCapability:
    return WorkflowCapability(
        capability_id=capability_id,
        name=capability_id,
        owner="test",
        version="1.0.0",
        status=status,
        guidance="Test workflow guidance",
        nodes=nodes
        or [
            WorkflowNodeDefinition(node_id="start", node_type="start"),
            WorkflowNodeDefinition(
                node_id="resolve",
                node_type="tool",
                tool_capability_id="tool.resolve_area",
                tool_version="1.0.0",
                config={"query_from": "message"},
            ),
            WorkflowNodeDefinition(node_id="end", node_type="end"),
        ],
        edges=edges
        or [
            WorkflowEdgeDefinition(source_node_id="start", target_node_id="resolve"),
            WorkflowEdgeDefinition(source_node_id="resolve", target_node_id="end"),
        ],
    )


def _tool(*, status: CapabilityStatus = "published") -> ToolCapability:
    return ToolCapability(
        capability_id="tool.resolve_area",
        name="Resolve area",
        owner="test",
        version="1.0.0",
        status=status,
        guidance="Test guidance for Resolve area",
        connector_ref="geo-qxst",
        resource_path="/resolve-area",
    )


@pytest.mark.asyncio
async def test_loader_returns_only_published_skills_as_frozen_runtime_contracts() -> None:
    repository = InMemoryCapabilityRepository()
    await repository.save_skill(_skill(capability_id="skill.area", status="published"))
    await repository.save_skill(_skill(capability_id="skill.draft", status="draft"))

    contracts = await PublishedRuntimeCapabilityLoader(repository).load_skills()

    assert len(contracts) == 1
    assert contracts[0].skill_id == "skill.area"
    assert contracts[0].guidance == "Use the area tool"
    assert contracts[0].allowed_tool_ids == ("tool.resolve_area",)
    with pytest.raises(ValidationError):
        contracts[0].guidance = "changed"


@pytest.mark.asyncio
async def test_loader_rejects_published_skill_without_runtime_guidance() -> None:
    with pytest.raises(ValidationError):
        SkillCapability(
            capability_id="skill.empty",
            name="skill.empty",
            owner="test",
            version="1.0.0",
            status="published",
            guidance="",
            allowed_tool_ids=["tool.resolve_area"],
        )


@pytest.mark.asyncio
async def test_loader_returns_only_published_workflows_as_immutable_graph_snapshots() -> None:
    repository = InMemoryCapabilityRepository()
    await repository.save_tool(_tool())
    await repository.save_workflow(_workflow())
    await repository.save_workflow(
        _workflow(capability_id="workflow.draft", status="draft")
    )

    snapshots = await PublishedRuntimeCapabilityLoader(repository).load_workflows()

    assert len(snapshots) == 1
    graph = snapshots[0]
    assert graph.workflow_id == "workflow.area_review"
    assert graph.start_node_id == "start"
    assert graph.end_node_ids == ("end",)
    assert graph.nodes[1].tool_ref == ("tool.resolve_area", "1.0.0")
    assert graph.nodes[1].config_json == '{"query_from":"message"}'
    with pytest.raises(ValidationError):
        graph.start_node_id = "changed"


@pytest.mark.asyncio
async def test_loader_rejects_workflow_reference_to_unpublished_tool() -> None:
    repository = InMemoryCapabilityRepository()
    await repository.save_tool(_tool(status="draft"))
    await repository.save_workflow(_workflow())

    with pytest.raises(RuntimeCapabilityDefinitionInvalid, match="published tool"):
        await PublishedRuntimeCapabilityLoader(repository).load_workflows()


@pytest.mark.asyncio
async def test_loader_pins_workflow_skill_node_to_published_skill_version() -> None:
    repository = InMemoryCapabilityRepository()
    await repository.save_skill(_skill(capability_id="skill.area", status="published"))
    await repository.save_workflow(
        _workflow(
            nodes=[
                WorkflowNodeDefinition(node_id="start", node_type="start"),
                WorkflowNodeDefinition(
                    node_id="analyse",
                    node_type="skill",
                    skill_capability_id="skill.area",
                    skill_version="1.0.0",
                    config={
                        "tool_id": "tool.resolve_area",
                        "arguments": {"query": "西湖区"},
                    },
                ),
                WorkflowNodeDefinition(node_id="end", node_type="end"),
            ],
            edges=[
                WorkflowEdgeDefinition(source_node_id="start", target_node_id="analyse"),
                WorkflowEdgeDefinition(source_node_id="analyse", target_node_id="end"),
            ],
        )
    )

    snapshots = await PublishedRuntimeCapabilityLoader(repository).load_workflows()

    assert snapshots[0].nodes[1].skill_ref == ("skill.area", "1.0.0")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("nodes", "edges", "message"),
    [
        (
            [WorkflowNodeDefinition(node_id="end", node_type="end")],
            [],
            "exactly one start",
        ),
        (
            [WorkflowNodeDefinition(node_id="start", node_type="start")],
            [],
            "at least one end",
        ),
        (
            [
                WorkflowNodeDefinition(node_id="start", node_type="start"),
                WorkflowNodeDefinition(node_id="end", node_type="end"),
            ],
            [WorkflowEdgeDefinition(source_node_id="start", target_node_id="missing")],
            "unknown target node",
        ),
        (
            [
                WorkflowNodeDefinition(node_id="start", node_type="start"),
                WorkflowNodeDefinition(
                    node_id="tool",
                    node_type="tool",
                    tool_capability_id="tool.resolve_area",
                ),
                WorkflowNodeDefinition(node_id="end", node_type="end"),
            ],
            [
                WorkflowEdgeDefinition(source_node_id="start", target_node_id="tool"),
                WorkflowEdgeDefinition(source_node_id="tool", target_node_id="end"),
            ],
            "tool_version",
        ),
    ],
)
async def test_loader_rejects_workflow_graphs_that_are_not_runtime_safe(
    nodes: list[WorkflowNodeDefinition],
    edges: list[WorkflowEdgeDefinition],
    message: str,
) -> None:
    repository = InMemoryCapabilityRepository()
    await repository.save_workflow(_workflow(nodes=nodes, edges=edges))

    with pytest.raises(RuntimeCapabilityDefinitionInvalid, match=message):
        await PublishedRuntimeCapabilityLoader(repository).load_workflows()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("edges", "message"),
    [
        (
            [
                WorkflowEdgeDefinition(source_node_id="start", target_node_id="end"),
            ],
            "unreachable",
        ),
        (
            [
                WorkflowEdgeDefinition(source_node_id="start", target_node_id="resolve"),
                WorkflowEdgeDefinition(source_node_id="resolve", target_node_id="resolve"),
                WorkflowEdgeDefinition(source_node_id="resolve", target_node_id="end"),
            ],
            "cycle",
        ),
    ],
)
async def test_loader_rejects_unreachable_nodes_and_cycles(
    edges: list[WorkflowEdgeDefinition], message: str
) -> None:
    repository = InMemoryCapabilityRepository()
    await repository.save_tool(_tool())
    await repository.save_workflow(_workflow(edges=edges))

    with pytest.raises(RuntimeCapabilityDefinitionInvalid, match=message):
        await PublishedRuntimeCapabilityLoader(repository).load_workflows()


@pytest.mark.asyncio
async def test_runtime_initialize_loads_published_skill_and_workflow_contracts() -> None:
    runtime = RuntimeContainer(credentials=InMemoryCredentialBroker())
    assert runtime.capability_repository is not None
    await runtime.capability_repository.save_tool(_tool())
    await runtime.capability_repository.save_skill(
        _skill(capability_id="skill.area", status="published")
    )
    await runtime.capability_repository.save_workflow(
        _workflow(
            nodes=[
                WorkflowNodeDefinition(node_id="start", node_type="start"),
                WorkflowNodeDefinition(
                    node_id="resolve",
                    node_type="tool",
                    tool_capability_id="tool.resolve_area",
                    tool_version="1.0.0",
                    config={"arguments": {}},
                ),
                WorkflowNodeDefinition(node_id="end", node_type="end"),
            ]
        )
    )

    await runtime.initialize()

    assert [item.skill_id for item in runtime.runtime_skills] == ["skill.area"]
    assert [item.workflow_id for item in runtime.runtime_workflows] == [
        "workflow.area_review"
    ]
    assert [
        item.skill_id for item in runtime.runtime_skill_registry.list()
    ] == ["skill.area"]
    assert runtime.runtime_workflow_registry.get(
        WorkflowRef(
            workflow_id="workflow.area_review",
            workflow_version="1.0.0",
        )
    ).workflow_id == "workflow.area_review"
