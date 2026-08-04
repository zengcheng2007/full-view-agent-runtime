"""P2 模型受控研判意图路由契约测试。

契约边界：

- 模型选中 ``agent.request_regional_analysis`` 且该虚拟能力确实被广告时，
  ModelPlanner 用 ``AnalysisIntentV1`` 严格校验并返回独立
  ``AnalysisIntentAction``——绝不返回 ToolAction、绝不进入 CapabilityService；
- 未广告、非法载荷、敏感字段一律 ``ModelContractError``，不降级；
- execution bridge 未配置时，AgentHarness 在任何 Tool 副作用前稳定
  fail closed；Native/LangGraph 共用该门禁，run 以
  ``model_contract_error`` 失败且不触发 adapter；
- 默认 composition（无 presenter 注入）不广告该虚拟能力。
"""

import pytest

from full_view_agent.application.analysis_intent_presenter import (
    AnalysisIntentToolPresenter,
)
from full_view_agent.application.capability_service import CapabilityService, ToolAdapter
from full_view_agent.application.context_builder import AgentContextBuilder
from full_view_agent.application.errors import ModelContractError
from full_view_agent.application.harness import (
    ANALYSIS_INTENT_TOOL_ID,
    AgentHarness,
    AnalysisIntentAction,
    HarnessAction,
    HarnessState,
    ToolAction,
)
from full_view_agent.application.model_planner import ModelPlanner
from full_view_agent.application.model_provider import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ModelToolCall,
    ModelToolDefinition,
    ModelUsage,
)
from full_view_agent.application.native_orchestrator import NativeOrchestrator
from full_view_agent.application.policy import MinimalPolicyAdapter
from full_view_agent.application.session_run_service import SessionRunService
from full_view_agent.application.tool_registry import ToolRegistry
from full_view_agent.domain.analysis_intent import AnalysisIntentV1
from full_view_agent.domain.models import AuthContext, DataResult, ToolResult
from full_view_agent.infrastructure.event_broker import InMemoryEventBroker
from full_view_agent.infrastructure.langgraph_orchestrator import (
    LangGraphOrchestrator,
)
from full_view_agent.infrastructure.memory_store import InMemoryAgentStore
from full_view_agent.semantic.catalog import SemanticCatalog

from .test_policy import population_auth_context
from .test_session_run_service import run_request

VALID_INTENT_ARGUMENTS = {
    "kind": "regional_analysis",
    "goals": ["population"],
    "scope": {"kind": "named_area", "area_query": "西湖区"},
}


def _area_capable_auth() -> AuthContext:
    """正向用例授权：显式持有区划解析前置（governance.area.read +
    administrative_area），否则 presenter fail closed 不呈现虚拟能力。"""
    base = population_auth_context()
    data_scopes = base.data_scopes.model_copy(
        update={"datasets": [*base.data_scopes.datasets, "administrative_area"]}
    )
    return base.model_copy(
        update={
            "entitlements": [*base.entitlements, "governance.area.read"],
            "data_scopes": data_scopes,
        }
    )


class IntentAdContextBuilder:
    """只广告研判虚拟能力（可选携带 server_arguments 的反例注入）。"""

    def __init__(self, *, server_arguments: dict[str, object] | None = None) -> None:
        self.request = ModelRequest(
            messages=(ModelMessage(role="user", content="研判一下西湖区"),),
            tools=(
                ModelToolDefinition(
                    tool_id=ANALYSIS_INTENT_TOOL_ID,
                    description="区域研判请求入口",
                    input_schema={"type": "object"},
                    server_arguments=dict(server_arguments or {}),
                ),
            ),
        )

    async def build(self, **_kwargs) -> ModelRequest:
        return self.request


class PopulationToolContextBuilder(IntentAdContextBuilder):
    """只广告普通业务 Tool：虚拟能力未广告。"""

    def __init__(self) -> None:
        super().__init__()
        self.request = ModelRequest(
            messages=(ModelMessage(role="user", content="查询人口"),),
            tools=(
                ModelToolDefinition(
                    tool_id="governance.query_population_metrics",
                    description="查询人口指标",
                    input_schema={"type": "object"},
                ),
            ),
        )


class QueueModelProvider:
    def __init__(self, *responses: ModelResponse) -> None:
        self.responses = list(responses)
        self.requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        return self.responses.pop(0)


def intent_response(arguments: dict[str, object]) -> ModelResponse:
    return ModelResponse(
        content=None,
        tool_calls=(
            ModelToolCall(tool_id=ANALYSIS_INTENT_TOOL_ID, arguments=arguments),
        ),
        finish_reason="tool_calls",
        usage=ModelUsage(total_tokens=10),
    )


def make_planner(
    provider: QueueModelProvider, context_builder: object
) -> ModelPlanner:
    return ModelPlanner(
        provider=provider,
        context_builder=context_builder,  # type: ignore[arg-type]
        user_id="user-01",
        auth_context=population_auth_context(),
    )


# ---------------------------------------------------------------------------
# ModelPlanner：合法 call → AnalysisIntentAction；其余一律 ModelContractError
# ---------------------------------------------------------------------------


async def test_planner_returns_analysis_intent_action_for_valid_call() -> None:
    provider = QueueModelProvider(intent_response(dict(VALID_INTENT_ARGUMENTS)))
    planner = make_planner(provider, IntentAdContextBuilder())

    action = await planner.decide(HarnessState())

    assert isinstance(action, AnalysisIntentAction)
    assert not isinstance(action, ToolAction)
    assert action.intent == AnalysisIntentV1.model_validate(VALID_INTENT_ARGUMENTS)
    assert action.intent.goals == ("population",)
    assert action.intent.scope.kind == "named_area"
    assert len(provider.requests) == 1


async def test_planner_canonicalizes_intent_goals() -> None:
    provider = QueueModelProvider(
        intent_response(
            {**VALID_INTENT_ARGUMENTS, "goals": ["housing", "population", "housing"]}
        )
    )
    planner = make_planner(provider, IntentAdContextBuilder())

    action = await planner.decide(HarnessState())

    assert isinstance(action, AnalysisIntentAction)
    assert action.intent.goals == ("population", "housing")


async def test_planner_rejects_intent_call_when_not_advertised() -> None:
    provider = QueueModelProvider(intent_response(dict(VALID_INTENT_ARGUMENTS)))
    planner = make_planner(provider, PopulationToolContextBuilder())

    with pytest.raises(ModelContractError):
        await planner.decide(HarnessState())


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("steps", ["query_population_metrics"]),
        ("sql", "SELECT * FROM population"),
        ("url", "http://internal.test/adapter"),
        ("adapter", "adapter://geo-qxst/population-metrics/1.0"),
        ("budget", {"tool_calls": 9}),
        ("area_code", "330106"),
    ],
)
async def test_planner_rejects_sensitive_intent_fields(field: str, value: object) -> None:
    provider = QueueModelProvider(
        intent_response({**VALID_INTENT_ARGUMENTS, field: value})
    )
    planner = make_planner(provider, IntentAdContextBuilder())

    with pytest.raises(ModelContractError):
        await planner.decide(HarnessState())


async def test_planner_rejects_area_code_smuggled_into_scope() -> None:
    provider = QueueModelProvider(
        intent_response(
            {
                **VALID_INTENT_ARGUMENTS,
                "scope": {"kind": "current_area", "area_code": "330106"},
            }
        )
    )
    planner = make_planner(provider, IntentAdContextBuilder())

    with pytest.raises(ModelContractError):
        await planner.decide(HarnessState())


async def test_planner_rejects_current_area_scope_even_if_domain_valid() -> None:
    """不合约 provider 无视呈现 schema、返回 current_area 意图时，
    planner 必须显式 fail closed，不能仅依赖 provider 遵守 JSON schema。

    载荷本身是领域合法的（服务端内部仍支持 current_area），拒绝发生在
    planner 边界——任何 AnalysisIntentAction/ToolAction 都不得返回。
    """
    arguments = {**VALID_INTENT_ARGUMENTS, "scope": {"kind": "current_area"}}
    # 前置确认：领域模型仍支持 current_area，拒绝纯粹来自模型边界策略。
    assert AnalysisIntentV1.model_validate(arguments).scope.kind == "current_area"
    provider = QueueModelProvider(intent_response(arguments))
    planner = make_planner(provider, IntentAdContextBuilder())

    # "current.area" 同时容忍 current_area / current-area 两种措辞。
    with pytest.raises(ModelContractError, match="current.area"):
        await planner.decide(HarnessState())


@pytest.mark.parametrize(
    "arguments",
    [
        {**VALID_INTENT_ARGUMENTS, "goals": []},
        {**VALID_INTENT_ARGUMENTS, "goals": ["not_a_goal"]},
        {**VALID_INTENT_ARGUMENTS, "scope": {"kind": "anywhere"}},
        {"goals": ["population"]},  # missing scope
    ],
)
async def test_planner_rejects_invalid_intent_payload(
    arguments: dict[str, object],
) -> None:
    provider = QueueModelProvider(intent_response(arguments))
    planner = make_planner(provider, IntentAdContextBuilder())

    with pytest.raises(ModelContractError):
        await planner.decide(HarnessState())


async def test_planner_never_injects_server_arguments_into_intent_action() -> None:
    provider = QueueModelProvider(intent_response(dict(VALID_INTENT_ARGUMENTS)))
    builder = IntentAdContextBuilder(
        server_arguments={"area_code": "330106", "budget": {"tool_calls": 9}}
    )
    planner = make_planner(provider, builder)

    action = await planner.decide(HarnessState())

    assert isinstance(action, AnalysisIntentAction)
    serialized = action.intent.model_dump(mode="json")
    assert "area_code" not in str(serialized)
    assert "budget" not in str(serialized)


async def test_planner_calls_model_when_only_intent_tool_is_advertised() -> None:
    provider = QueueModelProvider(intent_response(dict(VALID_INTENT_ARGUMENTS)))
    planner = make_planner(provider, IntentAdContextBuilder())

    await planner.decide(HarnessState())

    # 仅有研判虚拟能力时不得触发 no-tools 快停：模型必须被调用。
    assert len(provider.requests) == 1
    advertised = [tool.tool_id for tool in provider.requests[0].tools]
    assert ANALYSIS_INTENT_TOOL_ID in advertised


# ---------------------------------------------------------------------------
# AgentHarness：execution bridge 未配置 → 任何 Tool 副作用前 fail closed
# ---------------------------------------------------------------------------


class _SpyToolExecutor:
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
        del tool_call_id, raw_arguments, auth_context
        self.calls.append(tool_id)
        raise RuntimeError("tool executor must never run for an intent action")


class _IntentPlanner:
    def __init__(self, intent: AnalysisIntentV1) -> None:
        self._intent = intent

    async def decide(self, state: HarnessState) -> HarnessAction:
        del state
        return AnalysisIntentAction(intent=self._intent)


def _validated_intent() -> AnalysisIntentV1:
    return AnalysisIntentV1.model_validate(VALID_INTENT_ARGUMENTS)


async def test_harness_run_fails_closed_before_any_tool_side_effect() -> None:
    executor = _SpyToolExecutor()
    before_calls: list[str] = []
    after_calls: list[str] = []

    async def before_tool_call(action: ToolAction, tool_call_id: str) -> None:
        before_calls.append(f"{action.tool_id}:{tool_call_id}")

    async def after_tool_call(result: ToolResult) -> None:
        after_calls.append(result.tool_call_id)

    harness = AgentHarness(tool_executor=executor)

    with pytest.raises(ModelContractError):
        await harness.run(
            planner=_IntentPlanner(_validated_intent()),
            auth_context=population_auth_context(),
            before_tool_call=before_tool_call,
            after_tool_call=after_tool_call,
        )

    assert executor.calls == []
    assert before_calls == []
    assert after_calls == []


async def test_harness_plan_action_once_rejects_intent_action() -> None:
    harness = AgentHarness(tool_executor=_SpyToolExecutor())
    control = harness.begin()

    with pytest.raises(ModelContractError):
        await harness.plan_action_once(
            planner=_IntentPlanner(_validated_intent()),
            control=control,
        )


async def test_harness_plan_once_rejects_intent_action() -> None:
    harness = AgentHarness(tool_executor=_SpyToolExecutor())
    control = harness.begin()

    with pytest.raises(ModelContractError):
        await harness.plan_once(
            planner=_IntentPlanner(_validated_intent()),
            control=control,
        )


# ---------------------------------------------------------------------------
# Native / LangGraph：专用 action 不触达 Tool 执行链，run 稳定 fail closed
# ---------------------------------------------------------------------------


class _SpyAdapter(ToolAdapter):
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def execute(self, **kwargs: object) -> DataResult:
        self.calls.append(kwargs)
        raise RuntimeError("adapter must never run for an intent action")


class _StaticAuth:
    def __init__(self, ctx: AuthContext) -> None:
        self._ctx = ctx

    async def get(self, *, user_id: str, run_id: str) -> AuthContext:
        del user_id, run_id
        return self._ctx


async def _make_run(store: InMemoryAgentStore) -> str:
    service = SessionRunService(store)
    session = await service.create_session(user_id="user-intent", title="研判")
    run = await service.create_run(
        user_id="user-intent",
        session_id=session.session_id,
        request=run_request(),
    )
    return run.run_id


@pytest.fixture(
    params=[NativeOrchestrator, LangGraphOrchestrator],
    ids=["native", "langgraph"],
)
def orchestrator(
    request: pytest.FixtureRequest,
) -> tuple[object, InMemoryAgentStore, InMemoryEventBroker, _SpyAdapter]:
    orchestrator_type = request.param
    store = InMemoryAgentStore()
    events = InMemoryEventBroker()
    adapter = _SpyAdapter()
    registry = ToolRegistry.default()
    capability = CapabilityService(
        registry=registry,
        policy=MinimalPolicyAdapter(),
        adapter=adapter,
    )

    class _IntentPlannerFactory:
        def create(self, *, user_id: str, auth_context: AuthContext) -> object:
            del user_id, auth_context
            return _IntentPlanner(_validated_intent())

    orch = orchestrator_type(
        service=SessionRunService(store),
        store=store,
        events=events,
        auth_context_provider=_StaticAuth(population_auth_context()),
        capability=capability,
        registry=registry,
        planner_factory=_IntentPlannerFactory(),
    )
    return orch, store, events, adapter


async def test_orchestrators_fail_closed_on_intent_action(
    orchestrator: tuple[object, InMemoryAgentStore, InMemoryEventBroker, _SpyAdapter],
) -> None:
    orch, store, events, adapter = orchestrator
    run_id = await _make_run(store)

    await orch.execute(user_id="user-intent", run_id=run_id)  # type: ignore[attr-defined]

    run = await store.get_run(user_id="user-intent", run_id=run_id)
    assert run.status == "failed"
    assert run.completion_reason_code == "model_contract_error"
    assert adapter.calls == []
    event_types = [
        event.type for event in await events.list_events(run_id=run_id)
    ]
    assert "tool.started" not in event_types
    assert "tool.completed" not in event_types
    assert "run.failed" in event_types


# ---------------------------------------------------------------------------
# ContextBuilder：默认不广告；显式注入且 present 非 None 才追加虚拟 Tool
# ---------------------------------------------------------------------------


async def test_context_builder_default_does_not_advertise_intent_tool() -> None:
    store = InMemoryAgentStore()
    service = SessionRunService(store)
    session = await service.create_session(user_id="user-01", title="默认")
    run = await service.create_run(
        user_id="user-01",
        session_id=session.session_id,
        request=run_request(),
    )
    auth_context = population_auth_context().model_copy(
        update={"session_id": session.session_id, "run_id": run.run_id}
    )
    builder = AgentContextBuilder(store=store, registry=ToolRegistry.default())

    request = await builder.build(
        user_id="user-01",
        auth_context=auth_context,
        state=HarnessState(),
    )

    assert ANALYSIS_INTENT_TOOL_ID not in [tool.tool_id for tool in request.tools]


async def test_context_builder_advertises_intent_tool_when_injected() -> None:
    store = InMemoryAgentStore()
    service = SessionRunService(store)
    session = await service.create_session(user_id="user-01", title="注入")
    run = await service.create_run(
        user_id="user-01",
        session_id=session.session_id,
        request=run_request(),
    )
    auth_context = _area_capable_auth().model_copy(
        update={"session_id": session.session_id, "run_id": run.run_id}
    )
    builder = AgentContextBuilder(
        store=store,
        registry=ToolRegistry.default(),
        analysis_intent_presenter=AnalysisIntentToolPresenter(
            catalog=SemanticCatalog.default()
        ),
    )

    request = await builder.build(
        user_id="user-01",
        auth_context=auth_context,
        state=HarnessState(),
    )

    tool_ids = [tool.tool_id for tool in request.tools]
    assert ANALYSIS_INTENT_TOOL_ID in tool_ids
    intent_tool = next(
        tool for tool in request.tools if tool.tool_id == ANALYSIS_INTENT_TOOL_ID
    )
    # 虚拟能力不是业务 Tool：不携带任何 server_arguments。
    assert intent_tool.server_arguments == {}
    assert intent_tool.input_schema["additionalProperties"] is False


async def test_context_builder_skips_intent_tool_when_present_returns_none() -> None:
    store = InMemoryAgentStore()
    service = SessionRunService(store)
    session = await service.create_session(user_id="user-01", title="无权")
    run = await service.create_run(
        user_id="user-01",
        session_id=session.session_id,
        request=run_request(),
    )
    # 无研判授权：presenter 返回 None，虚拟能力不可见。
    auth_context = population_auth_context().model_copy(
        update={
            "session_id": session.session_id,
            "run_id": run.run_id,
            "entitlements": [],
            "data_scopes": population_auth_context()
            .data_scopes.model_copy(update={"datasets": []}),
        }
    )
    builder = AgentContextBuilder(
        store=store,
        registry=ToolRegistry.default(),
        analysis_intent_presenter=AnalysisIntentToolPresenter(
            catalog=SemanticCatalog.default()
        ),
    )

    request = await builder.build(
        user_id="user-01",
        auth_context=auth_context,
        state=HarnessState(),
    )

    assert ANALYSIS_INTENT_TOOL_ID not in [tool.tool_id for tool in request.tools]


async def test_context_builder_keeps_semantic_and_finish_behavior_with_presenter() -> None:
    store = InMemoryAgentStore()
    service = SessionRunService(store)
    session = await service.create_session(user_id="user-01", title="共存")
    run = await service.create_run(
        user_id="user-01",
        session_id=session.session_id,
        request=run_request(),
    )
    auth_context = _area_capable_auth().model_copy(
        update={"session_id": session.session_id, "run_id": run.run_id}
    )
    default_builder = AgentContextBuilder(store=store, registry=ToolRegistry.default())
    baseline = await default_builder.build(
        user_id="user-01",
        auth_context=auth_context,
        state=HarnessState(),
    )
    builder = AgentContextBuilder(
        store=store,
        registry=ToolRegistry.default(),
        analysis_intent_presenter=AnalysisIntentToolPresenter(
            catalog=SemanticCatalog.default()
        ),
    )

    request = await builder.build(
        user_id="user-01",
        auth_context=auth_context,
        state=HarnessState(),
    )

    baseline_ids = [tool.tool_id for tool in baseline.tools]
    assert [
        tool.tool_id
        for tool in request.tools
        if tool.tool_id != ANALYSIS_INTENT_TOOL_ID
    ] == baseline_ids
    assert request.messages[0].content == baseline.messages[0].content


async def test_context_builder_with_only_intent_tool_keeps_model_reachable() -> None:
    store = InMemoryAgentStore()
    service = SessionRunService(store)
    session = await service.create_session(user_id="user-01", title="仅研判")
    run = await service.create_run(
        user_id="user-01",
        session_id=session.session_id,
        request=run_request(),
    )
    auth_context = _area_capable_auth().model_copy(
        update={"session_id": session.session_id, "run_id": run.run_id}
    )
    builder = AgentContextBuilder(
        store=store,
        registry=ToolRegistry(manifests=[], descriptors=[]),
        analysis_intent_presenter=AnalysisIntentToolPresenter(
            catalog=SemanticCatalog.default()
        ),
    )

    request = await builder.build(
        user_id="user-01",
        auth_context=auth_context,
        state=HarnessState(),
    )

    # 只有研判虚拟能力时工具表非空：ModelPlanner 的 no-tools 快停不得触发。
    assert [tool.tool_id for tool in request.tools] == [ANALYSIS_INTENT_TOOL_ID]
