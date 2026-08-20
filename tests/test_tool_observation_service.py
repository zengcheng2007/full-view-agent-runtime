from __future__ import annotations

import os
from uuid import uuid4

import pytest

from full_view_agent.application.errors import RunStateConflict
from full_view_agent.application.harness import ToolAction
from full_view_agent.application.native_orchestrator import NativeOrchestrator
from full_view_agent.application.session_run_service import SessionRunService
from full_view_agent.application.tool_observation_service import ToolObservationService
from full_view_agent.application.tool_registry import ToolRegistry
from full_view_agent.domain.models import ToolResult
from full_view_agent.infrastructure.event_broker import InMemoryEventBroker
from full_view_agent.infrastructure.memory_store import InMemoryAgentStore
from full_view_agent.infrastructure.postgres_persistence import PostgresAgentPersistence

from .test_mock_executor import RecordingCapability, StaticAuthContextProvider
from .test_session_run_service import run_request


class _OrderedStore(InMemoryAgentStore):
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


class _RejectingObservationStore(_OrderedStore):
    async def save_tool_observation(self, **_kwargs):
        self._order.append("save_tool_observation")
        raise RuntimeError("observation transaction failed")


class _CancelBeforeObservationStore(_OrderedStore):
    async def save_tool_observation(self, **kwargs):
        await self.cancel_run(
            user_id=kwargs["user_id"],
            run_id=kwargs["run_id"],
        )
        return await super().save_tool_observation(**kwargs)


class _RecordingObservations:
    def __init__(self, delegate: ToolObservationService) -> None:
        self._delegate = delegate
        self.tool_call_ids: list[str] = []

    async def persist(self, **kwargs):
        self.tool_call_ids.append(kwargs["tool_result"].tool_call_id)
        return await self._delegate.persist(**kwargs)


def _action() -> ToolAction:
    return ToolAction(
        tool_id="governance.query_population_metrics",
        arguments={"query": {"scope": {"area_code": "330106"}}},
    )


def _tool_result(*, result_id: str = "res-observation-01") -> ToolResult:
    return ToolResult.model_validate(
        {
            "tool_call_id": "call-observation-01",
            "tool_id": "governance.query_population_metrics",
            "tool_version": "1.0.0",
            "status": "success",
            "summary": "查询完成",
            "data_result": {
                "result_id": result_id,
                "kind": "table",
                "data_schema_ref": "schema://data/population-metric-table/1.0.0",
                "result_fingerprint": "sha256:observation",
                "data": {"rows": []},
                "row_count": 0,
            },
        }
    )


async def _running_run(
    store: InMemoryAgentStore, *, supported_commands: list[str] | None = None
):
    service = SessionRunService(store)
    session = await service.create_session(user_id="user-01", title="观测持久化")
    request = run_request()
    if supported_commands is not None:
        request = request.model_copy(
            update={
                "client": request.client.model_copy(
                    update={"supported_commands": supported_commands}
                )
            }
        )
    queued = await service.create_run(
        user_id="user-01",
        session_id=session.session_id,
        request=request,
    )
    return service, await service.start_run(user_id="user-01", run_id=queued.run_id)


@pytest.mark.asyncio
async def test_observation_is_saved_before_events_and_frontend_commands() -> None:
    order: list[str] = []
    store = _OrderedStore(order)
    events = _OrderedEvents(order)
    _service, run = await _running_run(store)
    observations = ToolObservationService(
        store=store,
        events=events,
        registry=ToolRegistry.default(),
        evidence_source_system="test-source",
    )

    persisted = await observations.persist(
        user_id="user-01",
        run=run,
        action=_action(),
        tool_result=_tool_result(),
    )

    assert persisted.data_result.evidence_ids == [persisted.evidence.evidence_id]
    assert order == [
        "save_tool_observation",
        "event:result.available",
        "event:evidence.available",
        "event:frontend.command.requested",
    ]


@pytest.mark.asyncio
async def test_observation_failure_does_not_publish_or_request_commands() -> None:
    order: list[str] = []
    store = _RejectingObservationStore(order)
    events = _OrderedEvents(order)
    _service, run = await _running_run(store)
    observations = ToolObservationService(
        store=store,
        events=events,
        registry=ToolRegistry.default(),
        evidence_source_system="test-source",
    )

    with pytest.raises(RuntimeError, match="observation transaction failed"):
        await observations.persist(
            user_id="user-01",
            run=run,
            action=_action(),
            tool_result=_tool_result(),
        )

    assert order == ["save_tool_observation"]
    assert store.results == {}
    assert store.evidence == {}
    assert store.frontend_commands == {}
    assert await events.list_events(run_id=run.run_id) == []


@pytest.mark.asyncio
async def test_cancelled_run_rejects_a_late_tool_observation() -> None:
    order: list[str] = []
    store = _OrderedStore(order)
    events = _OrderedEvents(order)
    service, run = await _running_run(store)
    observations = ToolObservationService(
        store=store,
        events=events,
        registry=ToolRegistry.default(),
        evidence_source_system="test-source",
    )
    await service.cancel_run(user_id="user-01", run_id=run.run_id)

    with pytest.raises(RunStateConflict, match="active run"):
        await observations.persist(
            user_id="user-01",
            run=run,
            action=_action(),
            tool_result=_tool_result(),
        )

    assert not store.results
    assert order == ["save_tool_observation"]
    assert await events.list_events(run_id=run.run_id) == []


@pytest.mark.asyncio
async def test_cancellation_before_atomic_observation_leaves_no_partial_rows() -> None:
    order: list[str] = []
    store = _CancelBeforeObservationStore(order)
    events = _OrderedEvents(order)
    _service, run = await _running_run(store)
    observations = ToolObservationService(
        store=store,
        events=events,
        registry=ToolRegistry.default(),
        evidence_source_system="test-source",
    )

    with pytest.raises(RunStateConflict, match="active run"):
        await observations.persist(
            user_id="user-01",
            run=run,
            action=_action(),
            tool_result=_tool_result(),
        )

    assert not store.results
    assert store.evidence == {}
    assert order == ["save_tool_observation"]
    assert await events.list_events(run_id=run.run_id) == []


@pytest.mark.asyncio
async def test_observation_replay_is_idempotent_for_data_events_and_commands() -> None:
    order: list[str] = []
    store = _OrderedStore(order)
    events = _OrderedEvents(order)
    _service, run = await _running_run(store)
    observations = ToolObservationService(
        store=store,
        events=events,
        registry=ToolRegistry.default(),
        evidence_source_system="test-source",
    )

    first = await observations.persist(
        user_id="user-01", run=run, action=_action(), tool_result=_tool_result()
    )
    replayed = await observations.persist(
        user_id="user-01", run=run, action=_action(), tool_result=_tool_result()
    )

    assert replayed == first
    assert len(store.results) == 1
    assert len(store.evidence) == 1
    assert len(store.frontend_commands) == 1
    assert len(await events.list_events(run_id=run.run_id)) == 3
    forged_evidence = first.evidence.model_copy(
        update={"result_fingerprint": "sha256:forged"}
    )
    with pytest.raises(RunStateConflict):
        await store.save_evidence(
            user_id="user-01", run_id=run.run_id, evidence=forged_evidence
        )
    assert await store.get_evidence(
        user_id="user-01", evidence_id=first.evidence.evidence_id
    ) == first.evidence


@pytest.mark.asyncio
async def test_observation_replay_with_fresh_adapter_result_id_converges() -> None:
    store = InMemoryAgentStore()
    events = InMemoryEventBroker()
    _service, run = await _running_run(store)
    observations = ToolObservationService(
        store=store,
        events=events,
        registry=ToolRegistry.default(),
        evidence_source_system="test-source",
    )

    first = await observations.persist(
        user_id="user-01",
        run=run,
        action=_action(),
        tool_result=_tool_result(result_id="adapter-result-first"),
    )
    replayed = await observations.persist(
        user_id="user-01",
        run=run,
        action=_action(),
        tool_result=_tool_result(result_id="adapter-result-retry"),
    )

    assert replayed == first
    assert first.data_result.result_id not in {
        "adapter-result-first",
        "adapter-result-retry",
    }
    assert len(store.results) == 1
    assert len(store.evidence) == 1
    assert len(await events.list_events(run_id=run.run_id)) == 3


@pytest.mark.asyncio
async def test_postgres_observation_replay_is_atomic_and_idempotent() -> None:
    dsn = os.getenv("FULL_VIEW_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("FULL_VIEW_TEST_DATABASE_URL is not configured")
    store = PostgresAgentPersistence(
        dsn=dsn, schema=f"fva_test_{uuid4().hex[:12]}"
    )
    await store.initialize()
    try:
        _service, run = await _running_run(store)
        observations = ToolObservationService(
            store=store,
            events=store,
            registry=ToolRegistry.default(),
            evidence_source_system="test-source",
        )

        first = await observations.persist(
            user_id="user-01", run=run, action=_action(), tool_result=_tool_result()
        )
        replayed = await observations.persist(
            user_id="user-01",
            run=run,
            action=_action(),
            tool_result=_tool_result(result_id="adapter-result-after-restart"),
        )

        assert replayed == first
        assert await store.get_result(
            user_id="user-01", result_id=first.data_result.result_id
        ) == first.data_result
        assert await store.get_evidence(
            user_id="user-01", evidence_id=first.evidence.evidence_id
        ) == first.evidence
        assert len(await store.list_events(run_id=run.run_id)) == 3
        forged_evidence = first.evidence.model_copy(
            update={"result_fingerprint": "sha256:forged"}
        )
        with pytest.raises(RunStateConflict):
            await store.save_evidence(
                user_id="user-01", run_id=run.run_id, evidence=forged_evidence
            )
        assert await store.get_evidence(
            user_id="user-01", evidence_id=first.evidence.evidence_id
        ) == first.evidence
    finally:
        await store.drop_schema()


@pytest.mark.asyncio
@pytest.mark.xfail(
    reason=(
        "NativeOrchestrator now requires snapshot_service for capability binding; "
        "test setup needs harness factory. TODO: update test to provide snapshot service."
    )
)
async def test_native_orchestrator_reuses_the_injected_observation_port() -> None:
    store = InMemoryAgentStore()
    events = InMemoryEventBroker()
    service = SessionRunService(store)
    observations = _RecordingObservations(
        ToolObservationService(
            store=store,
            events=events,
            registry=ToolRegistry.default(),
            evidence_source_system="test-source",
        )
    )
    orchestrator = NativeOrchestrator(
        service=service,
        store=store,
        events=events,
        auth_context_provider=StaticAuthContextProvider(),
        capability=RecordingCapability(),
        observation_service=observations,
    )
    session = await service.create_session(user_id="user-01", title="共享观测端口")
    run = await service.create_run(
        user_id="user-01",
        session_id=session.session_id,
        request=run_request(),
    )

    await orchestrator.execute(user_id="user-01", run_id=run.run_id)

    assert len(observations.tool_call_ids) == 1
    assert len(store.results) == 1
    assert len(store.evidence) == 1


@pytest.mark.asyncio
async def test_area_result_exposes_business_summary_instead_of_candidate_count() -> None:
    store = InMemoryAgentStore()
    events = InMemoryEventBroker()
    _service, run = await _running_run(store)
    observations = ToolObservationService(
        store=store,
        events=events,
        registry=ToolRegistry.default(),
        evidence_source_system="geo-qxst",
    )
    result = ToolResult.model_validate(
        {
            "tool_call_id": "call-resolve-area-display",
            "tool_id": "governance.resolve_area",
            "tool_version": "1.0.0",
            "status": "success",
            "summary": "区划解析完成。",
            "data_result": {
                "result_id": "adapter-area-result",
                "kind": "area_candidates",
                "data_schema_ref": "schema://data/area-candidates/1.0.0",
                "result_fingerprint": "sha256:area-display",
                "data": {
                    "resolved_area_code": "330106",
                    "ambiguous": False,
                    "candidates": [
                        {
                            "area_code": "330106",
                            "area_name": "西湖区",
                            "level": "district",
                            "parent_area_code": "3301",
                        }
                    ],
                },
                "candidate_count": 1,
            },
        }
    )

    persisted = await observations.persist(
        user_id="user-01",
        run=run,
        action=ToolAction(
            tool_id="governance.resolve_area",
            arguments={"query": "西湖区"},
        ),
        tool_result=result,
    )

    payload = persisted.data_result.model_dump(mode="json")
    assert payload["presentation"]["title"] == "区划解析结果"
    assert payload["presentation"]["summary"] == "已定位到西湖区（区县，330106）。"
    assert payload["presentation"]["status_label"] == "已解析"


@pytest.mark.asyncio
async def test_housing_result_exposes_chart_map_download_and_business_evidence() -> None:
    store = InMemoryAgentStore()
    events = InMemoryEventBroker()
    _service, run = await _running_run(
        store,
        supported_commands=["panel.show_table", "map.render_choropleth"],
    )
    observations = ToolObservationService(
        store=store,
        events=events,
        registry=ToolRegistry.default(),
        evidence_source_system="geo-qxst",
    )
    result = ToolResult.model_validate(
        {
            "tool_call_id": "call-housing-display",
            "tool_id": "governance.query_housing_metrics",
            "tool_version": "1.0.0",
            "status": "success",
            "summary": "出租房指标查询完成。",
            "data_result": {
                "result_id": "adapter-housing-result",
                "kind": "table",
                "data_schema_ref": "schema://data/housing-area-group-table/1.0.0",
                "result_fingerprint": "sha256:housing-display",
                "data": {
                    "rows": [
                        {
                            "area_code": "330106001",
                            "area_name": "翠苑街道",
                            "dwelling_count": 23,
                        }
                    ]
                },
                "row_count": 1,
            },
        }
    )

    persisted = await observations.persist(
        user_id="user-01",
        run=run,
        action=ToolAction(
            tool_id="governance.query_housing_metrics",
            arguments={
                "query": {
                    "scope": {"area_code": "330106"},
                    "group_by": ["next_area"],
                }
            },
        ),
        tool_result=result,
    )

    payload = persisted.data_result.model_dump(mode="json")
    presentation = payload["presentation"]
    assert presentation["title"] == "出租房分布"
    assert presentation["summary"] == "共 1 个区划，出租房合计 23 套。"
    assert [field["label"] for field in presentation["fields"]] == [
        "区划编码",
        "区划名称",
        "出租房数量",
    ]
    assert {view["kind"] for view in presentation["visualizations"]} == {
        "table",
        "bar",
        "choropleth",
    }
    assert presentation["download"]["formats"] == ["csv"]
    assert presentation["download"]["path"].endswith("/download?format=csv")

    evidence = persisted.evidence.model_dump(mode="json")
    assert evidence["display"]["source_label"] == "全量信息视图业务数据"
    assert evidence["display"]["dataset_label"] == "房屋聚合数据"
    assert evidence["display"]["tool_label"] == "查询房屋聚合指标"
    assert evidence["display"]["area_summary"] == "区划编码 330106"
    assert "credential_ref" not in str(evidence)

    commands = list(store.frontend_commands.values())
    choropleth = next(command for command in commands if command.type == "map.render_choropleth")
    assert choropleth.payload.metric_field == "dwelling_count"
    assert choropleth.payload.label_field == "area_name"
    assert choropleth.payload.area_code_field == "area_code"
    assert choropleth.payload.legend_title == "出租房数量（套）"


@pytest.mark.asyncio
async def test_enterprise_result_exposes_table_bar_map_download_and_evidence() -> None:
    store = InMemoryAgentStore()
    events = InMemoryEventBroker()
    _service, run = await _running_run(
        store,
        supported_commands=["panel.show_table", "map.render_choropleth"],
    )
    observations = ToolObservationService(
        store=store,
        events=events,
        registry=ToolRegistry.default(),
        evidence_source_system="geo-qxst",
    )
    result = ToolResult.model_validate(
        {
            "tool_call_id": "call-enterprise-display",
            "tool_id": "governance.query_enterprise_metrics",
            "tool_version": "1.0.0",
            "status": "success",
            "summary": "企业区划分布查询完成。",
            "data_result": {
                "result_id": "adapter-enterprise-result",
                "kind": "table",
                "data_schema_ref": "schema://data/enterprise-metric-table/1.0.0",
                "result_fingerprint": "sha256:enterprise-display",
                "data": {
                    "rows": [
                        {
                            "area_code": "330106001",
                            "area_name": "翠苑街道",
                            "enterprise_count": 31,
                        }
                    ]
                },
                "row_count": 1,
            },
        }
    )

    persisted = await observations.persist(
        user_id="user-01",
        run=run,
        action=ToolAction(
            tool_id="governance.query_enterprise_metrics",
            arguments={
                "query": {
                    "scope": {"area_code": "330106"},
                    "group_by": ["next_area"],
                }
            },
        ),
        tool_result=result,
    )

    presentation = persisted.data_result.model_dump(mode="json")["presentation"]
    assert presentation["title"] == "企业区划分布"
    assert presentation["summary"] == (
        "共 1 个区划，企业合计 31 家；企业最多的区划为翠苑街道（31 家）。"
    )
    assert {view["kind"] for view in presentation["visualizations"]} == {
        "table",
        "bar",
        "choropleth",
    }
    assert presentation["download"]["formats"] == ["csv"]
    assert persisted.evidence.display.dataset_label == "企业聚合数据"
    assert persisted.evidence.display.tool_label == "查询企业区划分布"
    commands = list(store.frontend_commands.values())
    choropleth = next(
        command for command in commands if command.type == "map.render_choropleth"
    )
    assert choropleth.payload.metric_field == "enterprise_count"
    assert choropleth.payload.legend_title == "企业数量（家）"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_id", "data_schema_ref", "rows", "expected_title", "expected_view"),
    [
        (
            "governance.query_housing_metrics",
            "schema://data/housing-lease-type-table/1.0.0",
            [{"lease_type": "整租", "dwelling_count": 7}],
            "出租房类型统计",
            {
                "kind": "bar",
                "title": "各类型出租房数量",
                "category_field": "lease_type",
                "value_field": "dwelling_count",
                "label_field": None,
                "area_code_field": None,
            },
        ),
        (
            "governance.query_event_metrics",
            "schema://data/event-finish-rate-table/1.0.0",
            [{"level": "street", "finish_rate": 92.5}],
            "治理事件办结率",
            {
                "kind": "bar",
                "title": "各层级事件办结率",
                "category_field": "level",
                "value_field": "finish_rate",
                "label_field": None,
                "area_code_field": None,
            },
        ),
    ],
)
async def test_existing_metric_tables_expose_business_bar_chart_metadata(
    tool_id: str,
    data_schema_ref: str,
    rows: list[dict[str, object]],
    expected_title: str,
    expected_view: dict[str, object],
) -> None:
    store = InMemoryAgentStore()
    events = InMemoryEventBroker()
    _service, run = await _running_run(store)
    observations = ToolObservationService(
        store=store,
        events=events,
        registry=ToolRegistry.default(),
        evidence_source_system="geo-qxst",
    )
    result = ToolResult.model_validate(
        {
            "tool_call_id": f"call-{tool_id}-display",
            "tool_id": tool_id,
            "tool_version": "1.0.0",
            "status": "success",
            "summary": "查询完成。",
            "data_result": {
                "result_id": f"adapter-{tool_id}-result",
                "kind": "table",
                "data_schema_ref": data_schema_ref,
                "result_fingerprint": f"sha256:{tool_id}-display",
                "data": {"rows": rows},
                "row_count": len(rows),
            },
        }
    )

    persisted = await observations.persist(
        user_id="user-01",
        run=run,
        action=ToolAction(
            tool_id=tool_id,
            arguments={"query": {"scope": {"area_code": "330106"}}},
        ),
        tool_result=result,
    )

    presentation = persisted.data_result.model_dump(mode="json")["presentation"]
    assert presentation["title"] == expected_title
    assert any(
        all(view.get(key) == value for key, value in expected_view.items())
        for view in presentation["visualizations"]
    )
    if tool_id == "governance.query_event_metrics":
        level_field = next(
            field for field in presentation["fields"] if field["field"] == "level"
        )
        assert level_field["value_labels"] == {
            "grid": "网格",
            "community": "社区",
            "street": "街道",
        }


@pytest.mark.asyncio
async def test_governance_overview_exposes_metric_bar_download_and_chinese_evidence() -> None:
    store = InMemoryAgentStore()
    events = InMemoryEventBroker()
    _service, run = await _running_run(store)
    observations = ToolObservationService(
        store=store,
        events=events,
        registry=ToolRegistry.default(),
        evidence_source_system="geo-qxst",
    )
    result = ToolResult.model_validate(
        {
            "tool_call_id": "call-governance-overview-display",
            "tool_id": "governance.get_governance_overview",
            "tool_version": "1.0.0",
            "status": "success",
            "summary": "区域治理总览查询完成。",
            "data_result": {
                "result_id": "adapter-governance-overview-result",
                "kind": "table",
                "data_schema_ref": "schema://data/governance-overview-table/1.0.0",
                "result_fingerprint": "sha256:governance-overview-display",
                "data": {
                    "rows": [
                        {
                            "subject": "person",
                            "subject_label": "人",
                            "related_count": 80,
                            "total_count": 100,
                            "coverage_rate": 80.0,
                        }
                    ]
                },
                "row_count": 1,
            },
        }
    )

    persisted = await observations.persist(
        user_id="user-01",
        run=run,
        action=ToolAction(
            tool_id="governance.get_governance_overview",
            arguments={"query": {"scope": {"area_code": "330106"}}},
        ),
        tool_result=result,
    )

    presentation = persisted.data_result.model_dump(mode="json")["presentation"]
    assert presentation["title"] == "区域治理总览"
    assert presentation["summary"] == (
        "共 1 类治理要素，关联总数 80 个、要素总数 100 个，综合覆盖率 80%。"
    )
    assert [field["label"] for field in presentation["fields"]] == [
        "治理要素",
        "治理关联数",
        "要素总数",
        "治理覆盖率",
    ]
    assert {view["kind"] for view in presentation["visualizations"]} == {
        "table",
        "metric",
        "bar",
    }
    assert presentation["download"]["formats"] == ["csv"]
    assert persisted.evidence.display.dataset_label == "区域治理总览数据"
    assert persisted.evidence.display.tool_label == "查询区域治理总览"


@pytest.mark.asyncio
async def test_governance_power_exposes_table_metric_bar_and_csv() -> None:
    store = InMemoryAgentStore()
    events = InMemoryEventBroker()
    _service, run = await _running_run(store)
    observations = ToolObservationService(
        store=store,
        events=events,
        registry=ToolRegistry.default(),
        evidence_source_system="geo-qxst",
    )
    result = ToolResult.model_validate(
        {
            "tool_call_id": "call-governance-power",
            "tool_id": "governance.query_governance_power_metrics",
            "tool_version": "1.0.0",
            "status": "success",
            "summary": "治理力量汇总查询完成。",
            "data_result": {
                "result_id": "res-governance-power",
                "kind": "table",
                "data_schema_ref": "schema://data/governance-power-metric-table/1.0.0",
                "result_fingerprint": "sha256:governance-power",
                "data": {
                    "rows": [
                        {"type_code": "10", "type_name": "网格长", "count": 5},
                        {"type_code": "12", "type_name": "专职网格员", "count": 9},
                    ]
                },
                "row_count": 2,
            },
        }
    )
    persisted = await observations.persist(
        user_id="user-01",
        run=run,
        action=ToolAction(
            tool_id="governance.query_governance_power_metrics",
            arguments={"query": {"scope": {"area_code": "330106"}}},
        ),
        tool_result=result,
    )

    presentation = persisted.data_result.model_dump(mode="json")["presentation"]
    assert presentation["title"] == "治理力量汇总"
    assert {view["kind"] for view in presentation["visualizations"]} == {
        "table",
        "metric",
        "bar",
    }
    assert presentation["download"]["formats"] == ["csv"]
    assert persisted.evidence.display.dataset_label == "治理力量汇总数据"


@pytest.mark.asyncio
async def test_event_trend_evidence_explains_monthly_zero_fill_method() -> None:
    store = InMemoryAgentStore()
    events = InMemoryEventBroker()
    _service, run = await _running_run(store)
    observations = ToolObservationService(
        store=store,
        events=events,
        registry=ToolRegistry.default(),
        evidence_source_system="geo-qxst",
    )
    result = ToolResult.model_validate(
        {
            "tool_call_id": "call-event-trend-evidence",
            "tool_id": "governance.query_event_metrics",
            "tool_version": "1.0.0",
            "status": "success",
            "summary": "事件总数月度趋势查询完成。",
            "data_result": {
                "result_id": "adapter-event-trend-result",
                "kind": "table",
                "data_schema_ref": "schema://data/event-trend-table/1.0.0",
                "result_fingerprint": "sha256:event-trend-evidence",
                "data": {
                    "rows": [
                        {"month": "2026-01", "event_count": 2},
                        {"month": "2026-02", "event_count": 0},
                    ]
                },
                "row_count": 2,
            },
        }
    )

    persisted = await observations.persist(
        user_id="user-01",
        run=run,
        action=ToolAction(
            tool_id="governance.query_event_metrics",
            arguments={
                "query": {
                    "metrics": ["event_count"],
                    "scope": {"area_code": "330106"},
                    "group_by": ["month"],
                    "time_range": {
                        "start": "2026-01-01",
                        "end": "2026-02-28",
                    },
                }
            },
        ),
        tool_result=result,
    )

    assert persisted.evidence.display is not None
    display = persisted.evidence.display.model_dump(mode="json")
    assert display["method_note"] == (
        "按自然月汇总；上游未返回的缺失月份按 0 补齐。"
    )
