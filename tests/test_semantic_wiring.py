"""S1-A 生产接线：语义入口端到端（Native/LangGraph 双路径）。

覆盖：Presenter fail-closed 与目录派生、ContextBuilder 虚拟 Tool 可见性、
语义查询全链路（解析→规范执行→Evidence 血缘→FrontendCommand）、
基于规范动作的循环检测、连续追问不误判、恢复语义（重新解析 fail closed）。
"""

from __future__ import annotations

import pytest

from full_view_agent.application.context_builder import AgentContextBuilder
from full_view_agent.application.harness import FinishAction, HarnessState, ToolAction
from full_view_agent.application.native_orchestrator import NativeOrchestrator
from full_view_agent.application.semantic_wiring import (
    build_semantic_capability_stack,
)
from full_view_agent.application.session_run_service import SessionRunService
from full_view_agent.application.tool_registry import ToolRegistry
from full_view_agent.domain.models import AuthContext, RunCreateRequest
from full_view_agent.infrastructure.event_broker import InMemoryEventBroker
from full_view_agent.infrastructure.governance_adapter import (
    InMemoryGovernanceAdapter,
)
from full_view_agent.infrastructure.langgraph_orchestrator import (
    LangGraphOrchestrator,
)
from full_view_agent.infrastructure.memory_store import InMemoryAgentStore
from full_view_agent.semantic.action_resolver import SEMANTIC_QUERY_TOOL_ID
from full_view_agent.semantic.catalog import SemanticCatalog
from full_view_agent.semantic.presenter import SemanticToolPresenter

from .test_policy import population_auth_context
from .test_session_run_service import run_request


def _semantic_args(
    area_code: str = "330106",
    group_by: str = "street",
    output: str = "table",
) -> dict[str, object]:
    return {
        "catalog_version": SemanticCatalog.default().catalog_version,
        "catalog_fingerprint": SemanticCatalog.default().execution_fingerprint,
        "spec": {
            "subject": "population",
            "metrics": ["person_count"],
            "scope": {"area_code": area_code},
            "filters": [
                {
                    "field": "person_category",
                    "operator": "eq",
                    "value": "solitary_elderly",
                }
            ],
            "group_by": [group_by],
            "output": output,
        }
    }


class _StaticAuth:
    def __init__(self, ctx: AuthContext) -> None:
        self._ctx = ctx

    async def get(self, *, user_id: str, run_id: str) -> AuthContext:
        del user_id, run_id
        return self._ctx


class _SemanticPlanner:
    def __init__(self, calls: list[dict[str, object]]) -> None:
        self._calls = list(calls)
        self._index = 0

    async def decide(self, state: HarnessState) -> ToolAction | FinishAction:
        if state.tool_results:
            self._index += 1
        if self._index < len(self._calls):
            return ToolAction(
                tool_id=SEMANTIC_QUERY_TOOL_ID,
                arguments=self._calls[self._index],
            )
        return FinishAction(summary="语义查询已完成，结果已生成。")


class _RepeatingSemanticPlanner:
    async def decide(self, state: HarnessState) -> ToolAction | FinishAction:
        del state
        return ToolAction(
            tool_id=SEMANTIC_QUERY_TOOL_ID,
            arguments=_semantic_args(),
        )


def _map_enabled_request(message_id: str = "web-semantic-01") -> RunCreateRequest:
    return RunCreateRequest.model_validate(
        {
            "input": {
                "client_message_id": message_id,
                "content": [{"type": "text", "text": "查询西湖区独居老人数量"}],
            },
            "client": {
                "client_instance_id": "cli-semantic",
                "frontend_command_schema_versions": ["1.0"],
                "supported_commands": ["panel.show_table", "map.render_choropleth"],
            },
            "mode": "agent",
        }
    )


async def _semantic_orchestrator(
    *,
    orchestrator_type: type,
    planner: object,
    auth: AuthContext | None = None,
    request: RunCreateRequest | None = None,
):
    store = InMemoryAgentStore()
    events = InMemoryEventBroker()
    service = SessionRunService(store)
    registry = ToolRegistry.default()
    stack = build_semantic_capability_stack(
        registry=registry,
        adapter=InMemoryGovernanceAdapter(),
    )

    class _PF:
        def create(self, *, user_id: str, auth_context: AuthContext) -> object:
            del user_id, auth_context
            return planner

    orch = orchestrator_type(
        service=service,
        store=store,
        events=events,
        auth_context_provider=_StaticAuth(auth or population_auth_context()),
        capability=stack.capability,
        registry=registry,
        planner_factory=_PF(),
        harness=stack.build_harness(),
    )
    session = await service.create_session(user_id="user-semantic", title="语义切片")
    run = await service.create_run(
        user_id="user-semantic",
        session_id=session.session_id,
        request=request or _map_enabled_request(),
    )
    return orch, store, events, run.run_id, stack


# ---------------------------------------------------------------------------
# Presenter：fail closed + 目录派生
# ---------------------------------------------------------------------------


def test_presenter_returns_none_without_population_authorization() -> None:
    presenter = SemanticToolPresenter(catalog=SemanticCatalog.default())
    auth = population_auth_context().model_copy(update={"entitlements": []})

    assert presenter.present(auth_context=auth) is None


def test_presenter_returns_none_for_unbindable_catalog_subjects_only() -> None:
    # 仅有 housing 权限：Catalog 声明存在，但 S1-A 不绑定 → 不可见。
    presenter = SemanticToolPresenter(catalog=SemanticCatalog.default())
    auth = population_auth_context().model_copy(
        update={
            "entitlements": ["governance.housing.aggregate.read"],
            "data_scopes": population_auth_context().data_scopes.model_copy(
                update={"datasets": ["housing"]}
            ),
        }
    )

    assert presenter.present(auth_context=auth) is None


def test_presenter_derives_description_from_catalog_not_handwritten_list() -> None:
    catalog = SemanticCatalog.default()
    presenter = SemanticToolPresenter(catalog=catalog)

    presentation = presenter.present(auth_context=population_auth_context())

    assert presentation is not None
    assert presentation.tool_id == SEMANTIC_QUERY_TOOL_ID
    description = presentation.description
    # 目录事实出现。
    assert catalog.catalog_version in description
    assert "population" in description
    assert "person_count" in description
    assert "solitary_elderly" in description
    # 未绑定主题不宣称。
    assert "housing" not in description
    assert "event" not in description
    # 物理实现不泄漏。
    assert "getNextSiteData" not in description
    assert "adapter://" not in description
    assert "governance.query_population_metrics" not in description
    # 输入 Schema 为受控语义契约。
    assert presentation.input_schema["type"] == "object"
    assert "spec" in presentation.input_schema.get("properties", {})


@pytest.mark.asyncio
async def test_context_builder_advertises_semantic_tool_and_prompt_section() -> None:
    store = InMemoryAgentStore()
    service = SessionRunService(store)
    session = await service.create_session(user_id="user-ctx", title="语义上下文")
    run = await service.create_run(
        user_id="user-ctx", session_id=session.session_id, request=run_request()
    )
    auth = population_auth_context().model_copy(
        update={"session_id": session.session_id, "run_id": run.run_id}
    )
    builder = AgentContextBuilder(
        store=store,
        registry=ToolRegistry.default(),
        semantic_presenter=SemanticToolPresenter(catalog=SemanticCatalog.default()),
    )

    request = await builder.build(
        user_id="user-ctx", auth_context=auth, state=HarnessState()
    )

    tool_ids = [tool.tool_id for tool in request.tools]
    assert SEMANTIC_QUERY_TOOL_ID in tool_ids
    prompt = request.messages[0].content
    assert prompt is not None
    assert "语义查询入口说明" in prompt
    assert "semantic_query" in prompt
    assert request.prompt_version == "full-view-governance-readonly-v10"


@pytest.mark.asyncio
async def test_context_builder_hides_semantic_tool_without_authorization() -> None:
    store = InMemoryAgentStore()
    service = SessionRunService(store)
    session = await service.create_session(user_id="user-noauth", title="无授权")
    run = await service.create_run(
        user_id="user-noauth", session_id=session.session_id, request=run_request()
    )
    auth = population_auth_context().model_copy(
        update={
            "session_id": session.session_id,
            "run_id": run.run_id,
            "entitlements": [],
        }
    )
    builder = AgentContextBuilder(
        store=store,
        registry=ToolRegistry.default(),
        semantic_presenter=SemanticToolPresenter(catalog=SemanticCatalog.default()),
    )

    request = await builder.build(
        user_id="user-noauth", auth_context=auth, state=HarnessState()
    )

    assert SEMANTIC_QUERY_TOOL_ID not in [tool.tool_id for tool in request.tools]


# ---------------------------------------------------------------------------
# 端到端：Native 与 LangGraph 双路径
# ---------------------------------------------------------------------------


@pytest.fixture(params=[NativeOrchestrator, LangGraphOrchestrator], ids=["native", "langgraph"])
def orchestrator_type(request: pytest.FixtureRequest):
    return request.param


@pytest.mark.asyncio
async def test_semantic_query_end_to_end_success_with_lineage_and_commands(
    orchestrator_type: type,
) -> None:
    orch, store, events, run_id, _stack = await _semantic_orchestrator(
        orchestrator_type=orchestrator_type,
        planner=_SemanticPlanner([_semantic_args()]),
    )

    await orch.execute(user_id="user-semantic", run_id=run_id)

    run = await store.get_run(user_id="user-semantic", run_id=run_id)
    assert run.status == "completed"
    assert run.outcome == "success"

    published = await events.list_events(run_id=run_id)
    types = [event.type for event in published]
    assert "tool.completed" in types
    assert "result.available" in types
    assert "evidence.available" in types

    # 模型层事件保留原始语义动作。
    started = next(event for event in published if event.type == "tool.started")
    assert started.data["tool_id"] == SEMANTIC_QUERY_TOOL_ID

    # Evidence 携带语义血缘：登记版本与指标口径。
    evidence_ids = sorted(store.evidence)
    assert len(evidence_ids) == 1
    evidence = await store.get_evidence(
        user_id="user-semantic", evidence_id=evidence_ids[0]
    )
    assert evidence.semantic_registry_version == SemanticCatalog.default().catalog_version
    assert [m.metric_id for m in evidence.metric_definitions] == ["person_count"]
    assert evidence.effective_area_codes == ["330106"]
    assert evidence.tool.tool_id == "governance.query_population_metrics"

    # FrontendCommand：表格面板 + 人口分级设色地图。
    commands = [
        event.data["command"]
        for event in published
        if event.type == "frontend.command.requested"
    ]
    command_types = sorted(command["type"] for command in commands)
    assert command_types == ["map.render_choropleth", "panel.show_table"]
    for command in commands:
        assert command["preconditions"]["area_code"] == "330106"


@pytest.mark.asyncio
async def test_semantic_query_choropleth_output_still_issues_map_command(
    orchestrator_type: type,
) -> None:
    orch, store, events, run_id, _stack = await _semantic_orchestrator(
        orchestrator_type=orchestrator_type,
        planner=_SemanticPlanner([_semantic_args(output="choropleth")]),
    )

    await orch.execute(user_id="user-semantic", run_id=run_id)

    published = await events.list_events(run_id=run_id)
    command_types = sorted(
        event.data["command"]["type"]
        for event in published
        if event.type == "frontend.command.requested"
    )
    assert "map.render_choropleth" in command_types
    run = await store.get_run(user_id="user-semantic", run_id=run_id)
    assert run.status == "completed"


@pytest.mark.asyncio
async def test_semantic_query_without_map_capability_only_shows_table(
    orchestrator_type: type,
) -> None:
    orch, store, events, run_id, _stack = await _semantic_orchestrator(
        orchestrator_type=orchestrator_type,
        planner=_SemanticPlanner([_semantic_args()]),
        request=run_request("web-semantic-table-only"),
    )

    await orch.execute(user_id="user-semantic", run_id=run_id)

    published = await events.list_events(run_id=run_id)
    command_types = [
        event.data["command"]["type"]
        for event in published
        if event.type == "frontend.command.requested"
    ]
    assert command_types == ["panel.show_table"]


@pytest.mark.asyncio
async def test_repeated_identical_semantic_query_is_loop_detected(
    orchestrator_type: type,
) -> None:
    orch, store, events, run_id, _stack = await _semantic_orchestrator(
        orchestrator_type=orchestrator_type,
        planner=_RepeatingSemanticPlanner(),
    )

    await orch.execute(user_id="user-semantic", run_id=run_id)

    run = await store.get_run(user_id="user-semantic", run_id=run_id)
    assert run.status == "failed"
    assert run.completion_reason_code == "loop_detected"
    published = await events.list_events(run_id=run_id)
    started_count = sum(
        1 for event in published if event.type == "tool.started"
    )
    # repeated_call_limit=2：第三次等价调用前即阻断。
    assert started_count == 2


@pytest.mark.asyncio
async def test_synonymous_spec_does_not_evade_repetition_detection(
    orchestrator_type: type,
) -> None:
    # 键序不同的同义 spec：规范指纹相同，仍计入重复调用。
    reordered = {
        "catalog_version": SemanticCatalog.default().catalog_version,
        "catalog_fingerprint": SemanticCatalog.default().execution_fingerprint,
        "spec": {
            "group_by": ["street"],
            "output": "table",
            "scope": {"include_descendants": True, "area_code": "330106"},
            "filters": [
                {"value": "solitary_elderly", "operator": "eq", "field": "person_category"}
            ],
            "metrics": ["person_count"],
            "subject": "population",
        }
    }
    orch, store, _events, run_id, _stack = await _semantic_orchestrator(
        orchestrator_type=orchestrator_type,
        planner=_SemanticPlanner([_semantic_args(), reordered, _semantic_args()]),
    )

    await orch.execute(user_id="user-semantic", run_id=run_id)

    run = await store.get_run(user_id="user-semantic", run_id=run_id)
    assert run.status == "failed"
    assert run.completion_reason_code == "loop_detected"


@pytest.mark.asyncio
async def test_distinct_followup_queries_are_not_false_positive_loops(
    orchestrator_type: type,
) -> None:
    # 区县→街道两级不同查询：不得误判为循环。
    orch, store, _events, run_id, _stack = await _semantic_orchestrator(
        orchestrator_type=orchestrator_type,
        planner=_SemanticPlanner(
            [
                _semantic_args(area_code="330106", group_by="street"),
                _semantic_args(area_code="330106001", group_by="community"),
            ]
        ),
    )

    await orch.execute(user_id="user-semantic", run_id=run_id)

    run = await store.get_run(user_id="user-semantic", run_id=run_id)
    assert run.status == "completed"
    assert run.outcome == "success"
    evidence_ids = sorted(store.evidence)
    assert len(evidence_ids) == 2


@pytest.mark.asyncio
async def test_semantic_rejection_completes_run_without_evidence(
    orchestrator_type: type,
) -> None:
    # 越权区域：语义层 denied，Run 以 denied 终态闭合，无 Evidence。
    orch, store, _events, run_id, _stack = await _semantic_orchestrator(
        orchestrator_type=orchestrator_type,
        planner=_SemanticPlanner([_semantic_args(area_code="330108")]),
    )

    await orch.execute(user_id="user-semantic", run_id=run_id)

    run = await store.get_run(user_id="user-semantic", run_id=run_id)
    assert run.status == "completed"
    assert run.outcome == "denied"
    assert run.completion_reason_code == "AREA_OUT_OF_SCOPE"
    assert not store.evidence
