"""P1-3 纵向切片：mode=analysis 结果按客户端能力发出前端命令的契约。

覆盖：
- 支持/不支持命令能力时 AnalysisPlan 执行的命令产生差异；
- 非区域分组结果（租赁类型/三层办结率）不得发 choropleth；
- 命令绑定 run.origin_client_instance_id，即使能力声明中的实例 id 漂移；
- Result/Evidence 原子持久化先于事件与命令发布；
- 终态重放与恢复不重复发布、不篡改已发布命令。
"""

import pytest

from full_view_agent.application.analysis_graph_execution_service import (
    AnalysisGraphExecutionService,
)
from full_view_agent.application.analysis_observation_validator import (
    AgentStoreAnalysisObservationValidator,
)
from full_view_agent.application.errors import RunStateConflict
from full_view_agent.application.session_run_service import SessionRunService
from full_view_agent.application.tool_observation_service import ToolObservationService
from full_view_agent.application.tool_registry import ToolRegistry
from full_view_agent.domain.analysis_plan import PlanBudget
from full_view_agent.domain.models import (
    AgentMessage,
    AgentRun,
    AgentSession,
    ClientCapabilities,
    TableDataResult,
    TextContent,
)
from full_view_agent.infrastructure.analysis_run_binding_store import (
    InMemoryAnalysisRunBindingStore,
)
from full_view_agent.infrastructure.analysis_step_ledger_store import (
    InMemoryAnalysisStepLedgerStore,
)
from full_view_agent.infrastructure.event_broker import InMemoryEventBroker
from full_view_agent.infrastructure.langgraph_analysis_orchestrator import (
    LangGraphAnalysisOrchestrator,
)
from full_view_agent.infrastructure.memory_store import InMemoryAgentStore

from .test_analysis_executor import _executor, _full_auth_context, _overview_plan

ORIGIN_CLIENT = "cli-analysis-origin"
AREA_CODE = "330106"


class _OrderedStore(InMemoryAgentStore):
    """记录原子写入次序，证明持久化先于事件发布。"""

    def __init__(self, order: list[str]) -> None:
        super().__init__()
        self._order = order

    async def save_tool_observation(self, **kwargs):
        self._order.append("save_tool_observation")
        return await super().save_tool_observation(**kwargs)


class _OrderedEvents(InMemoryEventBroker):
    def __init__(self, order: list[str]) -> None:
        super().__init__()
        self._order = order

    async def publish(self, **kwargs):
        self._order.append(f"event:{kwargs['event_type']}")
        return await super().publish(**kwargs)


def _capabilities(
    supported_commands: tuple[str, ...],
    schema_versions: tuple[str, ...] = ("1.1",),
    *,
    client_instance_id: str = ORIGIN_CLIENT,
) -> ClientCapabilities:
    return ClientCapabilities(
        client_instance_id=client_instance_id,
        frontend_command_schema_versions=list(schema_versions),
        supported_commands=list(supported_commands),
    )


async def _seed_analysis_run(
    store: InMemoryAgentStore,
    *,
    auth,
    capabilities: ClientCapabilities | None,
    origin_client_instance_id: str,
) -> None:
    await store.create_session(
        AgentSession(
            session_id=auth.session_id,
            owner_user_id=auth.principal.user_id,
            title="analysis command contract",
        )
    )
    message = AgentMessage(
        message_id=f"msg-{auth.run_id}",
        session_id=auth.session_id,
        run_id=auth.run_id,
        role="user",
        content=[TextContent(type="text", text="analysis")],
    )
    await store.create_run_if_session_idle(
        user_id=auth.principal.user_id,
        session_id=auth.session_id,
        run=AgentRun(
            run_id=auth.run_id,
            session_id=auth.session_id,
            origin_client_instance_id=origin_client_instance_id,
            client_capabilities=capabilities,
            status="queued",
            mode="analysis",
            input_message_id=message.message_id,
            base_context_version=1,
        ),
        input_message=message,
    )
    await store.start_run(user_id=auth.principal.user_id, run_id=auth.run_id)


async def _runtime(*, capabilities: ClientCapabilities | None):
    """串行预算的 production 组合：步骤逐个执行，发布次序可确定性断言。"""
    _legacy_executor, port, catalog = _executor()
    auth = _full_auth_context()
    plan = _overview_plan(
        catalog,
        constraints=PlanBudget(max_parallel=1, max_tool_calls=6, total_timeout_ms=60_000),
    )
    order: list[str] = []
    store = _OrderedStore(order)
    events = _OrderedEvents(order)
    await _seed_analysis_run(
        store, auth=auth, capabilities=capabilities, origin_client_instance_id=ORIGIN_CLIENT
    )
    port.result_store = store
    port.plan_repository.plans[
        (
            auth.principal.tenant_id,
            auth.principal.user_id,
            auth.run_id,
            plan.plan_id,
        )
    ] = plan
    bindings = InMemoryAnalysisRunBindingStore()
    ledger = InMemoryAnalysisStepLedgerStore(
        observation_validator=AgentStoreAnalysisObservationValidator(store)
    )
    observations = ToolObservationService(
        store=store,
        events=events,
        registry=ToolRegistry.default(),
        evidence_source_system="test-source",
    )
    execution = AnalysisGraphExecutionService(
        catalog=catalog,
        planner=port.planner,
        plan_repository=port.plan_repository,
        resolver=port.resolver,
        semantic_executor=port,
        result_store=store,
        observation_service=observations,
        binding_store=bindings,
        step_ledger=ledger,
    )
    graph = LangGraphAnalysisOrchestrator(
        execution=execution,
        lifecycle=SessionRunService(store),
    )
    return graph, execution, store, events, plan, auth, order


def _subject_of_result(result: TableDataResult) -> str:
    row_fields = set(type(result.data.rows[0]).model_fields) if result.data.rows else set()
    if "person_count" in row_fields:
        return "population"
    if "lease_type" in row_fields:
        return "housing"
    if "level" in row_fields:
        return "event"
    raise AssertionError(f"unrecognized analysis result shape: {result.data_schema_ref}")


def _commands_for_result(store: InMemoryAgentStore, result_id: str) -> list:
    return sorted(
        (
            command
            for command in store.frontend_commands.values()
            if command.payload.result_id == result_id
        ),
        key=lambda command: command.type,
    )


def _graph_kwargs(plan, auth) -> dict:
    return {
        "user_id": auth.principal.user_id,
        "session_id": auth.session_id,
        "analysis_run_id": auth.run_id,
        "plan_id": plan.plan_id,
        "request_id": plan.request_id,
        "auth_context": auth,
    }


@pytest.mark.asyncio
async def test_analysis_commands_follow_capabilities_and_area_grouping() -> None:
    graph, _execution, store, events, plan, auth, order = await _runtime(
        capabilities=_capabilities(("panel.show_table", "map.render_choropleth"))
    )

    outcome = await graph.run(**_graph_kwargs(plan, auth))

    assert outcome.status == "completed"
    table_results = [
        result for result in store.results.values() if isinstance(result, TableDataResult)
    ]
    assert len(table_results) == 3
    commands_by_subject = {
        _subject_of_result(result): _commands_for_result(store, result.result_id)
        for result in table_results
    }
    # 区域分组（直接下级区划）的人口结果：表格面板 + 分级设色地图。
    assert [c.type for c in commands_by_subject["population"]] == [
        "map.render_choropleth",
        "panel.show_table",
    ]
    # 非区域分组（租赁类型汇总 / 三层办结率快照）不得发 choropleth。
    assert [c.type for c in commands_by_subject["housing"]] == ["panel.show_table"]
    assert [c.type for c in commands_by_subject["event"]] == ["panel.show_table"]
    for command in store.frontend_commands.values():
        # 声明 schema 1.1 的客户端正常收到命令，且命令实体固定 schema 1.1。
        assert command.schema_version == "1.1"
        assert command.run_id == auth.run_id
        assert command.target_client_instance_id == ORIGIN_CLIENT
        assert command.preconditions.session_id == auth.session_id
        assert command.preconditions.area_code == AREA_CODE
        assert command.expires_at > command.issued_at
    choropleth = next(
        c for c in store.frontend_commands.values() if c.type == "map.render_choropleth"
    )
    assert choropleth.target == "map_panel"
    assert choropleth.preconditions.required_client_capability == "map.render_choropleth@1.0"
    # 每一步必须先在 Result/Evidence 原子持久化，之后才发布事件与命令。
    groups: list[list[str]] = []
    for entry in order:
        if entry == "save_tool_observation":
            groups.append([entry])
        else:
            assert groups, f"event published before any persistence: {entry}"
            groups[-1].append(entry)
    assert len(groups) == 3
    command_group_sizes = sorted(len(group) - 3 for group in groups)
    assert command_group_sizes == [1, 1, 2]
    for group in groups:
        assert group[0] == "save_tool_observation"
        assert group[1] == "event:result.available"
        assert group[2] == "event:evidence.available"
        assert all(item == "event:frontend.command.requested" for item in group[3:])
    run_events = await events.list_events(run_id=auth.run_id)
    assert sum(event.type == "frontend.command.requested" for event in run_events) == 4


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "capabilities",
    [
        None,
        _capabilities((), ()),
        _capabilities(("panel.show_table", "map.render_choropleth"), ()),
        _capabilities((), ("1.1",)),
        # 反例：仅声明旧版 schema 1.0 的客户端不得收到 schema 1.1 命令，
        # 即使其声明了全部命令能力（命令实体 schema_version 固定为 1.1）。
        _capabilities(("panel.show_table", "map.render_choropleth"), ("1.0",)),
    ],
    ids=[
        "no-capabilities",
        "no-schema-no-commands",
        "commands-without-schema-1.1",
        "schema-without-commands",
        "schema-1.0-only-gets-no-commands",
    ],
)
async def test_analysis_without_declared_capability_persists_without_commands(
    capabilities,
) -> None:
    graph, _execution, store, events, plan, auth, order = await _runtime(
        capabilities=capabilities
    )

    outcome = await graph.run(**_graph_kwargs(plan, auth))

    assert outcome.status == "completed"
    assert outcome.report_result_id is not None
    assert store.frontend_commands == {}
    assert "save_tool_observation" in order
    run_events = await events.list_events(run_id=auth.run_id)
    assert not [e for e in run_events if e.type == "frontend.command.requested"]
    assert sum(event.type == "result.available" for event in run_events) == 3
    assert sum(event.type == "evidence.available" for event in run_events) == 3
    assert len(store.evidence) == 3


@pytest.mark.asyncio
async def test_analysis_replay_and_recovery_do_not_republish_or_mutate_commands() -> None:
    graph, execution, store, events, plan, auth, _order = await _runtime(
        capabilities=_capabilities(("panel.show_table", "map.render_choropleth"))
    )
    kwargs = _graph_kwargs(plan, auth)

    first = await graph.run(**kwargs)
    commands_snapshot = dict(store.frontend_commands)
    events_snapshot = list(await events.list_events(run_id=auth.run_id))
    assert commands_snapshot

    # 终态重放：不产生新的命令或事件，命令内容不被改写。
    replayed = await graph.run(**kwargs)
    assert replayed == first
    assert store.frontend_commands == commands_snapshot
    assert list(await events.list_events(run_id=auth.run_id)) == events_snapshot

    # 恢复场景：已持久化步骤重新执行时直接走台账短路，不重新发布。
    for step in plan.steps:
        recovered = await execution.execute_step(
            analysis_run_id=auth.run_id,
            plan_id=plan.plan_id,
            request_id=plan.request_id,
            step_id=step.step_id,
            auth_context=auth,
        )
        assert recovered.result_id is not None
    assert store.frontend_commands == commands_snapshot
    assert list(await events.list_events(run_id=auth.run_id)) == events_snapshot


@pytest.mark.asyncio
async def test_analysis_commands_bind_origin_client_when_capabilities_diverge() -> None:
    graph, _execution, store, _events, plan, auth, _order = await _runtime(
        capabilities=_capabilities(
            ("panel.show_table", "map.render_choropleth"),
            client_instance_id="cli-stale-capability",
        )
    )

    outcome = await graph.run(**_graph_kwargs(plan, auth))

    assert outcome.status == "completed"
    assert store.frontend_commands, "commands must still be issued for the origin client"
    for command in store.frontend_commands.values():
        assert command.target_client_instance_id == ORIGIN_CLIENT


@pytest.mark.asyncio
async def test_analysis_observation_replay_rejects_forged_command_payload() -> None:
    _graph, execution, store, _events, plan, auth, _order = await _runtime(
        capabilities=_capabilities(("panel.show_table", "map.render_choropleth"))
    )
    await execution.prepare(
        analysis_run_id=auth.run_id,
        plan_id=plan.plan_id,
        request_id=plan.request_id,
        auth_context=auth,
    )
    for step in plan.steps:
        await execution.execute_step(
            analysis_run_id=auth.run_id,
            plan_id=plan.plan_id,
            request_id=plan.request_id,
            step_id=step.step_id,
            auth_context=auth,
        )
    command = next(iter(store.frontend_commands.values()))
    forged = command.model_copy(
        update={
            "preconditions": command.preconditions.model_copy(
                update={"area_code": "999999"}
            )
        }
    )

    with pytest.raises(RunStateConflict):
        await store.save_tool_observation(
            user_id=auth.principal.user_id,
            run_id=auth.run_id,
            result=next(
                result
                for result in store.results.values()
                if isinstance(result, TableDataResult)
                and result.result_id == command.payload.result_id
            ),
            evidence=next(
                evidence
                for evidence in store.evidence.values()
                if evidence.result_id == command.payload.result_id
            ),
            commands=(forged,),
        )
    assert store.frontend_commands[command.command_id] == command
