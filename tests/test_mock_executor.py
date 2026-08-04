import asyncio

import pytest

from full_view_agent.application.errors import (
    ModelProviderTimeout,
    ReauthenticationRequired,
    ResourceNotFound,
)
from full_view_agent.application.harness import (
    AgentHarness,
    FinishAction,
    HarnessLimits,
    ToolAction,
)
from full_view_agent.application.mock_executor import MockRunExecutor, _action_area_codes
from full_view_agent.application.session_run_service import SessionRunService
from full_view_agent.domain.models import ToolResult
from full_view_agent.infrastructure.event_broker import InMemoryEventBroker
from full_view_agent.infrastructure.memory_store import InMemoryAgentStore

from .test_policy import population_auth_context
from .test_session_run_service import run_request


class RecordingCapability:
    def __init__(self) -> None:
        self.tool_ids: list[str] = []
        self.auth_contexts = []

    async def execute(self, **kwargs) -> ToolResult:
        self.tool_ids.append(kwargs["tool_id"])
        self.auth_contexts.append(kwargs["auth_context"])
        return ToolResult.model_validate(
            {
                "tool_call_id": kwargs["tool_call_id"],
                "tool_id": kwargs["tool_id"],
                "tool_version": "1.0.0",
                "status": "success",
                "summary": "来自 Capability Service。",
                "data_result": {
                    "result_id": "res-capability-01",
                    "kind": "table",
                    "data_schema_ref": (
                        "schema://data/population-metric-table/1.0.0"
                    ),
                    "result_fingerprint": "sha256:capability",
                    "data": {
                        "rows": [
                            {
                                "area_code": "330106001",
                                "area_name": "Capability 街道",
                                "person_count": 321,
                            }
                        ]
                    },
                    "row_count": 1,
                },
            }
        )


def test_evidence_area_scope_is_derived_from_executed_tool_arguments() -> None:
    action = ToolAction(
        tool_id="governance.query_population_metrics",
        arguments={"query": {"scope": {"area_code": "330105"}}},
    )

    assert _action_area_codes(action) == ["330105"]


class StaticAuthContextProvider:
    def __init__(self):
        self.auth_context = population_auth_context()

    async def get(self, *, user_id: str, run_id: str):
        return self.auth_context


class SlowCapability:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def execute(self, **kwargs) -> ToolResult:
        self.started.set()
        await self.release.wait()
        return ToolResult.model_validate(
            {
                "tool_call_id": kwargs["tool_call_id"],
                "tool_id": kwargs["tool_id"],
                "tool_version": "1.0.0",
                "status": "success",
                "summary": "延迟结果。",
                "data_result": {
                    "result_id": "res-cancel-race",
                    "kind": "table",
                    "data_schema_ref": (
                        "schema://data/population-metric-table/1.0.0"
                    ),
                    "result_fingerprint": "sha256:cancel-race",
                    "data": {"rows": []},
                    "row_count": 0,
                },
            }
        )


class ReauthenticationCapability:
    async def execute(self, **_kwargs) -> ToolResult:
        raise ReauthenticationRequired("credential must be refreshed")


class FailedCapability:
    async def execute(self, **kwargs) -> ToolResult:
        return ToolResult(
            tool_call_id=kwargs["tool_call_id"],
            tool_id=kwargs["tool_id"],
            tool_version="1.0.0",
            status="failed",
            summary="旧系统请求超时。",
            warnings=["upstream_timeout"],
        )


class UnexpectedFailureCapability:
    async def execute(self, **_kwargs) -> ToolResult:
        raise RuntimeError("unexpected adapter defect")


class DeniedCapability:
    async def execute(self, **kwargs) -> ToolResult:
        return ToolResult(
            tool_call_id=kwargs["tool_call_id"],
            tool_id=kwargs["tool_id"],
            tool_version="1.0.0",
            status="denied",
            summary="当前用户无权使用该能力。",
            warnings=["TOOL_NOT_ENTITLED"],
        )


class ResolveAreaPlanner:
    async def decide(self, state):
        if state.tool_results:
            return FinishAction(summary="区划解析完成", legacy=True)
        return ToolAction(
            tool_id="governance.resolve_area",
            arguments={"query": "西湖区"},
        )


class DirectFinishPlanner:
    async def decide(self, _state):
        return FinishAction(
            summary="我可以查询授权范围内的治理数据。",
            legacy=True,
        )


class TwoToolPlanner:
    async def decide(self, state):
        if len(state.tool_results) == 0:
            return ToolAction(
                tool_id="governance.resolve_area",
                arguments={"query": "西湖区"},
            )
        if len(state.tool_results) == 1:
            return ToolAction(
                tool_id="governance.query_population_metrics",
                arguments={"query": {"scope": {"area_code": "330106"}}},
            )
        return FinishAction(summary="区划与人口查询均已完成", legacy=True)


class RetryFailedToolPlanner:
    async def decide(self, state):
        return ToolAction(
            tool_id="governance.query_population_metrics",
            arguments={
                "query": {
                    "scope": {"area_code": f"330106-{state.model_turns}"}
                }
            },
        )


class MultiResultCapability:
    def __init__(self) -> None:
        self.calls = 0

    async def execute(self, **kwargs) -> ToolResult:
        self.calls += 1
        return ToolResult.model_validate(
            {
                "tool_call_id": kwargs["tool_call_id"],
                "tool_id": kwargs["tool_id"],
                "tool_version": "1.0.0",
                "status": "success",
                "summary": f"第 {self.calls} 步完成",
                "data_result": {
                    "result_id": f"res-multi-{self.calls}",
                    "kind": "table",
                    "data_schema_ref": "schema://data/population-metric-table/1.0.0",
                    "result_fingerprint": f"sha256:multi-{self.calls}",
                    "data": {"rows": []},
                    "row_count": 0,
                },
            }
        )


class FailingModelPlanner:
    async def decide(self, _state):
        raise ModelProviderTimeout("model request timed out")


class RecordingPlannerFactory:
    def __init__(self, planner) -> None:
        self.planner = planner
        self.calls = []

    def create(self, *, user_id, auth_context):
        self.calls.append((user_id, auth_context))
        return self.planner


@pytest.mark.asyncio
async def test_mock_executor_completes_run_and_emits_ordered_events() -> None:
    store = InMemoryAgentStore()
    events = InMemoryEventBroker()
    service = SessionRunService(store)
    executor = MockRunExecutor(
        service=service,
        store=store,
        events=events,
        auth_context_provider=StaticAuthContextProvider(),
    )
    session = await service.create_session(user_id="user-01", title="独居老人分析")
    run = await service.create_run(
        user_id="user-01", session_id=session.session_id, request=run_request()
    )

    await executor.execute(user_id="user-01", run_id=run.run_id)

    completed = await store.get_run(user_id="user-01", run_id=run.run_id)
    published = await events.list_events(run_id=run.run_id)
    assert completed.status == "completed"
    assert completed.outcome == "success"
    assert [event.sequence for event in published] == list(range(1, len(published) + 1))
    assert [event.type for event in published] == [
        "run.started",
        "tool.started",
        "tool.completed",
        "result.available",
        "evidence.available",
        "frontend.command.requested",
        "assistant.message.completed",
        "run.completed",
    ]
    assert published[2].data["tool_result"]["data_result"]["kind"] == "table"
    result_id = published[3].data["result_id"]
    stored_result = await store.get_result(
        user_id="user-01",
        result_id=result_id,
    )
    assert stored_result.kind == "table"
    assert stored_result.data.rows[0].person_count == 128
    command = published[5].data["command"]
    assert command["type"] == "panel.show_table"
    assert command["target_client_instance_id"] == "cli-01"
    assert command["payload"] == {"result_id": result_id}
    with pytest.raises(ResourceNotFound):
        await store.get_result(user_id="other-user", result_id=result_id)


@pytest.mark.asyncio
async def test_mock_executor_requests_a_choropleth_for_supported_population_results() -> None:
    store = InMemoryAgentStore()
    events = InMemoryEventBroker()
    service = SessionRunService(store)
    executor = MockRunExecutor(
        service=service,
        store=store,
        events=events,
        auth_context_provider=StaticAuthContextProvider(),
    )
    session = await service.create_session(user_id="user-map", title="人口地图")
    request = run_request("web-msg-map")
    request.client.supported_commands.append("map.render_choropleth")
    run = await service.create_run(
        user_id="user-map", session_id=session.session_id, request=request
    )

    await executor.execute(user_id="user-map", run_id=run.run_id)

    published = await events.list_events(run_id=run.run_id)
    commands = [
        event.data["command"]
        for event in published
        if event.type == "frontend.command.requested"
    ]
    assert [command["type"] for command in commands] == [
        "panel.show_table",
        "map.render_choropleth",
    ]
    map_command = commands[1]
    assert map_command["target"] == "map_panel"
    assert map_command["preconditions"]["required_client_capability"] == (
        "map.render_choropleth@1.0"
    )
    assert map_command["payload"] == {
        "result_id": commands[0]["payload"]["result_id"],
        "metric_field": "person_count",
        "label_field": "area_name",
        "area_code_field": "area_code",
        "legend_title": "独居老人数量",
        "palette": "sequential_blue_5",
        "fit_bounds": True,
    }


@pytest.mark.asyncio
async def test_mock_executor_applies_accepted_steer_at_safe_checkpoint() -> None:
    store = InMemoryAgentStore()
    events = InMemoryEventBroker()
    service = SessionRunService(store)
    executor = MockRunExecutor(
        service=service,
        store=store,
        events=events,
        auth_context_provider=StaticAuthContextProvider(),
    )
    session = await service.create_session(user_id="user-01", title="独居老人分析")
    run = await service.create_run(
        user_id="user-01", session_id=session.session_id, request=run_request()
    )
    await service.start_run(user_id="user-01", run_id=run.run_id)
    await service.steer_run(
        user_id="user-01",
        run_id=run.run_id,
        client_instance_id="cli-01",
        content="结果出来后再筛选 80 岁以上。",
    )

    await executor.execute(user_id="user-01", run_id=run.run_id)

    published = await events.list_events(run_id=run.run_id)
    assert "steer.applied" in [event.type for event in published]


@pytest.mark.asyncio
async def test_mock_executor_delegates_tool_execution_to_capability_service() -> None:
    store = InMemoryAgentStore()
    events = InMemoryEventBroker()
    service = SessionRunService(store)
    capability = RecordingCapability()
    executor = MockRunExecutor(
        service=service,
        store=store,
        events=events,
        capability=capability,
        auth_context_provider=StaticAuthContextProvider(),
    )
    session = await service.create_session(user_id="user-01", title="能力调用测试")
    run = await service.create_run(
        user_id="user-01",
        session_id=session.session_id,
        request=run_request(),
    )

    await executor.execute(user_id="user-01", run_id=run.run_id)

    published = await events.list_events(run_id=run.run_id)
    tool_result = published[2].data["tool_result"]
    assert capability.tool_ids == ["governance.query_population_metrics"]
    assert capability.auth_contexts == [population_auth_context()]
    assert tool_result["data_result"]["data"]["rows"][0]["person_count"] == 321


@pytest.mark.asyncio
async def test_mock_executor_uses_injected_planner_factory() -> None:
    store = InMemoryAgentStore()
    events = InMemoryEventBroker()
    service = SessionRunService(store)
    capability = RecordingCapability()
    planner_factory = RecordingPlannerFactory(ResolveAreaPlanner())
    executor = MockRunExecutor(
        service=service,
        store=store,
        events=events,
        capability=capability,
        auth_context_provider=StaticAuthContextProvider(),
        planner_factory=planner_factory,
    )
    session = await service.create_session(user_id="user-01", title="动态规划器")
    run = await service.create_run(
        user_id="user-01", session_id=session.session_id, request=run_request()
    )

    await executor.execute(user_id="user-01", run_id=run.run_id)

    assert capability.tool_ids == ["governance.resolve_area"]
    assert planner_factory.calls == [("user-01", population_auth_context())]


@pytest.mark.asyncio
async def test_executor_completes_a_direct_answer_without_a_tool_call() -> None:
    store = InMemoryAgentStore()
    events = InMemoryEventBroker()
    service = SessionRunService(store)
    executor = MockRunExecutor(
        service=service,
        store=store,
        events=events,
        auth_context_provider=StaticAuthContextProvider(),
        planner_factory=RecordingPlannerFactory(DirectFinishPlanner()),
    )
    session = await service.create_session(user_id="user-01", title="能力说明")
    run = await service.create_run(
        user_id="user-01", session_id=session.session_id, request=run_request()
    )

    await executor.execute(user_id="user-01", run_id=run.run_id)

    terminal = await store.get_run(user_id="user-01", run_id=run.run_id)
    messages = await store.list_messages(user_id="user-01", session_id=session.session_id)
    published = await events.list_events(run_id=run.run_id)
    assert terminal.status == "completed"
    assert terminal.outcome == "success"
    assert [message.role for message in messages] == ["user", "assistant"]
    assert messages[-1].content[0].text == "我可以查询授权范围内的治理数据。"
    assert messages[-1].evidence_ids == []
    assert [event.type for event in published] == [
        "run.started",
        "assistant.message.completed",
        "run.completed",
    ]


@pytest.mark.asyncio
async def test_executor_persists_result_and_evidence_for_every_successful_tool() -> None:
    store = InMemoryAgentStore()
    events = InMemoryEventBroker()
    service = SessionRunService(store)
    executor = MockRunExecutor(
        service=service,
        store=store,
        events=events,
        capability=MultiResultCapability(),
        auth_context_provider=StaticAuthContextProvider(),
        planner_factory=RecordingPlannerFactory(TwoToolPlanner()),
    )
    session = await service.create_session(user_id="user-01", title="多步骤查询")
    run = await service.create_run(
        user_id="user-01", session_id=session.session_id, request=run_request()
    )

    await executor.execute(user_id="user-01", run_id=run.run_id)

    messages = await store.list_messages(user_id="user-01", session_id=session.session_id)
    assistant = messages[-1]
    published = await events.list_events(run_id=run.run_id)
    persisted_result_ids = set(store.results)
    assert len(persisted_result_ids) == 2
    assert persisted_result_ids.isdisjoint({"res-multi-1", "res-multi-2"})
    assert len(store.evidence) == 2
    result_events = [event for event in published if event.type == "result.available"]
    assert {event.data["result_id"] for event in result_events} == persisted_result_ids
    assert sum(event.type == "evidence.available" for event in published) == 2
    assert {
        item.result_id for item in assistant.content if item.type == "result_reference"
    } == persisted_result_ids
    assert {evidence.result_id for evidence in store.evidence.values()} == (
        persisted_result_ids
    )
    assert len(assistant.evidence_ids) == 2
    assert set(assistant.evidence_ids) == set(store.evidence)


@pytest.mark.asyncio
async def test_executor_preserves_model_failure_code() -> None:
    store = InMemoryAgentStore()
    events = InMemoryEventBroker()
    service = SessionRunService(store)
    executor = MockRunExecutor(
        service=service,
        store=store,
        events=events,
        auth_context_provider=StaticAuthContextProvider(),
        planner_factory=RecordingPlannerFactory(FailingModelPlanner()),
    )
    session = await service.create_session(user_id="user-01", title="模型超时")
    run = await service.create_run(
        user_id="user-01", session_id=session.session_id, request=run_request()
    )

    await executor.execute(user_id="user-01", run_id=run.run_id)

    failed = await store.get_run(user_id="user-01", run_id=run.run_id)
    published = await events.list_events(run_id=run.run_id)
    assert failed.status == "failed"
    assert failed.completion_reason_code == "model_timeout"
    assert published[-1].data["error_code"] == "model_timeout"


@pytest.mark.asyncio
async def test_executor_drops_tool_result_when_run_is_cancelled_during_call() -> None:
    store = InMemoryAgentStore()
    events = InMemoryEventBroker()
    service = SessionRunService(store)
    capability = SlowCapability()
    executor = MockRunExecutor(
        service=service,
        store=store,
        events=events,
        capability=capability,
        auth_context_provider=StaticAuthContextProvider(),
    )
    session = await service.create_session(user_id="user-01", title="取消竞态")
    run = await service.create_run(
        user_id="user-01",
        session_id=session.session_id,
        request=run_request(),
    )

    task = asyncio.create_task(executor.execute(user_id="user-01", run_id=run.run_id))
    await capability.started.wait()
    await service.cancel_run(user_id="user-01", run_id=run.run_id)
    capability.release.set()
    await task

    cancelled = await store.get_run(user_id="user-01", run_id=run.run_id)
    published = await events.list_events(run_id=run.run_id)
    assert cancelled.status == "cancelled"
    assert "res-cancel-race" not in store.results
    assert [event.type for event in published] == ["run.started", "tool.started"]


@pytest.mark.asyncio
async def test_executor_waits_for_reauthentication_instead_of_failing_run() -> None:
    store = InMemoryAgentStore()
    events = InMemoryEventBroker()
    service = SessionRunService(store)
    executor = MockRunExecutor(
        service=service,
        store=store,
        events=events,
        capability=ReauthenticationCapability(),
        auth_context_provider=StaticAuthContextProvider(),
    )
    session = await service.create_session(user_id="user-01", title="重新认证")
    run = await service.create_run(
        user_id="user-01",
        session_id=session.session_id,
        request=run_request(),
    )

    await executor.execute(user_id="user-01", run_id=run.run_id)

    waiting = await store.get_run(user_id="user-01", run_id=run.run_id)
    published = await events.list_events(run_id=run.run_id)
    assert waiting.status == "waiting_input"
    assert waiting.waiting_for == "reauth"
    assert waiting.outcome is None
    assert [event.type for event in published][-3:] == [
        "run.waiting",
        "input.required",
        "reauth_required",
    ]
    assert published[-2].data["allow_free_text"] is False
    assert published[-1].data["input_request_id"].startswith("inreq_")
    assert published[-1].data["run_state_version"] == waiting.state_version


@pytest.mark.asyncio
async def test_executor_turns_harness_budget_exhaustion_into_failed_terminal_run() -> None:
    store = InMemoryAgentStore()
    events = InMemoryEventBroker()
    service = SessionRunService(store)
    capability = RecordingCapability()
    harness = AgentHarness(
        tool_executor=capability,
        limits=HarnessLimits(max_model_turns=1),
    )
    executor = MockRunExecutor(
        service=service,
        store=store,
        events=events,
        capability=capability,
        auth_context_provider=StaticAuthContextProvider(),
        harness=harness,
    )
    session = await service.create_session(user_id="user-01", title="预算耗尽")
    run = await service.create_run(
        user_id="user-01",
        session_id=session.session_id,
        request=run_request(),
    )

    await executor.execute(user_id="user-01", run_id=run.run_id)

    failed = await store.get_run(user_id="user-01", run_id=run.run_id)
    published = await events.list_events(run_id=run.run_id)
    assert failed.status == "failed"
    assert failed.outcome == "failed"
    assert failed.completion_reason_code == "budget_exceeded"
    assert published[-1].type == "run.failed"
    assert published[-1].data["error_code"] == "budget_exceeded"


@pytest.mark.asyncio
async def test_executor_preserves_failed_tool_reason_in_terminal_run_and_events() -> None:
    store = InMemoryAgentStore()
    events = InMemoryEventBroker()
    service = SessionRunService(store)
    executor = MockRunExecutor(
        service=service,
        store=store,
        events=events,
        capability=FailedCapability(),
        auth_context_provider=StaticAuthContextProvider(),
    )
    session = await service.create_session(user_id="user-01", title="Tool 失败")
    run = await service.create_run(
        user_id="user-01", session_id=session.session_id, request=run_request()
    )

    await executor.execute(user_id="user-01", run_id=run.run_id)

    failed = await store.get_run(user_id="user-01", run_id=run.run_id)
    published = await events.list_events(run_id=run.run_id)
    assert failed.status == "failed"
    assert failed.completion_reason_code == "upstream_timeout"
    assert [event.type for event in published] == [
        "run.started",
        "tool.started",
        "tool.failed",
        "run.failed",
    ]
    assert published[-2].data["tool_result"]["warnings"] == ["upstream_timeout"]


@pytest.mark.asyncio
async def test_executor_preserves_last_tool_reason_after_failure_budget_stops_retries() -> None:
    store = InMemoryAgentStore()
    events = InMemoryEventBroker()
    service = SessionRunService(store)
    executor = MockRunExecutor(
        service=service,
        store=store,
        events=events,
        capability=FailedCapability(),
        auth_context_provider=StaticAuthContextProvider(),
        planner_factory=RecordingPlannerFactory(RetryFailedToolPlanner()),
    )
    session = await service.create_session(user_id="user-01", title="Tool 连续超时")
    run = await service.create_run(
        user_id="user-01", session_id=session.session_id, request=run_request()
    )

    await executor.execute(user_id="user-01", run_id=run.run_id)

    failed = await store.get_run(user_id="user-01", run_id=run.run_id)
    published = await events.list_events(run_id=run.run_id)
    assert failed.status == "failed"
    assert failed.completion_reason_code == "upstream_timeout"
    assert published[-1].type == "run.failed"
    assert published[-1].data["error_code"] == "upstream_timeout"
    assert sum(event.type == "tool.failed" for event in published) == 3


@pytest.mark.asyncio
async def test_executor_terminalizes_run_after_unexpected_background_failure() -> None:
    store = InMemoryAgentStore()
    events = InMemoryEventBroker()
    service = SessionRunService(store)
    executor = MockRunExecutor(
        service=service,
        store=store,
        events=events,
        capability=UnexpectedFailureCapability(),
        auth_context_provider=StaticAuthContextProvider(),
    )
    session = await service.create_session(user_id="user-01", title="意外异常")
    run = await service.create_run(
        user_id="user-01", session_id=session.session_id, request=run_request()
    )

    await executor.execute(user_id="user-01", run_id=run.run_id)

    failed = await store.get_run(user_id="user-01", run_id=run.run_id)
    published = await events.list_events(run_id=run.run_id)
    assert failed.status == "failed"
    assert failed.completion_reason_code == "internal_error"
    assert published[-1].type == "run.failed"
    assert published[-1].data["error_code"] == "internal_error"
    assert store.sessions[session.session_id].active_run_id is None


@pytest.mark.asyncio
async def test_executor_completes_policy_denial_without_requiring_data_result() -> None:
    store = InMemoryAgentStore()
    events = InMemoryEventBroker()
    service = SessionRunService(store)
    executor = MockRunExecutor(
        service=service,
        store=store,
        events=events,
        capability=DeniedCapability(),
        auth_context_provider=StaticAuthContextProvider(),
    )
    session = await service.create_session(user_id="user-01", title="权限拒绝")
    run = await service.create_run(
        user_id="user-01", session_id=session.session_id, request=run_request()
    )

    await executor.execute(user_id="user-01", run_id=run.run_id)

    completed = await store.get_run(user_id="user-01", run_id=run.run_id)
    published = await events.list_events(run_id=run.run_id)
    assert completed.status == "completed"
    assert completed.outcome == "denied"
    assert completed.completion_reason_code == "TOOL_NOT_ENTITLED"
    assert [event.type for event in published] == [
        "run.started",
        "tool.started",
        "tool.completed",
        "run.completed",
    ]
