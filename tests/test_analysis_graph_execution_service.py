"""Production analysis graph execution through Harness and durable stores."""

import pytest

from full_view_agent.application.analysis_graph_execution_service import (
    AnalysisGraphExecutionService,
)
from full_view_agent.application.analysis_observation_validator import (
    AgentStoreAnalysisObservationValidator,
)
from full_view_agent.application.session_run_service import SessionRunService
from full_view_agent.application.tool_observation_service import ToolObservationService
from full_view_agent.application.tool_registry import ToolRegistry
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

from .test_analysis_executor import (
    _executor,
    _full_auth_context,
    _overview_plan,
    _seed_result_store,
)


async def _runtime():
    _legacy_executor, port, catalog = _executor()
    auth = _full_auth_context()
    plan = _overview_plan(catalog)
    store = port.result_store
    assert store is not None
    await _seed_result_store(store, auth_context=auth)
    port.plan_repository.plans[
        (
            auth.principal.tenant_id,
            auth.principal.user_id,
            auth.run_id,
            plan.plan_id,
        )
    ] = plan
    events = InMemoryEventBroker()
    bindings = InMemoryAnalysisRunBindingStore()
    ledger = InMemoryAnalysisStepLedgerStore(
        observation_validator=AgentStoreAnalysisObservationValidator(store)
    )
    execution = AnalysisGraphExecutionService(
        catalog=catalog,
        planner=port.planner,
        plan_repository=port.plan_repository,
        resolver=port.resolver,
        semantic_executor=port,
        result_store=store,
        observation_service=ToolObservationService(
            store=store,
            events=events,
            registry=ToolRegistry.default(),
            evidence_source_system="test-source",
        ),
        binding_store=bindings,
        step_ledger=ledger,
    )
    graph = LangGraphAnalysisOrchestrator(
        execution=execution,
        lifecycle=SessionRunService(store),
    )
    return graph, execution, port, store, events, bindings, plan, auth


@pytest.mark.asyncio
async def test_graph_executes_three_subjects_and_binds_one_report() -> None:
    graph, _execution, port, store, events, bindings, plan, auth = await _runtime()

    outcome = await graph.run(
        user_id=auth.principal.user_id,
        session_id=auth.session_id,
        analysis_run_id=auth.run_id,
        plan_id=plan.plan_id,
        request_id=plan.request_id,
        auth_context=auth,
    )

    assert outcome.status == "completed"
    assert outcome.report_result_id is not None
    report = await store.get_result_for_run(
        user_id=auth.principal.user_id,
        run_id=auth.run_id,
        result_id=outcome.report_result_id,
    )
    assert report.kind == "analysis_report"
    assert len(port.calls) == 3
    assert len(store.evidence) == 3
    run_events = await events.list_events(run_id=auth.run_id)
    assert sum(event.type == "result.available" for event in run_events) == 3
    binding = await bindings.get_binding(
        tenant_id=auth.principal.tenant_id,
        user_id=auth.principal.user_id,
        run_id=auth.run_id,
        invocation_fingerprint=execution_fingerprint(plan, auth),
    )
    assert binding.status == "completed"
    assert binding.report_result_id == outcome.report_result_id


@pytest.mark.asyncio
async def test_graph_terminal_replay_does_not_call_tools_or_duplicate_rows() -> None:
    graph, _execution, port, store, events, _bindings, plan, auth = await _runtime()
    kwargs = {
        "user_id": auth.principal.user_id,
        "session_id": auth.session_id,
        "analysis_run_id": auth.run_id,
        "plan_id": plan.plan_id,
        "request_id": plan.request_id,
        "auth_context": auth,
    }
    first = await graph.run(**kwargs)
    counts = (
        len(port.calls),
        len(store.results),
        len(store.evidence),
        len(await events.list_events(run_id=auth.run_id)),
    )

    replayed = await graph.run(**kwargs)

    assert replayed == first
    assert counts == (
        len(port.calls),
        len(store.results),
        len(store.evidence),
        len(await events.list_events(run_id=auth.run_id)),
    )


@pytest.mark.asyncio
async def test_execution_rejects_an_auth_context_from_another_run() -> None:
    _graph, execution, _port, _store, _events, _bindings, plan, auth = await _runtime()
    with pytest.raises(Exception, match="does not match"):
        await execution.prepare(
            analysis_run_id="another-run",
            plan_id=plan.plan_id,
            request_id=plan.request_id,
            auth_context=auth,
        )


def execution_fingerprint(plan, auth) -> str:
    return AnalysisGraphExecutionService._invocation_fingerprint(plan, auth)
