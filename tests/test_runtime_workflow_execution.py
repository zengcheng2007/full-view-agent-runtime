import json
from datetime import UTC, datetime
from typing import Literal

import pytest

from full_view_agent.application.capability_service import CapabilityService
from full_view_agent.application.dynamic_skill_workflow_bridge import (
    RuntimeSkillContract,
    RuntimeWorkflowEdge,
    RuntimeWorkflowGraphSnapshot,
    RuntimeWorkflowNode,
)
from full_view_agent.application.errors import ReauthenticationRequired, WorkflowNotAvailable
from full_view_agent.application.harness import (
    AgentHarness,
    FinishAction,
    HarnessState,
    ToolAction,
)
from full_view_agent.application.native_orchestrator import NativeOrchestrator
from full_view_agent.application.policy import MinimalPolicyAdapter
from full_view_agent.application.run_capability_snapshot import (
    RunCapabilitySnapshot,
    RunCapabilitySnapshotService,
)
from full_view_agent.application.run_capability_snapshot_store import (
    InMemoryRunCapabilitySnapshotStore,
)
from full_view_agent.application.runtime_skill_registry import RuntimeSkillRegistry
from full_view_agent.application.runtime_workflow_registry import (
    LinearWorkflowPlanner,
    RuntimeWorkflowRegistry,
)
from full_view_agent.application.session_run_service import SessionRunService
from full_view_agent.application.tool_registry import ToolRegistry
from full_view_agent.domain.capability import (
    WorkflowCapability,
    WorkflowEdgeDefinition,
    WorkflowNodeDefinition,
)
from full_view_agent.domain.models import (
    AuthContext,
    PopulationMetricRow,
    PopulationMetricTable,
    TableDataResult,
    ToolResult,
    WorkflowRef,
)
from full_view_agent.infrastructure.capability_repository import (
    InMemoryCapabilityRepository,
)
from full_view_agent.infrastructure.event_broker import InMemoryEventBroker
from full_view_agent.infrastructure.governance_adapter import InMemoryGovernanceAdapter
from full_view_agent.infrastructure.langgraph_orchestrator import LangGraphOrchestrator
from full_view_agent.infrastructure.memory_store import InMemoryAgentStore

from .test_policy import population_auth_context
from .test_session_run_service import run_request


def _linear_workflow() -> RuntimeWorkflowGraphSnapshot:
    return RuntimeWorkflowGraphSnapshot(
        workflow_id="workflow.housing-summary",
        version="1.0.0",
        start_node_id="start",
        end_node_ids=("end",),
        nodes=(
            RuntimeWorkflowNode(node_id="start", node_type="start"),
            RuntimeWorkflowNode(
                node_id="query",
                node_type="tool",
                tool_ref=("governance.query_housing_metrics", "1.0.0"),
                config_json=json.dumps(
                    {
                        "arguments": {
                            "query": {
                                "metrics": ["dwelling_count"],
                                "scope": {"area_code": "330106"},
                                "group_by": ["street"],
                            }
                        }
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            ),
            RuntimeWorkflowNode(
                node_id="summary",
                node_type="summary",
                config_json='{"text":"出租房汇总工作流已完成。"}',
            ),
            RuntimeWorkflowNode(node_id="end", node_type="end"),
        ),
        edges=(
            RuntimeWorkflowEdge(source_node_id="start", target_node_id="query"),
            RuntimeWorkflowEdge(source_node_id="query", target_node_id="summary"),
            RuntimeWorkflowEdge(source_node_id="summary", target_node_id="end"),
        ),
        timeout_seconds=120,
    )


def _advanced_workflow() -> RuntimeWorkflowGraphSnapshot:
    def tool_node(node_id: str, area_code: str) -> RuntimeWorkflowNode:
        return RuntimeWorkflowNode(
            node_id=node_id,
            node_type="tool",
            tool_ref=("governance.query_population_metrics", "1.0.0"),
            config_json=json.dumps(
                {
                    "arguments": {
                        "query": {
                            "metrics": ["person_count"],
                            "scope": {"area_code": area_code},
                            "filters": (
                                [
                                    {
                                        "field": "person_category",
                                        "operator": "eq",
                                        "value": "solitary_elderly",
                                    }
                                ]
                                if node_id == "branch-a"
                                else []
                            ),
                            "group_by": ["street"],
                        }
                    }
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
        )

    return RuntimeWorkflowGraphSnapshot(
        workflow_id="workflow.advanced-population",
        version="2.0.0",
        start_node_id="start",
        end_node_ids=("end",),
        nodes=(
            RuntimeWorkflowNode(node_id="start", node_type="start"),
            tool_node("gate-query", "330106"),
            RuntimeWorkflowNode(
                node_id="condition",
                node_type="condition",
                condition_expression='result.gate-query.status == "success"',
            ),
            tool_node("branch-a", "330106"),
            RuntimeWorkflowNode(
                node_id="branch-b",
                node_type="summary",
                config_json='{"text":"condition false"}',
            ),
            RuntimeWorkflowNode(
                node_id="join", node_type="join", config_json='{"mode":"selected"}'
            ),
            RuntimeWorkflowNode(
                node_id="summary",
                node_type="summary",
                config_json='{"text":"advanced workflow completed"}',
            ),
            RuntimeWorkflowNode(node_id="end", node_type="end"),
        ),
        edges=(
            RuntimeWorkflowEdge(source_node_id="start", target_node_id="gate-query"),
            RuntimeWorkflowEdge(
                source_node_id="gate-query", target_node_id="condition"
            ),
            RuntimeWorkflowEdge(
                source_node_id="condition", target_node_id="branch-a", condition="true"
            ),
            RuntimeWorkflowEdge(
                source_node_id="condition", target_node_id="branch-b", condition="false"
            ),
            RuntimeWorkflowEdge(source_node_id="branch-a", target_node_id="join"),
            RuntimeWorkflowEdge(source_node_id="branch-b", target_node_id="join"),
            RuntimeWorkflowEdge(source_node_id="join", target_node_id="summary"),
            RuntimeWorkflowEdge(source_node_id="summary", target_node_id="end"),
        ),
        timeout_seconds=120,
    )


def _tool_result(
    *,
    call_id: str,
    area_code: str,
    status: Literal["success", "partial", "denied", "failed"] = "success",
) -> ToolResult:
    return ToolResult(
        tool_call_id=call_id,
        tool_id="governance.query_population_metrics",
        tool_version="1.0.0",
        status=status,
        summary=status,
        data_result=(
            TableDataResult(
                result_id=f"res-{call_id}",
                data_schema_ref="schema://data/population-metric-table/1.0.0",
                result_fingerprint=f"sha256:{call_id}",
                data=PopulationMetricTable(
                    rows=[
                        PopulationMetricRow(
                            area_code=area_code,
                            area_name=area_code,
                            person_count=1,
                        )
                    ]
                ),
                row_count=1,
            )
            if status in {"success", "partial"}
            else None
        ),
    )


def _advanced_workflow_capability() -> WorkflowCapability:
    runtime = _advanced_workflow()
    return WorkflowCapability(
        capability_id=runtime.workflow_id,
        name="Advanced population workflow",
        domain="governance",
        owner="test",
        version=runtime.version,
        status="published",
        guidance="Test workflow guidance",
        nodes=[
            WorkflowNodeDefinition(
                node_id=node.node_id,
                node_type=node.node_type,
                tool_capability_id=node.tool_ref[0] if node.tool_ref else None,
                tool_version=node.tool_ref[1] if node.tool_ref else None,
                condition_expression=node.condition_expression,
                config=json.loads(node.config_json),
            )
            for node in runtime.nodes
        ],
        edges=[
            WorkflowEdgeDefinition(
                source_node_id=edge.source_node_id,
                target_node_id=edge.target_node_id,
                condition=edge.condition,
            )
            for edge in runtime.edges
        ],
        timeout_seconds=runtime.timeout_seconds,
    )


@pytest.mark.asyncio
async def test_linear_workflow_planner_executes_configured_tools_then_reference_only() -> None:
    registry = RuntimeWorkflowRegistry((_linear_workflow(),))
    planner = registry.create_planner(
        WorkflowRef(
            workflow_id="workflow.housing-summary",
            workflow_version="1.0.0",
        )
    )

    first = await planner.decide(HarnessState())
    assert first == ToolAction(
        tool_id="governance.query_housing_metrics",
        arguments={
            "query": {
                "metrics": ["dwelling_count"],
                "scope": {"area_code": "330106"},
                "group_by": ["street"],
            }
        },
    )

    finish = await planner.decide(
        HarnessState(
            tool_results=(
                ToolResult(
                    tool_call_id="tcl-1",
                    tool_id="governance.query_housing_metrics",
                    tool_version="1.0.0",
                    status="success",
                    summary="查询完成",
                    data_result=TableDataResult(
                        result_id="res-workflow",
                        data_schema_ref="schema://data/population-metric-table/1.0.0",
                        result_fingerprint="sha256:workflow",
                        data=PopulationMetricTable(
                            rows=[
                                PopulationMetricRow(
                                    area_code="330106",
                                    area_name="西湖区",
                                    person_count=1,
                                )
                            ]
                        ),
                        row_count=1,
                    ),
                ),
            )
        )
    )
    assert isinstance(finish, FinishAction)
    assert finish.structured_finish is not None
    assert finish.structured_finish.kind == "reference_only"


def test_runtime_workflow_registry_pins_snapshot() -> None:
    source = RuntimeWorkflowRegistry((_linear_workflow(),))
    pinned = source.snapshot()
    source.replace(())

    assert pinned.get(
        WorkflowRef(
            workflow_id="workflow.housing-summary",
            workflow_version="1.0.0",
        )
    ).workflow_id == "workflow.housing-summary"


@pytest.mark.asyncio
async def test_workflow_version_is_rebuilt_exactly_after_runtime_restart() -> None:
    repository = InMemoryCapabilityRepository()
    capability = _advanced_workflow_capability()
    await repository.save_workflow(capability)
    store = InMemoryRunCapabilitySnapshotStore()
    service_a = RunCapabilitySnapshotService(
        repository=repository,
        store=store,
        runtime_workflow_registry=RuntimeWorkflowRegistry((_advanced_workflow(),)),
    )

    first = await service_a.create_snapshot_for_run(
        "run-workflow-restart",
        ToolRegistry.default(),
    )
    assert first.runtime_workflow_registry.get(
        WorkflowRef(
            workflow_id=capability.capability_id,
            workflow_version="2.0.0",
        )
    ).version == "2.0.0"

    service_b = RunCapabilitySnapshotService(
        repository=repository,
        store=store,
        runtime_workflow_registry=RuntimeWorkflowRegistry(),
    )
    rebuilt = await service_b.create_snapshot_for_run(
        "run-workflow-restart",
        ToolRegistry.default(),
    )

    assert rebuilt.created_at == first.created_at
    assert rebuilt.runtime_workflow_registry.get(
        WorkflowRef(
            workflow_id=capability.capability_id,
            workflow_version="2.0.0",
        )
    ).version == "2.0.0"


def test_runtime_workflow_rejects_branching_graph_for_linear_mvp() -> None:
    workflow = _linear_workflow().model_copy(
        update={
            "edges": (
                *_linear_workflow().edges,
                RuntimeWorkflowEdge(
                    source_node_id="start", target_node_id="summary"
                ),
            )
        }
    )
    registry = RuntimeWorkflowRegistry((workflow,))

    with pytest.raises(WorkflowNotAvailable, match="linear"):
        registry.create_planner(
            WorkflowRef(
                workflow_id=workflow.workflow_id,
                workflow_version=workflow.version,
            )
        )


@pytest.mark.asyncio
async def test_advanced_workflow_evaluates_condition_and_merges_selected_branch() -> None:
    planner = RuntimeWorkflowRegistry((_advanced_workflow(),)).create_planner(
        WorkflowRef(
            workflow_id="workflow.advanced-population",
            workflow_version="2.0.0",
        ),
        tool_registry=ToolRegistry.default(),
    )

    gate = await planner.decide(HarnessState())
    assert gate == ToolAction(
        tool_id="governance.query_population_metrics",
        arguments={
            "query": {
                "metrics": ["person_count"],
                "scope": {"area_code": "330106"},
                "filters": [],
                "group_by": ["street"],
            }
        },
    )

    selected_branch = await planner.decide(
        HarnessState(tool_results=(_tool_result(call_id="gate", area_code="330106"),))
    )
    assert isinstance(selected_branch, ToolAction)
    selected_query = selected_branch.arguments["query"]
    assert isinstance(selected_query, dict)
    assert selected_query["filters"] == [
        {
            "field": "person_category",
            "operator": "eq",
            "value": "solitary_elderly",
        }
    ]

    finish = await planner.decide(
        HarnessState(
            tool_results=(
                _tool_result(call_id="gate", area_code="330106"),
                _tool_result(call_id="branch-a", area_code="330106"),
            )
        )
    )
    assert isinstance(finish, FinishAction)
    assert finish.summary == "advanced workflow completed"


@pytest.mark.asyncio
async def test_advanced_workflow_false_branch_skips_unselected_tool_and_merges() -> None:
    planner = RuntimeWorkflowRegistry((_advanced_workflow(),)).create_planner(
        WorkflowRef(
            workflow_id="workflow.advanced-population",
            workflow_version="2.0.0",
        ),
        tool_registry=ToolRegistry.default(),
    )

    finish = await planner.decide(
        HarnessState(
            tool_results=(
                _tool_result(
                    call_id="gate-partial",
                    area_code="330106",
                    status="partial",
                ),
            )
        )
    )

    assert isinstance(finish, FinishAction)
    assert finish.summary == "advanced workflow completed"
    assert len(planner._tools) == 2  # type: ignore[attr-defined]


@pytest.mark.parametrize("terminal_status", ["denied", "failed"])
def test_advanced_workflow_condition_dsl_excludes_terminal_statuses(
    terminal_status: str,
) -> None:
    workflow = _advanced_workflow()
    nodes = tuple(
        node.model_copy(
            update={
                "condition_expression": (
                    f'result.gate-query.status == "{terminal_status}"'
                )
            }
        )
        if node.node_id == "condition"
        else node
        for node in workflow.nodes
    )

    with pytest.raises(WorkflowNotAvailable, match="condition expression"):
        RuntimeWorkflowRegistry((workflow.model_copy(update={"nodes": nodes}),)).create_planner(
            WorkflowRef(
                workflow_id=workflow.workflow_id,
                workflow_version=workflow.version,
            ),
            tool_registry=ToolRegistry.default(),
        )


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda workflow: workflow.model_copy(
                update={
                    "edges": (
                        *workflow.edges,
                        workflow.edges[0],
                    )
                }
            ),
            "duplicate edge",
        ),
        (
            lambda workflow: workflow.model_copy(
                update={
                    "edges": tuple(
                        edge
                        for edge in workflow.edges
                        if not (
                            edge.source_node_id == "condition"
                            and edge.target_node_id == "branch-b"
                        )
                    )
                }
            ),
            "unreachable",
        ),
        (
            lambda workflow: workflow.model_copy(
                update={
                    "edges": (
                        *workflow.edges,
                        RuntimeWorkflowEdge(
                            source_node_id="summary", target_node_id="condition"
                        ),
                    )
                }
            ),
            "cycle",
        ),
        (
            lambda workflow: workflow.model_copy(
                update={
                    "nodes": tuple(
                        node.model_copy(
                            update={"condition_expression": "__import__('os').system('x')"}
                        )
                        if node.node_id == "condition"
                        else node
                        for node in workflow.nodes
                    )
                }
            ),
            "condition expression",
        ),
        (
            lambda workflow: workflow.model_copy(
                update={
                    "edges": tuple(
                        edge.model_copy(update={"target_node_id": "summary"})
                        if edge.source_node_id == "branch-a"
                        else edge
                        for edge in workflow.edges
                    )
                }
            ),
            "join",
        ),
    ],
)
def test_advanced_workflow_rejects_unsafe_graphs(mutate, message: str) -> None:
    workflow = mutate(_advanced_workflow())

    with pytest.raises(WorkflowNotAvailable, match=message):
        RuntimeWorkflowRegistry((workflow,)).create_planner(
            WorkflowRef(
                workflow_id=workflow.workflow_id,
                workflow_version=workflow.version,
            ),
            tool_registry=ToolRegistry.default(),
        )


def test_linear_workflow_planner_rejects_unconfigured_tool_arguments() -> None:
    workflow = _linear_workflow()
    nodes = tuple(
        node.model_copy(update={"config_json": "{}"})
        if node.node_id == "query"
        else node
        for node in workflow.nodes
    )

    with pytest.raises(WorkflowNotAvailable, match="arguments"):
        LinearWorkflowPlanner(workflow.model_copy(update={"nodes": nodes}))


def test_workflow_skill_node_executes_only_a_tool_allowed_by_pinned_skill() -> None:
    skill = RuntimeSkillContract(
        skill_id="skill.population-analysis",
        version="1.0.0",
        guidance="Use the population aggregate Tool.",
        allowed_tool_ids=("governance.query_population_metrics",),
    )
    workflow = RuntimeWorkflowGraphSnapshot(
        workflow_id="workflow.population-by-skill",
        version="1.0.0",
        start_node_id="start",
        end_node_ids=("end",),
        nodes=(
            RuntimeWorkflowNode(node_id="start", node_type="start"),
            RuntimeWorkflowNode(
                node_id="skill",
                node_type="skill",
                skill_ref=(skill.skill_id, skill.version),
                config_json=json.dumps(
                    {
                        "tool_id": "governance.query_population_metrics",
                        "arguments": {
                            "query": {
                                "metrics": ["person_count"],
                                "scope": {"area_code": "330100"},
                                "group_by": ["district"],
                            }
                        },
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            ),
            RuntimeWorkflowNode(node_id="end", node_type="end"),
        ),
        edges=(
            RuntimeWorkflowEdge(source_node_id="start", target_node_id="skill"),
            RuntimeWorkflowEdge(source_node_id="skill", target_node_id="end"),
        ),
        timeout_seconds=120,
    )

    planner = RuntimeWorkflowRegistry((workflow,)).create_planner(
        WorkflowRef(
            workflow_id=workflow.workflow_id,
            workflow_version=workflow.version,
        ),
        tool_registry=ToolRegistry.default(),
        skill_registry=RuntimeSkillRegistry((skill,)),
    )

    assert planner._tools == (  # type: ignore[attr-defined]
        ToolAction(
            tool_id="governance.query_population_metrics",
            arguments={
                "query": {
                    "metrics": ["person_count"],
                    "scope": {"area_code": "330100"},
                    "group_by": ["district"],
                }
            },
        ),
    )


def test_workflow_fails_closed_when_skill_invokes_tool_outside_pinned_allow_list() -> None:
    skill = RuntimeSkillContract(
        skill_id="skill.population-analysis",
        version="1.0.0",
        guidance="Use only population aggregates.",
        allowed_tool_ids=("governance.query_population_metrics",),
    )
    workflow = RuntimeWorkflowGraphSnapshot(
        workflow_id="workflow.invalid-skill-permission",
        version="1.0.0",
        start_node_id="start",
        end_node_ids=("end",),
        nodes=(
            RuntimeWorkflowNode(node_id="start", node_type="start"),
            RuntimeWorkflowNode(
                node_id="skill",
                node_type="skill",
                skill_ref=(skill.skill_id, skill.version),
                config_json=json.dumps(
                    {
                        "tool_id": "governance.query_housing_metrics",
                        "arguments": {"query": {"scope": {"area_code": "330106"}}},
                    }
                ),
            ),
            RuntimeWorkflowNode(node_id="end", node_type="end"),
        ),
        edges=(
            RuntimeWorkflowEdge(source_node_id="start", target_node_id="skill"),
            RuntimeWorkflowEdge(source_node_id="skill", target_node_id="end"),
        ),
        timeout_seconds=120,
    )

    with pytest.raises(WorkflowNotAvailable, match="outside its allow-list"):
        RuntimeWorkflowRegistry((workflow,)).create_planner(
            WorkflowRef(
                workflow_id=workflow.workflow_id,
                workflow_version=workflow.version,
            ),
            tool_registry=ToolRegistry.default(),
            skill_registry=RuntimeSkillRegistry((skill,)),
        )


def test_workflow_fails_closed_when_tool_version_is_not_in_run_registry() -> None:
    workflow = _advanced_workflow()
    nodes = tuple(
        node.model_copy(
            update={
                "tool_ref": (
                    "governance.query_population_metrics",
                    "9.9.9",
                )
            }
        )
        if node.node_id == "branch-a"
        else node
        for node in workflow.nodes
    )

    with pytest.raises(WorkflowNotAvailable, match="version is unavailable"):
        RuntimeWorkflowRegistry((workflow.model_copy(update={"nodes": nodes}),)).create_planner(
            WorkflowRef(
                workflow_id=workflow.workflow_id,
                workflow_version=workflow.version,
            ),
            tool_registry=ToolRegistry.default(),
        )


class _StaticAuth:
    async def get(self, *, user_id: str, run_id: str):
        del user_id, run_id
        return population_auth_context()


def _population_workflow() -> RuntimeWorkflowGraphSnapshot:
    workflow = _linear_workflow()
    nodes = tuple(
        node.model_copy(
            update={
                "tool_ref": ("governance.query_population_metrics", "1.0.0"),
                "config_json": json.dumps(
                    {
                        "arguments": {
                            "query": {
                                "metrics": ["person_count"],
                                "scope": {"area_code": "330106"},
                                "filters": [
                                    {
                                        "field": "person_category",
                                        "operator": "eq",
                                        "value": "solitary_elderly",
                                    }
                                ],
                                "group_by": ["street"],
                            }
                        }
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            }
        )
        if node.node_type == "tool"
        else node
        for node in workflow.nodes
    )
    return workflow.model_copy(
        update={
            "workflow_id": "workflow.population-summary",
            "nodes": nodes,
        }
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("orchestrator_type", ["native", "langgraph"])
async def test_workflow_mode_uses_deterministic_graph_not_agent_planner(
    orchestrator_type: str,
) -> None:
    store = InMemoryAgentStore()
    events = InMemoryEventBroker()
    service = SessionRunService(store)
    registry = ToolRegistry.default()
    capability = CapabilityService(
        registry=registry,
        policy=MinimalPolicyAdapter(),
        adapter=InMemoryGovernanceAdapter(),
    )
    orchestrator_class = (
        LangGraphOrchestrator if orchestrator_type == "langgraph" else NativeOrchestrator
    )
    orchestrator = orchestrator_class(
        service=service,
        store=store,
        events=events,
        auth_context_provider=_StaticAuth(),
        capability=capability,
        registry=registry,
        runtime_workflow_registry=RuntimeWorkflowRegistry((_population_workflow(),)),
    )
    session = await service.create_session(user_id="u", title="workflow")
    request = run_request().model_copy(
        update={
            "mode": "workflow",
            "workflow_ref": WorkflowRef(
                workflow_id="workflow.population-summary",
                workflow_version="1.0.0",
            ),
        }
    )
    run = await service.create_run(
        user_id="u",
        session_id=session.session_id,
        request=request,
    )

    await orchestrator.execute(user_id="u", run_id=run.run_id)

    completed = await store.get_run(user_id="u", run_id=run.run_id)
    assert completed.status == "completed", (
        completed.outcome,
        completed.completion_reason_code,
    )
    assert completed.mode == "workflow"
    assert len(store.results) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("orchestrator_type", ["native", "langgraph"])
async def test_advanced_workflow_runs_condition_join_in_both_orchestrators(
    orchestrator_type: str,
) -> None:
    store = InMemoryAgentStore()
    events = InMemoryEventBroker()
    service = SessionRunService(store)
    registry = ToolRegistry.default()
    capability = CapabilityService(
        registry=registry,
        policy=MinimalPolicyAdapter(),
        adapter=InMemoryGovernanceAdapter(),
    )
    orchestrator_class = (
        LangGraphOrchestrator if orchestrator_type == "langgraph" else NativeOrchestrator
    )
    orchestrator = orchestrator_class(
        service=service,
        store=store,
        events=events,
        auth_context_provider=_StaticAuth(),
        capability=capability,
        registry=registry,
        runtime_workflow_registry=RuntimeWorkflowRegistry((_advanced_workflow(),)),
    )
    session = await service.create_session(user_id="u", title="advanced workflow")
    run = await service.create_run(
        user_id="u",
        session_id=session.session_id,
        request=run_request().model_copy(
            update={
                "mode": "workflow",
                "workflow_ref": WorkflowRef(
                    workflow_id="workflow.advanced-population",
                    workflow_version="2.0.0",
                ),
            }
        ),
    )

    await orchestrator.execute(user_id="u", run_id=run.run_id)

    completed = await store.get_run(user_id="u", run_id=run.run_id)
    assert completed.status == "completed", (
        completed.outcome,
        completed.completion_reason_code,
    )
    assert len(store.results) == 2


class _ReauthenticateOnSecondWorkflowTool:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def execute(
        self,
        *,
        tool_call_id: str,
        tool_id: str,
        raw_arguments: dict[str, object],
        auth_context: AuthContext,
    ) -> ToolResult:
        del raw_arguments, auth_context
        self.calls.append(tool_id)
        if len(self.calls) == 2:
            raise ReauthenticationRequired("credential expired mid-workflow")
        return _tool_result(call_id=tool_call_id, area_code="330106")


@pytest.mark.asyncio
async def test_native_workflow_mid_run_reauthentication_fails_closed() -> None:
    store = InMemoryAgentStore()
    events = InMemoryEventBroker()
    service = SessionRunService(store)
    registry = ToolRegistry.default()
    executor = _ReauthenticateOnSecondWorkflowTool()
    orchestrator = NativeOrchestrator(
        service=service,
        store=store,
        events=events,
        auth_context_provider=_StaticAuth(),
        harness=AgentHarness(tool_executor=executor),
        registry=registry,
        runtime_workflow_registry=RuntimeWorkflowRegistry((_advanced_workflow(),)),
    )
    session = await service.create_session(user_id="u", title="reauth workflow")
    run = await service.create_run(
        user_id="u",
        session_id=session.session_id,
        request=run_request().model_copy(
            update={
                "mode": "workflow",
                "workflow_ref": WorkflowRef(
                    workflow_id="workflow.advanced-population",
                    workflow_version="2.0.0",
                ),
            }
        ),
    )

    await orchestrator.execute(user_id="u", run_id=run.run_id)

    failed = await store.get_run(user_id="u", run_id=run.run_id)
    assert failed.status == "failed"
    assert failed.completion_reason_code == "native_workflow_resume_unsupported"
    assert len(executor.calls) == 2


class _PinnedWorkflowSnapshotService:
    def __init__(self, registry: ToolRegistry) -> None:
        self._registry = registry

    async def create_snapshot_for_run(self, run_id, base_registry, app_id=None):
        del base_registry, app_id
        return RunCapabilitySnapshot(
            run_id=run_id,
            created_at=datetime.now(UTC),
            tool_registry=self._registry,
            runtime_workflow_registry=RuntimeWorkflowRegistry(
                (_population_workflow(),)
            ),
        )

    async def remove_snapshot(self, run_id):
        del run_id


@pytest.mark.asyncio
async def test_workflow_run_uses_definition_pinned_before_live_disable() -> None:
    store = InMemoryAgentStore()
    events = InMemoryEventBroker()
    service = SessionRunService(store)
    registry = ToolRegistry.default()
    capability = CapabilityService(
        registry=registry,
        policy=MinimalPolicyAdapter(),
        adapter=InMemoryGovernanceAdapter(),
    )
    orchestrator = NativeOrchestrator(
        service=service,
        store=store,
        events=events,
        auth_context_provider=_StaticAuth(),
        capability=capability,
        registry=registry,
        snapshot_service=_PinnedWorkflowSnapshotService(registry),  # type: ignore[arg-type]
        runtime_workflow_registry=RuntimeWorkflowRegistry(),
    )
    session = await service.create_session(user_id="u", title="pinned workflow")
    run = await service.create_run(
        user_id="u",
        session_id=session.session_id,
        request=run_request().model_copy(
            update={
                "mode": "workflow",
                "workflow_ref": WorkflowRef(
                    workflow_id="workflow.population-summary",
                    workflow_version="1.0.0",
                ),
            }
        ),
    )

    await orchestrator.execute(user_id="u", run_id=run.run_id)

    completed = await store.get_run(user_id="u", run_id=run.run_id)
    assert completed.status == "completed"
