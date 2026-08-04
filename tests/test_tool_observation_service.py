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
