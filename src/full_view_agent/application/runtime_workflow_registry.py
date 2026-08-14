"""Deterministic execution support for the first published Workflow slice."""

from __future__ import annotations

import json
import re
from collections import deque
from threading import RLock

from full_view_agent.application.answer_claims import (
    DENIAL_SUMMARY,
    FAILURE_SUMMARY,
    REFERENCE_ONLY_SUMMARY,
    StructuredFinish,
)
from full_view_agent.application.dynamic_skill_workflow_bridge import (
    RuntimeWorkflowEdge,
    RuntimeWorkflowGraphSnapshot,
    RuntimeWorkflowNode,
)
from full_view_agent.application.errors import WorkflowNotAvailable
from full_view_agent.application.harness import FinishAction, HarnessState, ToolAction
from full_view_agent.application.runtime_skill_registry import RuntimeSkillRegistry
from full_view_agent.application.tool_registry import ToolRegistry
from full_view_agent.domain.models import ToolResult, WorkflowRef

_CONDITION_EXPRESSION = re.compile(
    r'^result\.([A-Za-z0-9_-]{1,64})\.status\s*(==|!=)\s*'
    r'"(success|partial)"$'
)


class RuntimeWorkflowRegistry:
    """Live published definitions with explicit immutable snapshots."""

    def __init__(
        self, workflows: tuple[RuntimeWorkflowGraphSnapshot, ...] = ()
    ) -> None:
        self._lock = RLock()
        self._workflows = _validated_workflows(workflows)

    def replace(self, workflows: tuple[RuntimeWorkflowGraphSnapshot, ...]) -> None:
        validated = _validated_workflows(workflows)
        with self._lock:
            self._workflows = validated

    def bind_lock(self, lock: RLock) -> None:
        """Join the runtime's composite capability generation lock."""

        with self._lock:
            self._lock = lock

    def snapshot(self) -> RuntimeWorkflowRegistry:
        with self._lock:
            return RuntimeWorkflowRegistry(tuple(self._workflows.values()))

    def list(self) -> tuple[RuntimeWorkflowGraphSnapshot, ...]:
        with self._lock:
            return tuple(self._workflows.values())

    def get(self, workflow_ref: WorkflowRef) -> RuntimeWorkflowGraphSnapshot:
        with self._lock:
            workflow = self._workflows.get(
                (workflow_ref.workflow_id, workflow_ref.workflow_version)
            )
        if workflow is None:
            raise WorkflowNotAvailable("workflow is not published in this runtime")
        return workflow

    def create_planner(
        self,
        workflow_ref: WorkflowRef,
        *,
        tool_registry: ToolRegistry | None = None,
        skill_registry: RuntimeSkillRegistry | None = None,
    ) -> LinearWorkflowPlanner:
        return LinearWorkflowPlanner(
            self.get(workflow_ref),
            tool_registry=tool_registry,
            skill_registry=skill_registry,
        )


class LinearWorkflowPlanner:
    """Execute a validated deterministic workflow without model decisions.

    The compatibility name is retained for callers.  The planner supports a
    linear path or a controlled two-way condition whose selected branch merges
    at a selected-branch join.  It deliberately does not claim parallel execution.
    """

    def __init__(
        self,
        workflow: RuntimeWorkflowGraphSnapshot,
        *,
        tool_registry: ToolRegistry | None = None,
        skill_registry: RuntimeSkillRegistry | None = None,
    ) -> None:
        ordered, outgoing = _validated_nodes(workflow)
        self._workflow = workflow
        self._nodes = {node.node_id: node for node in workflow.nodes}
        self._outgoing = outgoing
        self._actions = {
            node.node_id: _node_action(
                workflow.workflow_id,
                node,
                tool_registry=tool_registry,
                skill_registry=skill_registry,
            )
            for node in ordered
            if node.node_type in {"tool", "skill"}
        }
        self._tools = tuple(
            self._actions[node.node_id]
            for node in ordered
            if node.node_id in self._actions
        )
        if not self._tools:
            raise WorkflowNotAvailable("workflow requires at least one tool or skill node")

    async def decide(self, state: HarnessState) -> ToolAction | FinishAction:
        if state.tool_results:
            latest = state.tool_results[-1]
            if latest.status == "denied":
                return FinishAction(
                    summary=DENIAL_SUMMARY,
                    structured_finish=StructuredFinish(
                        kind="denial", summary=DENIAL_SUMMARY
                    ),
                    server_authored=True,
                )
            if latest.status == "failed":
                return FinishAction(
                    summary=FAILURE_SUMMARY,
                    structured_finish=StructuredFinish(
                        kind="failure", summary=FAILURE_SUMMARY
                    ),
                    server_authored=True,
                )
        action, summary = self._advance(state.tool_results)
        if action is not None:
            return action
        return FinishAction(
            summary=summary,
            structured_finish=StructuredFinish(
                kind="reference_only",
                summary=summary,
            ),
            server_authored=True,
        )

    def _advance(
        self, tool_results: tuple[ToolResult, ...]
    ) -> tuple[ToolAction | None, str]:
        queue = deque((self._workflow.start_node_id,))
        activated: set[str] = set()
        result_by_node: dict[str, ToolResult] = {}
        result_index = 0
        summary = REFERENCE_ONLY_SUMMARY

        while queue:
            node_id = queue.popleft()
            if node_id in activated:
                continue
            activated.add(node_id)
            node = self._nodes[node_id]

            if node.node_type in {"tool", "skill"}:
                if result_index >= len(tool_results):
                    return self._actions[node_id], summary
                result_by_node[node_id] = tool_results[result_index]
                result_index += 1
            elif node.node_type == "condition":
                selected = _evaluate_condition(node, result_by_node)
                edge = next(
                    edge
                    for edge in self._outgoing[node_id]
                    if edge.condition == ("true" if selected else "false")
                )
                queue.append(edge.target_node_id)
                continue
            elif node.node_type == "summary":
                summary = _summary_text(node)

            for edge in self._outgoing[node_id]:
                queue.append(edge.target_node_id)

        if result_index != len(tool_results):
            raise WorkflowNotAvailable(
                "workflow state contains results outside the selected branch"
            )
        return None, summary


def _validated_workflows(
    workflows: tuple[RuntimeWorkflowGraphSnapshot, ...]
) -> dict[tuple[str, str], RuntimeWorkflowGraphSnapshot]:
    result: dict[tuple[str, str], RuntimeWorkflowGraphSnapshot] = {}
    for workflow in workflows:
        identity = (workflow.workflow_id, workflow.version)
        if identity in result:
            raise ValueError(
                f"duplicate runtime workflow: {workflow.workflow_id}@{workflow.version}"
            )
        result[identity] = workflow
    return result


def _validated_nodes(
    workflow: RuntimeWorkflowGraphSnapshot,
) -> tuple[
    tuple[RuntimeWorkflowNode, ...],
    dict[str, tuple[RuntimeWorkflowEdge, ...]],
]:
    if workflow.requires_human_confirmation:
        raise WorkflowNotAvailable(
            "workflow runtime does not support human confirmation"
        )
    nodes: dict[str, RuntimeWorkflowNode] = {}
    for node in workflow.nodes:
        if node.node_id in nodes:
            raise WorkflowNotAvailable(f"duplicate workflow node: {node.node_id}")
        nodes[node.node_id] = node
    if workflow.start_node_id not in nodes or nodes[workflow.start_node_id].node_type != "start":
        raise WorkflowNotAvailable("workflow start node is invalid")
    actual_end_ids = {
        node.node_id for node in workflow.nodes if node.node_type == "end"
    }
    if actual_end_ids != set(workflow.end_node_ids):
        raise WorkflowNotAvailable("workflow end nodes do not match end_node_ids")

    outgoing_lists: dict[str, list[RuntimeWorkflowEdge]] = {
        node_id: [] for node_id in nodes
    }
    incoming_lists: dict[str, list[RuntimeWorkflowEdge]] = {
        node_id: [] for node_id in nodes
    }
    seen_edges: set[tuple[str, str]] = set()
    for edge in workflow.edges:
        if edge.source_node_id not in nodes or edge.target_node_id not in nodes:
            raise WorkflowNotAvailable("workflow edge references an unknown node")
        identity = (edge.source_node_id, edge.target_node_id)
        if identity in seen_edges:
            raise WorkflowNotAvailable("duplicate edge in workflow graph")
        seen_edges.add(identity)
        outgoing_lists[edge.source_node_id].append(edge)
        incoming_lists[edge.target_node_id].append(edge)

    ordered_ids = _topological_order(nodes, outgoing_lists, incoming_lists)
    reachable: set[str] = set()
    queue = deque((workflow.start_node_id,))
    while queue:
        node_id = queue.popleft()
        if node_id in reachable:
            continue
        reachable.add(node_id)
        queue.extend(edge.target_node_id for edge in outgoing_lists[node_id])
    unreachable = sorted(set(nodes) - reachable)
    if unreachable:
        raise WorkflowNotAvailable(
            "workflow graph contains unreachable nodes: " + ", ".join(unreachable)
        )

    for node_id, node in nodes.items():
        outgoing = outgoing_lists[node_id]
        incoming = incoming_lists[node_id]
        if node.node_type == "condition":
            match = _CONDITION_EXPRESSION.fullmatch(node.condition_expression or "")
            if match is None:
                raise WorkflowNotAvailable("invalid condition expression")
            if len(incoming) != 1:
                raise WorkflowNotAvailable("condition node requires exactly one predecessor")
            predecessor_id = incoming[0].source_node_id
            if (
                match.group(1) != predecessor_id
                or nodes[predecessor_id].node_type not in {"tool", "skill"}
            ):
                raise WorkflowNotAvailable(
                    "condition expression must reference its tool or skill predecessor"
                )
            labels = {edge.condition for edge in outgoing}
            if len(outgoing) != 2 or labels != {"true", "false"}:
                raise WorkflowNotAvailable(
                    "condition node requires exactly true and false edges"
                )
            join_ids = {
                _first_join_id(edge.target_node_id, nodes, outgoing_lists)
                for edge in outgoing
            }
            if None in join_ids or len(join_ids) != 1:
                raise WorkflowNotAvailable(
                    "condition branches must merge at the same join node"
                )
        else:
            if any(edge.condition is not None for edge in outgoing):
                raise WorkflowNotAvailable(
                    "edge conditions are allowed only on condition nodes"
                )
            if len(outgoing) > 1:
                raise WorkflowNotAvailable("linear workflow cannot branch")

        if node.node_type == "join":
            if len(incoming) < 2 or len(outgoing) != 1:
                raise WorkflowNotAvailable(
                    "join node requires at least two inputs and exactly one output"
                )
            try:
                join_config = json.loads(node.config_json)
            except json.JSONDecodeError as exc:
                raise WorkflowNotAvailable("workflow join config is invalid JSON") from exc
            if join_config not in ({}, {"mode": "selected"}):
                raise WorkflowNotAvailable(
                    "join node supports only selected-branch merge mode"
                )
        if node.node_type == "start" and incoming:
            raise WorkflowNotAvailable("workflow start node cannot have incoming edges")
        if node.node_type == "end" and outgoing:
            raise WorkflowNotAvailable("workflow end node cannot have outgoing edges")
        if node.node_type == "human_confirmation":
            raise WorkflowNotAvailable(
                "workflow runtime does not support human confirmation"
            )

    return (
        tuple(nodes[node_id] for node_id in ordered_ids),
        {node_id: tuple(edges) for node_id, edges in outgoing_lists.items()},
    )


def _topological_order(
    nodes: dict[str, RuntimeWorkflowNode],
    outgoing: dict[str, list[RuntimeWorkflowEdge]],
    incoming: dict[str, list[RuntimeWorkflowEdge]],
) -> tuple[str, ...]:
    remaining = {node_id: len(edges) for node_id, edges in incoming.items()}
    queue = deque(node_id for node_id in nodes if remaining[node_id] == 0)
    ordered: list[str] = []
    while queue:
        node_id = queue.popleft()
        ordered.append(node_id)
        for edge in outgoing[node_id]:
            remaining[edge.target_node_id] -= 1
            if remaining[edge.target_node_id] == 0:
                queue.append(edge.target_node_id)
    if len(ordered) != len(nodes):
        raise WorkflowNotAvailable("workflow graph contains a cycle")
    return tuple(ordered)


def _first_join_id(
    start_node_id: str,
    nodes: dict[str, RuntimeWorkflowNode],
    outgoing: dict[str, list[RuntimeWorkflowEdge]],
) -> str | None:
    current = start_node_id
    while nodes[current].node_type != "join":
        edges = outgoing[current]
        if len(edges) != 1:
            return None
        current = edges[0].target_node_id
    return current


def _evaluate_condition(
    node: RuntimeWorkflowNode,
    result_by_node: dict[str, ToolResult],
) -> bool:
    match = _CONDITION_EXPRESSION.fullmatch(node.condition_expression or "")
    if match is None:
        raise WorkflowNotAvailable("invalid condition expression")
    source_node_id, operator, expected_status = match.groups()
    result = result_by_node.get(source_node_id)
    if result is None:
        raise WorkflowNotAvailable("condition predecessor result is unavailable")
    matches = result.status == expected_status
    return matches if operator == "==" else not matches


def _tool_action(
    workflow_id: str,
    node: RuntimeWorkflowNode,
    *,
    tool_registry: ToolRegistry | None,
) -> ToolAction:
    if node.tool_ref is None:
        raise WorkflowNotAvailable(f"workflow {workflow_id} tool node has no Tool ref")
    tool_id, tool_version = node.tool_ref
    if tool_registry is not None:
        try:
            manifest = tool_registry.get_manifest(tool_id)
        except KeyError as exc:
            raise WorkflowNotAvailable(
                f"workflow Tool is unavailable in this Run: {tool_id}"
            ) from exc
        if manifest.tool_version != tool_version:
            raise WorkflowNotAvailable(
                f"workflow Tool version is unavailable in this Run: {tool_id}@{tool_version}"
            )
    try:
        config = json.loads(node.config_json)
    except json.JSONDecodeError as exc:
        raise WorkflowNotAvailable("workflow tool config is invalid JSON") from exc
    arguments = config.get("arguments") if isinstance(config, dict) else None
    if not isinstance(arguments, dict):
        raise WorkflowNotAvailable("workflow tool node requires object arguments")
    return ToolAction(tool_id=tool_id, arguments=arguments)


def _node_action(
    workflow_id: str,
    node: RuntimeWorkflowNode,
    *,
    tool_registry: ToolRegistry | None,
    skill_registry: RuntimeSkillRegistry | None,
) -> ToolAction:
    if node.node_type == "tool":
        return _tool_action(workflow_id, node, tool_registry=tool_registry)
    if node.skill_ref is None or skill_registry is None:
        raise WorkflowNotAvailable(
            f"workflow {workflow_id} skill node has no pinned Skill ref"
        )
    try:
        skill = skill_registry.get(*node.skill_ref)
    except KeyError as exc:
        raise WorkflowNotAvailable(str(exc)) from exc
    try:
        config = json.loads(node.config_json)
    except json.JSONDecodeError as exc:
        raise WorkflowNotAvailable("workflow skill config is invalid JSON") from exc
    tool_id = config.get("tool_id") if isinstance(config, dict) else None
    arguments = config.get("arguments") if isinstance(config, dict) else None
    if tool_id not in skill.allowed_tool_ids:
        raise WorkflowNotAvailable(
            f"workflow Skill cannot invoke Tool outside its allow-list: {tool_id}"
        )
    if not isinstance(arguments, dict):
        raise WorkflowNotAvailable("workflow skill node requires object arguments")
    tool_version = (
        tool_registry.get_manifest(tool_id).tool_version
        if tool_registry is not None
        else "1.0.0"
    )
    action_node = node.model_copy(update={"tool_ref": (tool_id, tool_version)})
    action_node = action_node.model_copy(
        update={"config_json": json.dumps({"arguments": arguments})}
    )
    return _tool_action(workflow_id, action_node, tool_registry=tool_registry)


def _summary_text(node: RuntimeWorkflowNode) -> str:
    try:
        config = json.loads(node.config_json)
    except json.JSONDecodeError as exc:
        raise WorkflowNotAvailable("workflow summary config is invalid JSON") from exc
    text = config.get("text") if isinstance(config, dict) else None
    return text.strip() if isinstance(text, str) and text.strip() else REFERENCE_ONLY_SUMMARY
