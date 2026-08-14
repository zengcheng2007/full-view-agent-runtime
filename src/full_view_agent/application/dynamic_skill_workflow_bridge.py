"""Load published Skill and Workflow definitions into runtime-safe contracts.

The bridge deliberately stops at definition loading.  A workflow graph snapshot
is not an execution result and must be handed to a separate, controlled
executor before any node is run.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Literal

from pydantic import ConfigDict, Field

from full_view_agent.application.errors import ApplicationError
from full_view_agent.application.tool_registry import ToolRegistry
from full_view_agent.domain.capability import (
    CapabilityBase,
    SkillCapability,
    ToolCapability,
    WorkflowCapability,
    WorkflowNodeDefinition,
)
from full_view_agent.domain.contract_model import ContractModel

if TYPE_CHECKING:
    from full_view_agent.infrastructure.capability_repository import (
        CapabilityRepository,
    )


class RuntimeCapabilityDefinitionInvalid(ApplicationError, ValueError):
    """A published definition is not safe enough to enter the runtime."""

    code = "runtime_capability_definition_invalid"

    def __init__(self, capability_id: str, reason: str) -> None:
        super().__init__(f"{capability_id}: {reason}")
        self.capability_id = capability_id
        self.reason = reason


class RuntimeSkillContract(ContractModel):
    """Frozen model guidance and the exact tool allow-list it may use."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    skill_id: str = Field(min_length=1, max_length=128)
    version: str = Field(min_length=1, max_length=32)
    guidance: str = Field(min_length=1, max_length=5000)
    allowed_tool_ids: tuple[str, ...] = Field(min_length=1, max_length=50)
    applicable_questions: tuple[str, ...] = Field(default=(), max_length=50)


class RuntimeWorkflowNode(ContractModel):
    """Immutable workflow node definition; ``config_json`` is canonicalized."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    node_id: str = Field(min_length=1, max_length=64)
    node_type: Literal[
        "start",
        "tool",
        "skill",
        "condition",
        "join",
        "human_confirmation",
        "summary",
        "end",
    ]
    tool_ref: tuple[str, str] | None = None
    skill_ref: tuple[str, str] | None = None
    condition_expression: str | None = None
    config_json: str = "{}"


class RuntimeWorkflowEdge(ContractModel):
    """Immutable directed edge between two validated node identifiers."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_node_id: str = Field(min_length=1, max_length=64)
    target_node_id: str = Field(min_length=1, max_length=64)
    condition: str | None = None


class RuntimeWorkflowGraphSnapshot(ContractModel):
    """Validated immutable graph definition, not evidence that it ran."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    workflow_id: str = Field(min_length=1, max_length=128)
    version: str = Field(min_length=1, max_length=32)
    start_node_id: str = Field(min_length=1, max_length=64)
    end_node_ids: tuple[str, ...] = Field(min_length=1)
    nodes: tuple[RuntimeWorkflowNode, ...] = Field(min_length=2, max_length=50)
    edges: tuple[RuntimeWorkflowEdge, ...] = Field(default=(), max_length=100)
    timeout_seconds: int = Field(ge=10, le=3600)
    requires_human_confirmation: bool = False


class PublishedRuntimeCapabilityLoader:
    """Build runtime contracts exclusively from published repository entries."""

    def __init__(
        self,
        repository: CapabilityRepository,
        *,
        base_tool_registry: ToolRegistry | None = None,
    ) -> None:
        self._repository = repository
        self._base_tool_registry = base_tool_registry or ToolRegistry.default()

    async def load_skills(self) -> tuple[RuntimeSkillContract, ...]:
        capabilities = await self._repository.list_capabilities(
            capability_type="skill",
            status="published",
        )
        skills = (
            capability
            for capability in capabilities
            if isinstance(capability, SkillCapability)
            and capability.status == "published"
        )
        return tuple(_to_runtime_skill(skill) for skill in skills)

    async def load_workflows(self) -> tuple[RuntimeWorkflowGraphSnapshot, ...]:
        capabilities, tool_capabilities = await _load_workflows_and_tools(
            self._repository
        )
        skill_capabilities = await self._repository.list_capabilities(
            capability_type="skill", status="published"
        )
        allowed_tool_refs = {
            (
                tool_id,
                self._base_tool_registry.get_manifest(tool_id).tool_version,
            )
            for tool_id in self._base_tool_registry.list_tool_ids()
        }
        allowed_tool_refs.update(
            (tool.capability_id, tool.version)
            for tool in tool_capabilities
            if isinstance(tool, ToolCapability) and tool.status == "published"
        )
        allowed_skill_refs = {
            (skill.capability_id, skill.version)
            for skill in skill_capabilities
            if isinstance(skill, SkillCapability) and skill.status == "published"
        }
        workflows = (
            capability
            for capability in capabilities
            if isinstance(capability, WorkflowCapability)
            and capability.status == "published"
        )
        return tuple(
            _to_runtime_workflow(
                workflow,
                allowed_tool_refs=allowed_tool_refs,
                allowed_skill_refs=allowed_skill_refs,
            )
            for workflow in workflows
        )


async def _load_workflows_and_tools(
    repository: CapabilityRepository,
) -> tuple[list[CapabilityBase], list[CapabilityBase]]:
    workflows = await repository.list_capabilities(
        capability_type="workflow",
        status="published",
    )
    tools = await repository.list_capabilities(
        capability_type="tool",
        status="published",
    )
    return list(workflows), list(tools)


def _to_runtime_skill(skill: SkillCapability) -> RuntimeSkillContract:
    guidance = skill.guidance.strip()
    if not guidance:
        raise RuntimeCapabilityDefinitionInvalid(
            skill.capability_id,
            "published skill must provide runtime guidance",
        )
    if not skill.allowed_tool_ids:
        raise RuntimeCapabilityDefinitionInvalid(
            skill.capability_id,
            "published skill must provide allowed_tool_ids",
        )
    return RuntimeSkillContract(
        skill_id=skill.capability_id,
        version=skill.version,
        guidance=guidance,
        allowed_tool_ids=tuple(skill.allowed_tool_ids),
        applicable_questions=tuple(skill.applicable_questions),
    )


def build_runtime_skill_contract(skill: SkillCapability) -> RuntimeSkillContract:
    """Rebuild an exact published-or-pinned Skill version for a Run."""

    return _to_runtime_skill(skill)


def _to_runtime_workflow(
    workflow: WorkflowCapability,
    *,
    allowed_tool_refs: set[tuple[str, str]],
    allowed_skill_refs: set[tuple[str, str]] | None = None,
) -> RuntimeWorkflowGraphSnapshot:
    node_by_id: dict[str, WorkflowNodeDefinition] = {}
    for node in workflow.nodes:
        if node.node_id in node_by_id:
            raise RuntimeCapabilityDefinitionInvalid(
                workflow.capability_id,
                f"duplicate node_id: {node.node_id}",
            )
        node_by_id[node.node_id] = node

    start_ids = tuple(
        node.node_id for node in workflow.nodes if node.node_type == "start"
    )
    if len(start_ids) != 1:
        raise RuntimeCapabilityDefinitionInvalid(
            workflow.capability_id,
            "workflow must define exactly one start node",
        )
    end_ids = tuple(
        node.node_id for node in workflow.nodes if node.node_type == "end"
    )
    if not end_ids:
        raise RuntimeCapabilityDefinitionInvalid(
            workflow.capability_id,
            "workflow must define at least one end node",
        )

    runtime_nodes = tuple(
        _to_runtime_workflow_node(
            workflow.capability_id,
            node,
            allowed_tool_refs=allowed_tool_refs,
            allowed_skill_refs=allowed_skill_refs or set(),
        )
        for node in workflow.nodes
    )
    runtime_edges: list[RuntimeWorkflowEdge] = []
    for edge in workflow.edges:
        if edge.source_node_id not in node_by_id:
            raise RuntimeCapabilityDefinitionInvalid(
                workflow.capability_id,
                f"edge references unknown source node: {edge.source_node_id}",
            )
        if edge.target_node_id not in node_by_id:
            raise RuntimeCapabilityDefinitionInvalid(
                workflow.capability_id,
                f"edge references unknown target node: {edge.target_node_id}",
            )
        runtime_edges.append(
            RuntimeWorkflowEdge(
                source_node_id=edge.source_node_id,
                target_node_id=edge.target_node_id,
                condition=edge.condition,
            )
        )

    _validate_graph_reachability(
        workflow.capability_id,
        start_id=start_ids[0],
        node_ids=set(node_by_id),
        edges=tuple(runtime_edges),
    )

    return RuntimeWorkflowGraphSnapshot(
        workflow_id=workflow.capability_id,
        version=workflow.version,
        start_node_id=start_ids[0],
        end_node_ids=end_ids,
        nodes=runtime_nodes,
        edges=tuple(runtime_edges),
        timeout_seconds=workflow.timeout_seconds,
        requires_human_confirmation=workflow.requires_human_confirmation,
    )


def _validate_graph_reachability(
    workflow_id: str,
    *,
    start_id: str,
    node_ids: set[str],
    edges: tuple[RuntimeWorkflowEdge, ...],
) -> None:
    outgoing: dict[str, list[str]] = {node_id: [] for node_id in node_ids}
    for edge in edges:
        outgoing[edge.source_node_id].append(edge.target_node_id)
    visited: set[str] = set()
    active: set[str] = set()

    def visit(node_id: str) -> None:
        if node_id in active:
            raise RuntimeCapabilityDefinitionInvalid(
                workflow_id, "workflow graph contains a cycle"
            )
        if node_id in visited:
            return
        active.add(node_id)
        for target in outgoing[node_id]:
            visit(target)
        active.remove(node_id)
        visited.add(node_id)

    visit(start_id)
    unreachable = sorted(node_ids - visited)
    if unreachable:
        raise RuntimeCapabilityDefinitionInvalid(
            workflow_id,
            "workflow graph contains unreachable nodes: " + ", ".join(unreachable),
        )


def build_runtime_workflow_snapshot(
    workflow: WorkflowCapability,
    *,
    allowed_tool_refs: set[tuple[str, str]],
    allowed_skill_refs: set[tuple[str, str]] | None = None,
) -> RuntimeWorkflowGraphSnapshot:
    """Rebuild an exact published-or-pinned Workflow version for a Run."""

    return _to_runtime_workflow(
        workflow,
        allowed_tool_refs=allowed_tool_refs,
        allowed_skill_refs=allowed_skill_refs or set(),
    )


def _to_runtime_workflow_node(
    workflow_id: str,
    node: WorkflowNodeDefinition,
    *,
    allowed_tool_refs: set[tuple[str, str]],
    allowed_skill_refs: set[tuple[str, str]],
) -> RuntimeWorkflowNode:
    tool_ref: tuple[str, str] | None = None
    if node.node_type == "tool":
        if not node.tool_capability_id or not node.tool_version:
            raise RuntimeCapabilityDefinitionInvalid(
                workflow_id,
                f"tool node {node.node_id} requires tool_capability_id and tool_version",
            )
        tool_ref = (node.tool_capability_id, node.tool_version)
        if tool_ref not in allowed_tool_refs:
            raise RuntimeCapabilityDefinitionInvalid(
                workflow_id,
                f"tool node {node.node_id} must reference a published tool version",
            )
    skill_ref: tuple[str, str] | None = None
    if node.node_type == "skill":
        if not node.skill_capability_id or not node.skill_version:
            raise RuntimeCapabilityDefinitionInvalid(
                workflow_id,
                f"skill node {node.node_id} requires skill_capability_id and skill_version",
            )
        skill_ref = (node.skill_capability_id, node.skill_version)
        if skill_ref not in allowed_skill_refs:
            raise RuntimeCapabilityDefinitionInvalid(
                workflow_id,
                f"skill node {node.node_id} must reference a published skill version",
            )

    return RuntimeWorkflowNode(
        node_id=node.node_id,
        node_type=node.node_type,
        tool_ref=tool_ref,
        skill_ref=skill_ref,
        condition_expression=node.condition_expression,
        config_json=json.dumps(
            node.config,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
    )
