from __future__ import annotations

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

from .test_mock_executor import RecordingCapability, StaticAuthContextProvider
from .test_session_run_service import run_request


class _OrderedStore(InMemoryAgentStore):
    def __init__(self, order: list[str]) -> None:
        super().__init__()
        self._order = order

    async def save_result(self, **kwargs):
        self._order.append("save_result")
        return await super().save_result(**kwargs)

    async def save_evidence(self, **kwargs):
        self._order.append("save_evidence")
        return await super().save_evidence(**kwargs)

    async def save_frontend_command(self, **kwargs):
        self._order.append("save_frontend_command")
        return await super().save_frontend_command(**kwargs)


class _OrderedEvents(InMemoryEventBroker):
    def __init__(self, order: list[str]) -> None:
        super().__init__()
        self._order = order

    async def publish(self, **kwargs):
        self._order.append(f"event:{kwargs['event_type']}")
        return await super().publish(**kwargs)


class _RejectingEvidenceStore(_OrderedStore):
    async def save_evidence(self, **_kwargs):
        self._order.append("save_evidence")
        raise RuntimeError("evidence persistence failed")


class _CancelAfterResultStore(_OrderedStore):
    async def save_result(self, **kwargs):
        result = await super().save_result(**kwargs)
        await self.cancel_run(
            user_id=kwargs["user_id"],
            run_id=kwargs["run_id"],
        )
        return result


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


def _tool_result() -> ToolResult:
    return ToolResult.model_validate(
        {
            "tool_call_id": "call-observation-01",
            "tool_id": "governance.query_population_metrics",
            "tool_version": "1.0.0",
            "status": "success",
            "summary": "查询完成",
            "data_result": {
                "result_id": "res-observation-01",
                "kind": "table",
                "data_schema_ref": "schema://data/population-metric-table/1.0.0",
                "result_fingerprint": "sha256:observation",
                "data": {"rows": []},
                "row_count": 0,
            },
        }
    )


async def _running_run(store: InMemoryAgentStore):
    service = SessionRunService(store)
    session = await service.create_session(user_id="user-01", title="观测持久化")
    queued = await service.create_run(
        user_id="user-01",
        session_id=session.session_id,
        request=run_request(),
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
        "save_result",
        "save_evidence",
        "event:result.available",
        "event:evidence.available",
        "save_frontend_command",
        "event:frontend.command.requested",
    ]


@pytest.mark.asyncio
async def test_observation_failure_does_not_publish_or_request_commands() -> None:
    order: list[str] = []
    store = _RejectingEvidenceStore(order)
    events = _OrderedEvents(order)
    _service, run = await _running_run(store)
    observations = ToolObservationService(
        store=store,
        events=events,
        registry=ToolRegistry.default(),
        evidence_source_system="test-source",
    )

    with pytest.raises(RuntimeError, match="evidence persistence failed"):
        await observations.persist(
            user_id="user-01",
            run=run,
            action=_action(),
            tool_result=_tool_result(),
        )

    assert order == ["save_result", "save_evidence"]
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

    assert "res-observation-01" not in store.results
    assert order == ["save_result"]
    assert await events.list_events(run_id=run.run_id) == []


@pytest.mark.asyncio
async def test_cancellation_between_result_and_evidence_fails_closed() -> None:
    order: list[str] = []
    store = _CancelAfterResultStore(order)
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

    assert "res-observation-01" in store.results
    assert store.evidence == {}
    assert order == ["save_result"]
    assert await events.list_events(run_id=run.run_id) == []


@pytest.mark.asyncio
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
