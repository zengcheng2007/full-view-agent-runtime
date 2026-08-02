"""S1-A 生产接线：语义入口端到端（Native/LangGraph 双路径）。

覆盖：Presenter fail-closed 与目录派生、ContextBuilder 虚拟 Tool 可见性、
语义查询全链路（解析→规范执行→Evidence 血缘→FrontendCommand）、
基于规范动作的循环检测、连续追问不误判、恢复语义（重新解析 fail closed）。
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from full_view_agent.application.context_builder import AgentContextBuilder
from full_view_agent.application.harness import FinishAction, HarnessState, ToolAction
from full_view_agent.application.native_orchestrator import NativeOrchestrator
from full_view_agent.application.semantic_wiring import (
    build_semantic_capability_stack,
)
from full_view_agent.application.session_run_service import SessionRunService
from full_view_agent.application.tool_registry import ToolRegistry
from full_view_agent.domain.models import (
    AuthContext,
    PopulationMetricRow,
    PopulationMetricTable,
    RunCreateRequest,
    TableDataResult,
    ToolResult,
)
from full_view_agent.infrastructure.event_broker import InMemoryEventBroker
from full_view_agent.infrastructure.governance_adapter import (
    InMemoryGovernanceAdapter,
)
from full_view_agent.infrastructure.langgraph_orchestrator import (
    LangGraphOrchestrator,
)
from full_view_agent.infrastructure.memory_store import InMemoryAgentStore
from full_view_agent.semantic.action_resolver import (
    SEMANTIC_QUERY_TOOL_ID,
    RejectedSemanticAction,
)
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


def _subject_auth(subject: str) -> AuthContext:
    """按主题派生授权上下文：entitlement 与数据集跟随主题。"""
    base = population_auth_context()
    return base.model_copy(
        update={
            "entitlements": [f"governance.{subject}.aggregate.read"],
            "data_scopes": base.data_scopes.model_copy(
                update={"datasets": [subject]}
            ),
        }
    )


def _housing_args() -> dict[str, object]:
    return {
        "catalog_version": SemanticCatalog.default().catalog_version,
        "catalog_fingerprint": SemanticCatalog.default().execution_fingerprint,
        "spec": {
            "subject": "housing",
            "metrics": ["dwelling_count"],
            "scope": {"area_code": "330106"},
        },
    }


def _event_args() -> dict[str, object]:
    return {
        "catalog_version": SemanticCatalog.default().catalog_version,
        "catalog_fingerprint": SemanticCatalog.default().execution_fingerprint,
        "spec": {
            "subject": "event",
            "metrics": ["finish_rate"],
            "scope": {"area_code": "330106"},
        },
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


class _DirectPlanner:
    """直接规范 Tool 规划器：承载与语义入口等价的规范调用。"""

    def __init__(self, arguments: dict[str, object]) -> None:
        self._arguments = arguments

    async def decide(self, state: HarnessState) -> ToolAction | FinishAction:
        if state.tool_results:
            return FinishAction(summary="直接规范 Tool 查询已完成。")
        return ToolAction(
            tool_id="governance.query_population_metrics",
            arguments=self._arguments,
        )


class _ExplodingAdapter:
    """抛出未预期异常的 Adapter：验证语义入口 fail-closed 收敛。"""

    async def execute(self, **kwargs: object) -> object:
        del kwargs
        raise RuntimeError("unexpected upstream failure")


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
    adapter: object | None = None,
):
    store = InMemoryAgentStore()
    events = InMemoryEventBroker()
    service = SessionRunService(store)
    # 通用语义链路测试使用生产安全默认：住房 next_area 关闭。
    registry = ToolRegistry.default(housing_next_area_enabled=False)
    stack = build_semantic_capability_stack(
        registry=registry,
        adapter=adapter or InMemoryGovernanceAdapter(),
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


def test_presenter_returns_none_for_declared_but_unbound_subjects_only() -> None:
    # 仅有 housing 权限：主题在 Catalog 声明但没有已验证能力绑定 →
    # 可执行集合不含该主题，语义 Tool 对模型不可见（fail closed）。
    base = SemanticCatalog.default()
    partial = SemanticCatalog(
        catalog_version=base.catalog_version,
        supported_spec_versions=base.supported_spec_versions,
        subjects=base.subjects,
        bindings={"population": base.bindings["population"]},
    )
    presenter = SemanticToolPresenter(catalog=partial)
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
    # 所有已绑定主题都只通过统一语义入口对模型暴露；规范 Tool 仍保留在
    # Registry/CapabilityService 内部供编译结果兼容执行。
    assert presentation.shadowed_tool_ids == (
        "governance.query_event_metrics",
        "governance.query_housing_metrics",
        "governance.query_population_metrics",
    )
    description = presentation.description
    # 目录事实出现。
    assert catalog.catalog_version in description
    assert "population" in description
    assert "person_count" in description
    assert "solitary_elderly" in description
    # 描述按本次授权过滤：仅人口授权时不宣称住房/事件能力。
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
    assert tool_ids == [SEMANTIC_QUERY_TOOL_ID]
    assert "governance.query_population_metrics" not in tool_ids
    prompt = request.messages[0].content
    assert prompt is not None
    assert "语义查询入口说明" in prompt
    assert "semantic_query" in prompt
    assert "governance.query_population_metrics" not in prompt
    assert request.prompt_version == "full-view-governance-readonly-v13"


@pytest.mark.asyncio
async def test_context_builder_keeps_unbound_subject_tools_visible() -> None:
    # 目录未绑定住房能力时：住房规范 Tool 保持可见（尚未被语义入口接管），
    # 人口规范 Tool 由语义入口接管而隐藏；绑定增减驱动接管面，无需改代码。
    base_catalog = SemanticCatalog.default()
    partial = SemanticCatalog(
        catalog_version=base_catalog.catalog_version,
        supported_spec_versions=base_catalog.supported_spec_versions,
        subjects=base_catalog.subjects,
        bindings={"population": base_catalog.bindings["population"]},
    )
    store = InMemoryAgentStore()
    service = SessionRunService(store)
    session = await service.create_session(user_id="user-mixed", title="混合主题")
    run = await service.create_run(
        user_id="user-mixed", session_id=session.session_id, request=run_request()
    )
    base = population_auth_context()
    auth = base.model_copy(
        update={
            "session_id": session.session_id,
            "run_id": run.run_id,
            "entitlements": [
                "governance.population.aggregate.read",
                "governance.housing.aggregate.read",
            ],
            "data_scopes": base.data_scopes.model_copy(
                update={"datasets": ["population", "housing"]}
            ),
        }
    )
    request = await AgentContextBuilder(
        store=store,
        registry=ToolRegistry.default(),
        semantic_presenter=SemanticToolPresenter(catalog=partial),
    ).build(
        user_id="user-mixed",
        auth_context=auth,
        state=HarnessState(),
    )

    tool_ids = [tool.tool_id for tool in request.tools]
    assert "governance.query_population_metrics" not in tool_ids
    assert "governance.query_housing_metrics" in tool_ids
    assert SEMANTIC_QUERY_TOOL_ID in tool_ids


@pytest.mark.asyncio
async def test_context_builder_shadows_all_bindable_canonical_tools() -> None:
    # 全授权下，三个已绑定主题的规范 Tool 全部从模型面隐藏；统一语义
    # 入口描述同时宣称三主题能力。
    store = InMemoryAgentStore()
    service = SessionRunService(store)
    session = await service.create_session(user_id="user-all", title="全主题入口")
    run = await service.create_run(
        user_id="user-all", session_id=session.session_id, request=run_request()
    )
    base = population_auth_context()
    auth = base.model_copy(
        update={
            "session_id": session.session_id,
            "run_id": run.run_id,
            "entitlements": [
                "governance.area.read",
                "governance.population.aggregate.read",
                "governance.housing.aggregate.read",
                "governance.event.aggregate.read",
            ],
            "data_scopes": base.data_scopes.model_copy(
                update={
                    "datasets": [
                        "administrative_area",
                        "population",
                        "housing",
                        "event",
                    ]
                }
            ),
        }
    )
    request = await AgentContextBuilder(
        store=store,
        registry=ToolRegistry.default(),
        semantic_presenter=SemanticToolPresenter(catalog=SemanticCatalog.default()),
    ).build(
        user_id="user-all",
        auth_context=auth,
        state=HarnessState(),
    )

    tool_ids = [tool.tool_id for tool in request.tools]
    assert SEMANTIC_QUERY_TOOL_ID in tool_ids
    assert "governance.query_population_metrics" not in tool_ids
    assert "governance.query_housing_metrics" not in tool_ids
    assert "governance.query_event_metrics" not in tool_ids
    # 语义入口描述宣称全部已绑定且已授权的主题（模型可见入口）。
    prompt = request.messages[0].content
    assert prompt is not None
    for token in ("population", "housing", "event"):
        assert token in prompt


def test_presenter_description_advertises_all_authorized_bound_subjects() -> None:
    # 模型可见入口：全授权时住房/事件/人口逐字段出现在语义入口描述中，
    # 全部由 Catalog 派生，不手写能力清单。
    base = population_auth_context()
    auth = base.model_copy(
        update={
            "entitlements": [
                "governance.population.aggregate.read",
                "governance.housing.aggregate.read",
                "governance.event.aggregate.read",
            ],
            "data_scopes": base.data_scopes.model_copy(
                update={"datasets": ["population", "housing", "event"]}
            ),
        }
    )
    presentation = SemanticToolPresenter(catalog=SemanticCatalog.default()).present(
        auth_context=auth
    )

    assert presentation is not None
    description = presentation.description
    for token in (
        "population",
        "person_count",
        "housing",
        "dwelling_count",
        "event",
        "finish_rate",
    ):
        assert token in description
    # 物理实现不泄漏。
    for token in ("getNextSiteData", "adapter://", "getRoomLeaseType"):
        assert token not in description


def test_presenter_explains_catalog_derived_housing_result_grains() -> None:
    auth = _subject_auth("housing")
    presentation = SemanticToolPresenter(
        catalog=SemanticCatalog.default(housing_next_area_enabled=True)
    ).present(auth_context=auth)

    assert presentation is not None
    description = presentation.description
    assert "group_by=[]（不传 group_by）" in description
    assert "按租赁类型汇总" in description
    assert "group_by=['next_area']" in description
    assert "next_area（直接下级区划）" in description
    assert "按直接下级区划汇总" in description
    for internal_field in ("lease_type", "area_name"):
        assert internal_field not in description
    for token in ("schema://", "data_schema_ref", "adapter://", "getNextSiteData"):
        assert token not in description


def test_presenter_keeps_next_area_absent_when_production_gate_is_closed() -> None:
    presentation = SemanticToolPresenter(catalog=SemanticCatalog.default()).present(
        auth_context=_subject_auth("housing")
    )

    assert presentation is not None
    assert "group_by=[]（不传 group_by）" in presentation.description
    assert "按租赁类型汇总" in presentation.description
    assert "next_area" not in presentation.description


def test_presenter_result_grains_follow_catalog_without_internal_field_leak() -> None:
    base = SemanticCatalog.default(housing_next_area_enabled=True)
    housing = base.require_subject("housing")
    changed_shapes = (
        housing.result_shapes[0].model_copy(
            update={
                "grain_label": "按目录定义的自定义业务粒度",
                "row_fields": ("dm_secret_table_column",),
                "data_schema_ref": "schema://private/secret-table/9.9",
            }
        ),
    ) + housing.result_shapes[1:]
    changed = SemanticCatalog(
        catalog_version=base.catalog_version,
        supported_spec_versions=base.supported_spec_versions,
        subjects={
            **base.subjects,
            "housing": housing.model_copy(update={"result_shapes": changed_shapes}),
        },
        bindings=base.bindings,
    )

    presentation = SemanticToolPresenter(catalog=changed).present(
        auth_context=_subject_auth("housing")
    )

    assert presentation is not None
    assert "按目录定义的自定义业务粒度" in presentation.description
    assert "dm_secret_table_column" not in presentation.description
    assert "schema://private" not in presentation.description


def test_presenter_never_leaks_unvalidated_grain_label_from_custom_catalog() -> None:
    base = SemanticCatalog.default()
    housing = base.require_subject("housing")
    bypassed_validation_shape = housing.result_shapes[0].model_copy(
        update={
            "grain_label": "AdApTeR_ReF SELECT_secret_FROM_dm_private_table",
            "row_fields": ("dm_secret_column",),
        }
    )
    changed = SemanticCatalog(
        catalog_version=base.catalog_version,
        supported_spec_versions=base.supported_spec_versions,
        subjects={
            **base.subjects,
            "housing": housing.model_copy(
                update={"result_shapes": (bypassed_validation_shape,)}
            ),
        },
        bindings=base.bindings,
    )

    presentation = SemanticToolPresenter(catalog=changed).present(
        auth_context=_subject_auth("housing")
    )

    assert presentation is not None
    assert "AdApTeR_ReF" not in presentation.description
    assert "SELECT_secret" not in presentation.description
    assert "dm_secret" not in presentation.description
    assert "按所选查询维度返回结果" in presentation.description


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


@pytest.mark.asyncio
async def test_context_builder_population_takeover_fails_closed_for_unknown_policy() -> None:
    store = InMemoryAgentStore()
    service = SessionRunService(store)
    session = await service.create_session(user_id="user-policy", title="字段策略")
    run = await service.create_run(
        user_id="user-policy", session_id=session.session_id, request=run_request()
    )
    base = population_auth_context()
    auth = base.model_copy(
        update={
            "session_id": session.session_id,
            "run_id": run.run_id,
            "data_scopes": base.data_scopes.model_copy(
                update={"field_policy_set": "unsupported_policy"}
            ),
        }
    )

    request = await AgentContextBuilder(
        store=store,
        registry=ToolRegistry.default(),
        semantic_presenter=SemanticToolPresenter(catalog=SemanticCatalog.default()),
    ).build(
        user_id="user-policy",
        auth_context=auth,
        state=HarnessState(),
    )

    tool_ids = [tool.tool_id for tool in request.tools]
    assert SEMANTIC_QUERY_TOOL_ID not in tool_ids
    assert "governance.query_population_metrics" not in tool_ids
    assert "governance.query_population_metrics" not in request.messages[0].content


@pytest.mark.asyncio
async def test_context_builder_keeps_semantic_entry_for_distinct_follow_up_query() -> None:
    store = InMemoryAgentStore()
    service = SessionRunService(store)
    session = await service.create_session(user_id="user-followup", title="连续查询")
    run = await service.create_run(
        user_id="user-followup", session_id=session.session_id, request=run_request()
    )
    auth = population_auth_context().model_copy(
        update={"session_id": session.session_id, "run_id": run.run_id}
    )
    state = HarnessState(
        tool_results=(
            ToolResult(
                tool_call_id="tcl-semantic-01",
                tool_id="governance.query_population_metrics",
                tool_version="1.0.0",
                status="success",
                summary="首次人口查询完成",
                data_result=TableDataResult(
                    result_id="res-semantic-01",
                    data_schema_ref="schema://data/population-metric-table/1.0.0",
                    result_fingerprint="sha256:semantic-01",
                    data=PopulationMetricTable(
                        rows=[
                            PopulationMetricRow(
                                area_code="330106001",
                                area_name="示例街道",
                                person_count=128,
                            )
                        ]
                    ),
                    row_count=1,
                ),
            ),
        )
    )

    request = await AgentContextBuilder(
        store=store,
        registry=ToolRegistry.default(),
        semantic_presenter=SemanticToolPresenter(catalog=SemanticCatalog.default()),
    ).build(
        user_id="user-followup",
        auth_context=auth,
        state=state,
    )

    tool_ids = [tool.tool_id for tool in request.tools]
    assert SEMANTIC_QUERY_TOOL_ID in tool_ids
    assert "governance.query_population_metrics" not in tool_ids


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
@pytest.mark.parametrize(
    ("args_builder", "subject", "canonical_tool", "metric"),
    [
        (
            _housing_args,
            "housing",
            "governance.query_housing_metrics",
            "dwelling_count",
        ),
        (
            _event_args,
            "event",
            "governance.query_event_metrics",
            "finish_rate",
        ),
    ],
    ids=["housing", "event"],
)
async def test_housing_and_event_semantic_query_end_to_end_both_orchestrators(
    orchestrator_type: type,
    args_builder: Callable[[], dict[str, object]],
    subject: str,
    canonical_tool: str,
    metric: str,
) -> None:
    # 住房/事件与人口走同一统一入口：Native 与 LangGraph 双编排器均须
    # 解析到既有规范 Tool，落 Evidence 血缘，不产生第二份执行路径。
    orch, store, events, run_id, _stack = await _semantic_orchestrator(
        orchestrator_type=orchestrator_type,
        planner=_SemanticPlanner([args_builder()]),
        auth=_subject_auth(subject),
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
    started = next(event for event in published if event.type == "tool.started")
    assert started.data["tool_id"] == SEMANTIC_QUERY_TOOL_ID

    evidence_ids = sorted(store.evidence)
    assert len(evidence_ids) == 1
    evidence = await store.get_evidence(
        user_id="user-semantic", evidence_id=evidence_ids[0]
    )
    assert evidence.dataset_id == subject
    assert evidence.tool.tool_id == canonical_tool
    assert [m.metric_id for m in evidence.metric_definitions] == [metric]
    assert (
        evidence.semantic_registry_version
        == SemanticCatalog.default().catalog_version
    )
    assert evidence.effective_area_codes == ["330106"]

    # 住房租赁汇总与事件办结率快照均为表格形态：仅发表格面板命令，
    # 命令序列在两种编排器间一致（无固定地区名、无第二套命令路径）。
    commands = [
        event.data["command"]
        for event in published
        if event.type == "frontend.command.requested"
    ]
    assert [command["type"] for command in commands] == ["panel.show_table"]
    assert commands[0]["preconditions"]["area_code"] == "330106"


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


# ---------------------------------------------------------------------------
# S1-A Native freeze：语义入口与等价直接 Tool 的输出契约差分（direct
# differential）；未预期异常 fail-closed 收敛为 internal_error。
# ---------------------------------------------------------------------------


async def _sole_evidence(store, user_id: str):
    evidence_ids = sorted(store.evidence)
    assert len(evidence_ids) == 1
    return await store.get_evidence(
        user_id=user_id,
        evidence_id=evidence_ids[0],
    )


def _command_signatures(events) -> list[tuple[str, str | None, str]]:
    return sorted(
        (
            command["type"],
            command["preconditions"]["area_code"],
            command["preconditions"]["required_client_capability"],
        )
        for event in events
        if event.type == "frontend.command.requested"
        for command in [event.data["command"]]
    )


@pytest.mark.asyncio
async def test_semantic_query_contract_matches_direct_canonical_tool(
    orchestrator_type: type,
) -> None:
    # 语义入口 vs 等价直接规范 Tool：结果/Evidence/前端命令契约对齐，
    # 语义侧仅额外携带血缘登记信息；双编排器均须满足。
    auth = population_auth_context()
    semantic_orch, semantic_store, semantic_events, semantic_run_id, stack = (
        await _semantic_orchestrator(
            orchestrator_type=orchestrator_type,
            planner=_SemanticPlanner([_semantic_args()]),
            auth=auth,
        )
    )
    compiled = stack.resolver.compile_action(_semantic_args(), auth_context=auth)
    assert not isinstance(compiled, RejectedSemanticAction)
    direct_orch, direct_store, direct_events, direct_run_id, _stack = (
        await _semantic_orchestrator(
            orchestrator_type=orchestrator_type,
            planner=_DirectPlanner(compiled.canonical_action.arguments),
            auth=auth,
        )
    )

    await semantic_orch.execute(user_id="user-semantic", run_id=semantic_run_id)
    await direct_orch.execute(user_id="user-semantic", run_id=direct_run_id)

    semantic_run = await semantic_store.get_run(
        user_id="user-semantic", run_id=semantic_run_id
    )
    direct_run = await direct_store.get_run(
        user_id="user-semantic", run_id=direct_run_id
    )
    assert (semantic_run.status, semantic_run.outcome) == ("completed", "success")
    assert (direct_run.status, direct_run.outcome) == (
        semantic_run.status,
        semantic_run.outcome,
    )
    assert direct_run.completion_reason_code == semantic_run.completion_reason_code

    # 事件序列一致（含 result/evidence/frontend.command 生命周期）。
    semantic_events_list = await semantic_events.list_events(run_id=semantic_run_id)
    direct_events_list = await direct_events.list_events(run_id=direct_run_id)
    assert [event.type for event in semantic_events_list] == [
        event.type for event in direct_events_list
    ]

    # 结果负载一致：同数据、同行数、同结果指纹。
    semantic_evidence = await _sole_evidence(semantic_store, "user-semantic")
    direct_evidence = await _sole_evidence(direct_store, "user-semantic")
    semantic_result = await semantic_store.get_result(
        user_id="user-semantic", result_id=semantic_evidence.result_id
    )
    direct_result = await direct_store.get_result(
        user_id="user-semantic", result_id=direct_evidence.result_id
    )
    assert semantic_result.data.model_dump(mode="json") == direct_result.data.model_dump(
        mode="json"
    )
    assert semantic_result.row_count == direct_result.row_count
    assert semantic_result.result_fingerprint == direct_result.result_fingerprint

    # Evidence：规范 Tool 字段一致；语义侧额外携带登记版本与指标口径。
    assert semantic_evidence.dataset_id == direct_evidence.dataset_id
    assert semantic_evidence.effective_area_codes == direct_evidence.effective_area_codes
    assert semantic_evidence.effective_area_codes == ["330106"]
    assert semantic_evidence.tool == direct_evidence.tool
    assert semantic_evidence.tool.tool_id == "governance.query_population_metrics"
    assert direct_evidence.semantic_registry_version is None
    assert direct_evidence.metric_definitions == []
    assert (
        semantic_evidence.semantic_registry_version
        == SemanticCatalog.default().catalog_version
    )

    # FrontendCommand：命令集合与契约前置条件一致（兼容现有前端契约）。
    assert _command_signatures(semantic_events_list) == _command_signatures(
        direct_events_list
    )
    assert _command_signatures(semantic_events_list) == [
        ("map.render_choropleth", "330106", "map.render_choropleth@1.0"),
        ("panel.show_table", "330106", "panel.show_table@1.0"),
    ]


@pytest.mark.asyncio
async def test_semantic_unexpected_failure_fails_closed_as_internal_error(
    orchestrator_type: type,
) -> None:
    # 语义解析成功但下游抛出未预期异常：Run 必须 fail-closed 为
    # internal_error，不得落 Evidence，不得伪装成功。
    orch, store, events, run_id, _stack = await _semantic_orchestrator(
        orchestrator_type=orchestrator_type,
        planner=_SemanticPlanner([_semantic_args()]),
        adapter=_ExplodingAdapter(),
    )

    await orch.execute(user_id="user-semantic", run_id=run_id)

    run = await store.get_run(user_id="user-semantic", run_id=run_id)
    assert run.status == "failed"
    assert run.completion_reason_code == "internal_error"
    assert not store.evidence
    published = await events.list_events(run_id=run_id)
    failed = next(event for event in published if event.type == "run.failed")
    assert failed.data["error_code"] == "internal_error"
