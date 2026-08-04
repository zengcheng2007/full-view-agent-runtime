"""C1: 生产模式五层接线 —— Adapter 存在、Registry 可见、用户有主题权限、
模型可选择、生产代码路径可执行（经 LangGraph 默认编排路径）。

生产代码路径契约测试（非真实 HTTP 验证）：legacy 网关由 httpx.MockTransport
承载，身份准入、授权、策略绑定、HTTP Adapter、LangGraph 编排均为生产代码
路径；真实登录环境验证保持待办。
"""

import json
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest
from pydantic import SecretStr

from full_view_agent.api.app import RuntimeContainer
from full_view_agent.application.capability_service import CapabilityService
from full_view_agent.application.context_builder import AgentContextBuilder
from full_view_agent.application.harness import ANALYSIS_INTENT_TOOL_ID, HarnessState
from full_view_agent.application.native_orchestrator import NativeOrchestrator
from full_view_agent.application.policy import MinimalPolicyAdapter
from full_view_agent.application.prompt_catalog import (
    FULL_VIEW_SYSTEM_PROMPT_VERSION,
    build_full_view_system_prompt,
)
from full_view_agent.application.semantic_executor import (
    SemanticToolCallFingerprinter,
    SemanticToolExecutor,
)
from full_view_agent.application.session_run_service import SessionRunService
from full_view_agent.application.tool_registry import (
    PRODUCTION_HTTP_TOOL_IDS,
    ToolRegistry,
)
from full_view_agent.domain import models
from full_view_agent.evaluation.http_environment import HttpEvalEnvironment
from full_view_agent.evaluation.loader import load_eval_case
from full_view_agent.evaluation.runner import EvalRunner
from full_view_agent.infrastructure.credential_broker import (
    InMemoryCredentialBroker,
)
from full_view_agent.infrastructure.governance_adapter import (
    HttpGovernanceAdapter,
    InMemoryGovernanceAdapter,
)
from full_view_agent.infrastructure.langgraph_orchestrator import (
    LangGraphOrchestrator,
)
from full_view_agent.infrastructure.legacy_identity import (
    HashedLegacyIdentityAdapter,
)
from full_view_agent.infrastructure.memory_store import InMemoryAgentStore

from .test_policy import population_auth_context
from .test_session_run_service import run_request

EVAL_CASES = Path(__file__).parents[1] / "evals" / "cases"

def _full_governance_auth_context() -> models.AuthContext:
    context = population_auth_context()
    return context.model_copy(
        update={
            "entitlements": [
                "governance.area.read",
                "governance.event.aggregate.read",
                "governance.housing.aggregate.read",
                "governance.population.aggregate.read",
            ],
            "data_scopes": context.data_scopes.model_copy(
                update={
                    "areas": [
                        models.AuthorizedAreaScope(
                            area_code="3301", include_descendants=True
                        )
                    ],
                    "datasets": [
                        "administrative_area",
                        "event",
                        "housing",
                        "population",
                    ],
                }
            ),
        }
    )


def _legacy_envelope(data: object) -> httpx.Response:
    return httpx.Response(
        200, json={"state": True, "code": 200, "msg": "", "data": data}
    )


def _http_runtime_container(
    *, housing_next_area_enabled: bool = False
) -> RuntimeContainer:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: _legacy_envelope([]))
    )
    adapter = HttpGovernanceAdapter(
        base_url="http://legacy.test/geo-qxst",
        credential_broker=InMemoryCredentialBroker(),
        client=client,
        housing_next_area_enabled=housing_next_area_enabled,
    )
    return RuntimeContainer(
        identity_port=HashedLegacyIdentityAdapter(),
        credentials=InMemoryCredentialBroker(),
        governance_adapter=adapter,
    )


# ---------------------------------------------------------------------------
# 层 1+2+4：生产 HTTP Registry 子集、模型可选择、提示词不宣称未接线能力
# ---------------------------------------------------------------------------


def test_production_http_registry_subset_matches_wired_capabilities() -> None:
    container = _http_runtime_container()

    assert container.tool_registry.list_tool_ids() == list(PRODUCTION_HTTP_TOOL_IDS)


@pytest.mark.asyncio
async def test_production_context_advertises_wired_aggregate_tools() -> None:
    container = _http_runtime_container()
    store = InMemoryAgentStore()
    service = SessionRunService(store)
    session = await service.create_session(user_id="user-wiring", title="接线")
    run = await service.create_run(
        user_id="user-wiring",
        session_id=session.session_id,
        request=run_request(),
    )
    auth_context = _full_governance_auth_context().model_copy(
        update={"session_id": session.session_id, "run_id": run.run_id}
    )

    request = await AgentContextBuilder(
        store=store,
        registry=container.tool_registry,
    ).build(
        user_id="user-wiring",
        auth_context=auth_context,
        state=HarnessState(),
    )

    assert [tool.tool_id for tool in request.tools] == list(PRODUCTION_HTTP_TOOL_IDS)
    prompt = request.messages[0].content
    assert "query_housing_metrics" in prompt
    assert "按租赁类型" in prompt
    assert "next_area" not in prompt
    housing_tool = next(
        tool
        for tool in request.tools
        if tool.tool_id == "governance.query_housing_metrics"
    )
    assert "next_area" not in json.dumps(
        {
            "description": housing_tool.description,
            "input_schema": housing_tool.input_schema,
        },
        ensure_ascii=False,
    )
    assert "query_event_metrics" in prompt
    assert "不支持按阈值筛选" in prompt
    assert "get_object_profile" not in prompt
    assert "base_room_lease" not in prompt
    assert "getNextSiteData" not in prompt
    assert "getRoomLeaseType" not in prompt
    housing_subject = container.semantic_stack.catalog.require_subject("housing")
    assert housing_subject.max_group_by == 0
    assert housing_subject.group_by_rules == ()
    assert [shape.shape_id for shape in housing_subject.result_shapes] == [
        "housing_lease_type_table"
    ]


@pytest.mark.asyncio
async def test_production_context_exposes_next_area_only_when_explicitly_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FULL_VIEW_HOUSING_NEXT_AREA_ENABLED", "true")
    container = _http_runtime_container(housing_next_area_enabled=True)
    store = InMemoryAgentStore()
    service = SessionRunService(store)
    session = await service.create_session(user_id="user-gated", title="门禁")
    run = await service.create_run(
        user_id="user-gated",
        session_id=session.session_id,
        request=run_request(),
    )
    auth_context = _full_governance_auth_context().model_copy(
        update={"session_id": session.session_id, "run_id": run.run_id}
    )

    request = await AgentContextBuilder(
        store=store,
        registry=container.tool_registry,
    ).build(
        user_id="user-gated",
        auth_context=auth_context,
        state=HarnessState(),
    )

    housing_tool = next(
        tool
        for tool in request.tools
        if tool.tool_id == "governance.query_housing_metrics"
    )
    model_surface = json.dumps(
        {
            "description": housing_tool.description,
            "input_schema": housing_tool.input_schema,
        },
        ensure_ascii=False,
    )
    assert "next_area" in request.messages[0].content
    assert "next_area" in model_surface
    assert container.semantic_stack.catalog.require_subject(
        "housing"
    ).max_group_by == 1


def test_runtime_rejects_http_adapter_flag_mismatch() -> None:
    adapter = HttpGovernanceAdapter(
        base_url="http://legacy.test/geo-qxst",
        credential_broker=InMemoryCredentialBroker(),
        housing_next_area_enabled=True,
    )

    with pytest.raises(
        RuntimeError, match="FULL_VIEW_HOUSING_NEXT_AREA_ENABLED"
    ):
        RuntimeContainer(
            identity_port=HashedLegacyIdentityAdapter(),
            credentials=InMemoryCredentialBroker(),
            governance_adapter=adapter,
        )


def test_prompt_only_lists_registered_and_authorized_capabilities() -> None:
    assert FULL_VIEW_SYSTEM_PROMPT_VERSION == "full-view-governance-readonly-v14"

    population_only = build_full_view_system_prompt(
        {}, tool_ids=("governance.query_population_metrics",)
    )
    assert "query_population_metrics" in population_only
    assert "区县按街道" in population_only
    assert "query_housing_metrics" not in population_only
    assert "query_event_metrics" not in population_only
    assert "get_object_profile" not in population_only

    production_subset = build_full_view_system_prompt(
        {}, tool_ids=tuple(PRODUCTION_HTTP_TOOL_IDS)
    )
    assert "resolve_area" in production_subset
    assert "query_housing_metrics" in production_subset
    assert "query_event_metrics" in production_subset
    assert "不支持按阈值筛选" in production_subset
    assert "get_object_profile" not in production_subset


# ---------------------------------------------------------------------------
# S1-A Native freeze：组合根在 native 回滚模式下仍须完整接线语义栈
# （执行器/指纹/模型上下文 Presenter），且不得借道 LangGraph 链路
# ---------------------------------------------------------------------------


class _StubModelProvider:
    """仅用于触发组合根构建 ModelPlanner，不会被调用。"""

    async def complete(self, request) -> object:
        del request
        raise NotImplementedError("stub provider must not be called in wiring tests")


def _assert_semantic_stack_wired(container: RuntimeContainer) -> None:
    executor = container.executor
    assert executor._capability is container.semantic_stack.capability  # noqa: SLF001
    harness = executor._harness  # noqa: SLF001
    assert isinstance(harness._tool_executor, SemanticToolExecutor)  # noqa: SLF001
    assert isinstance(  # noqa: SLF001
        harness._call_fingerprinter,  # noqa: SLF001
        SemanticToolCallFingerprinter,
    )
    # 模型上下文必须携带语义 Presenter：semantic_query 对模型可见。
    planner_factory = executor._planner_factory  # noqa: SLF001
    assert planner_factory is not None
    assert (  # noqa: SLF001
        planner_factory._context_builder._semantic_presenter  # noqa: SLF001
        is container.semantic_stack.presenter
    )


def test_runtime_container_native_freeze_wires_semantic_stack(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FULL_VIEW_ORCHESTRATOR", "native")

    container = RuntimeContainer(model_provider=_StubModelProvider())

    # Native 回滚路径必须是原生编排器本体，不得经由 LangGraph 子类。
    assert type(container.executor) is NativeOrchestrator
    assert not isinstance(container.executor, LangGraphOrchestrator)
    _assert_semantic_stack_wired(container)


def test_runtime_container_default_langgraph_wires_semantic_stack(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("FULL_VIEW_ORCHESTRATOR", raising=False)

    container = RuntimeContainer(model_provider=_StubModelProvider())

    assert isinstance(container.executor, LangGraphOrchestrator)
    _assert_semantic_stack_wired(container)


def test_runtime_container_does_not_wire_analysis_intent_presenter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # P2 研判意图虚拟能力在本切片保持未接线：默认生产组合根不注入
    # presenter，模型永远看不到 agent.request_regional_analysis。
    monkeypatch.delenv("FULL_VIEW_ORCHESTRATOR", raising=False)

    container = RuntimeContainer(model_provider=_StubModelProvider())

    planner_factory = container.executor._planner_factory  # noqa: SLF001
    assert planner_factory is not None
    assert (  # noqa: SLF001
        planner_factory._context_builder._analysis_intent_presenter  # noqa: SLF001
        is None
    )


@pytest.mark.asyncio
async def test_production_context_never_advertises_analysis_intent_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("FULL_VIEW_ORCHESTRATOR", raising=False)

    container = RuntimeContainer(model_provider=_StubModelProvider())
    # session/run 必须建在 container 自己的 store 上：被断言的
    # ContextBuilder 正是绑定该 store 的生产实例。
    assert container.store is not None
    service = SessionRunService(container.store)
    session = await service.create_session(user_id="user-wiring", title="研判")
    run = await service.create_run(
        user_id="user-wiring",
        session_id=session.session_id,
        request=run_request(),
    )
    auth_context = _full_governance_auth_context().model_copy(
        update={"session_id": session.session_id, "run_id": run.run_id}
    )
    planner_factory = container.executor._planner_factory  # noqa: SLF001
    assert planner_factory is not None

    request = await planner_factory._context_builder.build(  # noqa: SLF001
        user_id="user-wiring",
        auth_context=auth_context,
        state=HarnessState(),
    )

    assert ANALYSIS_INTENT_TOOL_ID not in [tool.tool_id for tool in request.tools]


# ---------------------------------------------------------------------------
# 层 3+5：生产代码路径契约测试（MockTransport，非真实网络）+
# 双编排器（Native 冻结 / LangGraph 默认）的直连 Tool 纵向切片；
# 真实登录环境验证待办
# ---------------------------------------------------------------------------


@pytest.fixture(params=["native", "langgraph"], ids=["native", "langgraph"])
def production_orchestrator(request: pytest.FixtureRequest) -> str:
    return request.param


def _housing_http_environment(
    seen_paths: list[str],
    bodies: dict[str, list[dict[str, list[str]]]],
    *,
    housing_next_area_enabled: bool = False,
) -> HttpEvalEnvironment:
    def handle(request: httpx.Request) -> httpx.Response:
        seen_paths.append(request.url.path)
        assert request.headers["geoToken"] == "test-geo-token"
        if request.url.path == "/getUserByToken":
            return _legacy_envelope(
                {
                    "systemid": "legacy-user-1",
                    "organizatedId": "legacy-org-1",
                    "roleIds": "1",
                    "areaCode": "3301",
                }
            )
        if request.url.path == "/geo-qxst/area/getAreaInfoByAreaName":
            return _legacy_envelope({"areaname": "西湖区", "areacode": "330106"})
        if request.url.path == "/geo-qxst/house/getRoomLeaseType":
            bodies.setdefault("lease_type", []).append(
                parse_qs(request.content.decode())
            )
            return _legacy_envelope(
                [
                    {"house_type": "住宅出租", "total": 32},
                    {"house_type": "商铺出租", "total": 8},
                ]
            )
        if request.url.path == "/geo-qxst/getNextSiteData":
            bodies.setdefault("next_area", []).append(
                parse_qs(request.content.decode())
            )
            return _legacy_envelope(
                [
                    {"areaCode": "330106001", "areaName": "翠苑街道", "total": 21},
                    {"areaCode": "330106002", "areaName": "文新街道", "total": 17},
                ]
            )
        if request.url.path == (
            "/geo-qxst/api/getEventPropertiesAndConflictsByTotal"
        ):
            bodies.setdefault("event_rate", []).append(
                {
                    key: request.url.params.get_list(key)
                    for key in request.url.params
                }
            )
            return _legacy_envelope(
                {
                    "gridFinishRate": "75.5%",
                    "communityFinishRate": "82.25%",
                    "streetFinishRate": "90%",
                }
            )
        raise AssertionError(f"unexpected path: {request.url.path}")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    return HttpEvalEnvironment(
        raw_token=SecretStr("test-geo-token"),
        legacy_gateway_url="http://legacy.test",
        governance_base_url="http://legacy.test/geo-qxst",
        p0_allowed_user_ids={"legacy-user-1"},
        client=client,
        housing_next_area_enabled=housing_next_area_enabled,
    )


@pytest.mark.asyncio
async def test_production_wiring_housing_lease_type_over_http(
    production_orchestrator: str,
) -> None:
    seen_paths: list[str] = []
    bodies: dict[str, list[dict[str, list[str]]]] = {}
    environment = _housing_http_environment(seen_paths, bodies)
    case = load_eval_case(EVAL_CASES / "planning-housing-http-success.yaml")

    trace = await EvalRunner(
        environment=environment,
        orchestrator=production_orchestrator,
    ).run(case)

    assert trace.passed is True
    assert trace.terminal_status == "completed"
    assert seen_paths == [
        "/getUserByToken",
        "/geo-qxst/area/getAreaInfoByAreaName",
        "/geo-qxst/house/getRoomLeaseType",
    ]
    assert bodies["lease_type"][0] == {
        "areaName": ["county_code"],
        "areaCode": ["330106"],
    }
    assert "governance.semantic_query" in trace.model_requests[0].tool_ids
    assert "governance.query_housing_metrics" not in trace.model_requests[0].tool_ids
    assert len(trace.evidence_ids) >= 2
    serialized = trace.model_dump_json()
    assert "test-geo-token" not in serialized
    assert "credential_ref" not in serialized
    lease_requests = [
        summary
        for summary in trace.outbound_requests
        if summary.path == "/geo-qxst/house/getRoomLeaseType"
    ]
    assert [
        (summary.method, summary.path, summary.count)
        for summary in lease_requests
    ] == [("POST", "/geo-qxst/house/getRoomLeaseType", 1)]


@pytest.mark.asyncio
async def test_production_wiring_housing_next_area_over_http(
    production_orchestrator: str,
) -> None:
    seen_paths: list[str] = []
    bodies: dict[str, list[dict[str, list[str]]]] = {}
    environment = _housing_http_environment(
        seen_paths, bodies, housing_next_area_enabled=True
    )
    case = load_eval_case(
        EVAL_CASES / "planning-housing-next-area-http-success.yaml"
    )

    trace = await EvalRunner(
        environment=environment,
        orchestrator=production_orchestrator,
    ).run(case)

    assert trace.passed is True
    assert bodies["next_area"][0] == {
        "areaName": ["county_code"],
        "areaCode": ["330106"],
        "tableName": ["base_room_lease"],
    }
    assert "governance.semantic_query" in trace.model_requests[0].tool_ids
    assert "governance.query_housing_metrics" not in trace.model_requests[0].tool_ids
    serialized = trace.model_dump_json()
    assert "test-geo-token" not in serialized
    next_area_requests = [
        summary
        for summary in trace.outbound_requests
        if summary.path == "/geo-qxst/getNextSiteData"
    ]
    assert [
        (summary.method, summary.path, summary.count)
        for summary in next_area_requests
    ] == [("POST", "/geo-qxst/getNextSiteData", 1)]


@pytest.mark.asyncio
async def test_production_wiring_event_finish_rate_over_http(
    production_orchestrator: str,
) -> None:
    seen_paths: list[str] = []
    bodies: dict[str, list[dict[str, list[str]]]] = {}
    environment = _housing_http_environment(seen_paths, bodies)
    case = load_eval_case(EVAL_CASES / "planning-event-http-success.yaml")

    trace = await EvalRunner(
        environment=environment,
        orchestrator=production_orchestrator,
    ).run(case)

    assert trace.passed is True
    assert trace.terminal_status == "completed"
    assert seen_paths == [
        "/getUserByToken",
        "/geo-qxst/area/getAreaInfoByAreaName",
        "/geo-qxst/api/getEventPropertiesAndConflictsByTotal",
    ]
    assert bodies["event_rate"][0] == {
        "areaCodeName": ["county_code"],
        "areaCodeValue": ["330106"],
    }
    assert "governance.semantic_query" in trace.model_requests[0].tool_ids
    assert "governance.query_event_metrics" not in trace.model_requests[0].tool_ids
    assert len(trace.evidence_ids) >= 2
    serialized = trace.model_dump_json()
    model_surface = json.dumps(
        [request.model_dump(mode="json") for request in trace.model_requests],
        ensure_ascii=False,
    )
    assert "test-geo-token" not in serialized
    assert "getEventPropertiesAndConflictsByTotal" not in model_surface
    assert any(
        summary.path
        == "/geo-qxst/api/getEventPropertiesAndConflictsByTotal"
        for summary in trace.outbound_requests
    )


@pytest.mark.asyncio
async def test_production_wiring_population_resolve_and_query_over_http(
    production_orchestrator: str,
) -> None:
    seen_paths: list[str] = []
    bodies: dict[str, list[dict[str, list[str]]]] = {}
    environment = _housing_http_environment(seen_paths, bodies)
    case = load_eval_case(EVAL_CASES / "planning-population-http-success.yaml")

    trace = await EvalRunner(
        environment=environment,
        orchestrator=production_orchestrator,
    ).run(case)

    assert trace.passed is True
    assert trace.terminal_status == "completed"
    assert seen_paths == [
        "/getUserByToken",
        "/geo-qxst/area/getAreaInfoByAreaName",
        "/geo-qxst/getNextSiteData",
    ]
    # S1-B：生产准入钉扎 governance_analyst_v1，人口查询经语义入口落到
    # 同一规范 HTTP 链路；Evidence 数量与凭据防泄漏契约保持不变。
    assert trace.tool_ids == [
        "governance.resolve_area",
        "governance.semantic_query",
    ]
    assert len(trace.evidence_ids) >= 2
    serialized = trace.model_dump_json()
    model_surface = json.dumps(
        [request.model_dump(mode="json") for request in trace.model_requests],
        ensure_ascii=False,
    )
    assert "test-geo-token" not in serialized
    assert "getNextSiteData" not in model_surface
    assert any(
        summary.path == "/geo-qxst/getNextSiteData"
        for summary in trace.outbound_requests
    )


# ---------------------------------------------------------------------------
# 权限层反例：无出租房主题权限 → 拒绝且模型不可见
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_capability_service_denies_housing_without_entitlement() -> None:
    service = CapabilityService(
        registry=ToolRegistry.default(),
        policy=MinimalPolicyAdapter(),
        adapter=InMemoryGovernanceAdapter(),
    )

    result = await service.execute(
        tool_call_id="tcl-denied",
        tool_id="governance.query_housing_metrics",
        raw_arguments={"query": {"scope": {"area_code": "330106"}}},
        auth_context=population_auth_context(),
    )

    assert result.status == "denied"
    assert "TOOL_NOT_ENTITLED" in result.warnings


@pytest.mark.asyncio
async def test_context_builder_hides_housing_without_dataset_authorization() -> None:
    store = InMemoryAgentStore()
    service = SessionRunService(store)
    session = await service.create_session(user_id="user-no-housing", title="无住房")
    run = await service.create_run(
        user_id="user-no-housing",
        session_id=session.session_id,
        request=run_request(),
    )
    base = population_auth_context()
    auth_context = base.model_copy(
        update={
            "session_id": session.session_id,
            "run_id": run.run_id,
            "entitlements": [
                "governance.population.aggregate.read",
                "governance.housing.aggregate.read",
            ],
            "data_scopes": base.data_scopes.model_copy(
                update={"datasets": ["population"]}
            ),
        }
    )

    request = await AgentContextBuilder(
        store=store,
        registry=ToolRegistry.default(),
    ).build(
        user_id="user-no-housing",
        auth_context=auth_context,
        state=HarnessState(),
    )

    advertised = [tool.tool_id for tool in request.tools]
    assert "governance.query_housing_metrics" not in advertised


def test_trace_round_trip_does_not_expose_physical_names_to_model() -> None:
    case = load_eval_case(EVAL_CASES / "planning-housing-next-area-http-success.yaml")
    serialized = json.dumps(case.model_dump(mode="json"), ensure_ascii=False)

    assert "base_room_lease" not in serialized
    assert "getNextSiteData" not in serialized
