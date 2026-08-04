"""Production analysis graph execution through Harness and durable stores."""

import pytest

from full_view_agent.application.analysis_graph import AnalysisStepCheckpoint
from full_view_agent.application.analysis_graph_execution_service import (
    AnalysisGraphExecutionService,
)
from full_view_agent.application.analysis_observation_validator import (
    AgentStoreAnalysisObservationValidator,
)
from full_view_agent.application.errors import ReauthenticationRequired, RunStateConflict
from full_view_agent.application.session_run_service import SessionRunService
from full_view_agent.application.tool_observation_service import ToolObservationService
from full_view_agent.application.tool_registry import ToolRegistry
from full_view_agent.domain.models import ToolResult
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


@pytest.mark.asyncio
async def test_finalize_rejects_checkpoints_without_step_attestations() -> None:
    _graph, execution, _port, _store, _events, _bindings, plan, auth = await _runtime()
    await execution.prepare(
        analysis_run_id=auth.run_id,
        plan_id=plan.plan_id,
        request_id=plan.request_id,
        auth_context=auth,
    )
    forged = tuple(
        AnalysisStepCheckpoint(
            step_id=step.step_id,
            status="timeout",
            reason_code="ANALYSIS_DEADLINE_EXCEEDED",
            tool_call_consumed=False,
        )
        for step in plan.steps
    )

    with pytest.raises(RunStateConflict, match="no durable step attestation"):
        await execution.finalize(
            analysis_run_id=auth.run_id,
            plan_id=plan.plan_id,
            request_id=plan.request_id,
            completed=forged,
            auth_context=auth,
        )


@pytest.mark.asyncio
async def test_finalize_rejects_forged_persisted_reason_code() -> None:
    _graph, execution, _port, _store, _events, _bindings, plan, auth = await _runtime()
    await execution.prepare(
        analysis_run_id=auth.run_id,
        plan_id=plan.plan_id,
        request_id=plan.request_id,
        auth_context=auth,
    )
    completed = [
        await execution.execute_step(
            analysis_run_id=auth.run_id,
            plan_id=plan.plan_id,
            request_id=plan.request_id,
            step_id=step.step_id,
            auth_context=auth,
        )
        for step in plan.steps
    ]
    completed[0] = completed[0].model_copy(update={"reason_code": "FORGED_REASON"})

    with pytest.raises(RunStateConflict, match="durable step attestation"):
        await execution.finalize(
            analysis_run_id=auth.run_id,
            plan_id=plan.plan_id,
            request_id=plan.request_id,
            completed=tuple(completed),
            auth_context=auth,
        )


@pytest.mark.asyncio
async def test_reduce_persists_timeout_attestations_before_finalize() -> None:
    _graph, execution, _port, _store, _events, _bindings, plan, auth = await _runtime()
    await execution.prepare(
        analysis_run_id=auth.run_id,
        plan_id=plan.plan_id,
        request_id=plan.request_id,
        auth_context=auth,
    )
    reduction = await execution.reduce(
        analysis_run_id=auth.run_id,
        plan_id=plan.plan_id,
        request_id=plan.request_id,
        completed=(),
        tool_call_count=0,
        deadline_exceeded=True,
        auth_context=auth,
    )

    outcome = await execution.finalize(
        analysis_run_id=auth.run_id,
        plan_id=plan.plan_id,
        request_id=plan.request_id,
        completed=reduction.additions,
        auth_context=auth,
    )

    assert outcome.status == "failed"
    assert all(item.status == "timeout" for item in reduction.additions)


@pytest.mark.asyncio
async def test_partial_step_replay_preserves_status_and_reason(monkeypatch) -> None:
    _graph, execution, port, _store, _events, _bindings, plan, auth = await _runtime()
    await execution.prepare(
        analysis_run_id=auth.run_id,
        plan_id=plan.plan_id,
        request_id=plan.request_id,
        auth_context=auth,
    )
    original_execute = port.execute

    async def partial_execute(**values):
        result = await original_execute(**values)
        return result.model_copy(
            update={"status": "partial", "warnings": ["SOURCE_PARTIAL"]}
        )

    monkeypatch.setattr(port, "execute", partial_execute)
    step = plan.steps[0]
    kwargs = {
        "analysis_run_id": auth.run_id,
        "plan_id": plan.plan_id,
        "request_id": plan.request_id,
        "step_id": step.step_id,
        "auth_context": auth,
    }
    first = await execution.execute_step(**kwargs)
    replayed = await execution.execute_step(**kwargs)

    assert first.status == replayed.status == "partial"
    assert first.reason_code == replayed.reason_code == "SOURCE_PARTIAL"
    assert first.result_id == replayed.result_id


@pytest.mark.asyncio
async def test_reauthentication_can_resume_without_indeterminate(monkeypatch) -> None:
    _graph, execution, port, _store, _events, _bindings, plan, auth = await _runtime()
    await execution.prepare(
        analysis_run_id=auth.run_id,
        plan_id=plan.plan_id,
        request_id=plan.request_id,
        auth_context=auth,
    )
    original_execute = port.execute
    attempts = 0

    async def reauth_once(**values):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ReauthenticationRequired("credential expired")
        return await original_execute(**values)

    monkeypatch.setattr(port, "execute", reauth_once)
    step = plan.steps[0]
    kwargs = {
        "analysis_run_id": auth.run_id,
        "plan_id": plan.plan_id,
        "request_id": plan.request_id,
        "step_id": step.step_id,
        "auth_context": auth,
    }
    with pytest.raises(ReauthenticationRequired):
        await execution.execute_step(**kwargs)

    resumed = await execution.execute_step(**kwargs)

    assert resumed.status == "success"
    assert attempts == 2


@pytest.mark.asyncio
async def test_denied_step_replay_preserves_denial(monkeypatch) -> None:
    _graph, execution, port, _store, _events, _bindings, plan, auth = await _runtime()
    await execution.prepare(
        analysis_run_id=auth.run_id,
        plan_id=plan.plan_id,
        request_id=plan.request_id,
        auth_context=auth,
    )

    async def denied_execute(**values):
        return ToolResult(
            tool_call_id=values["tool_call_id"],
            tool_id=values["tool_id"],
            tool_version="1.0",
            status="denied",
            summary="denied",
            warnings=["POLICY_DENIED"],
        )

    monkeypatch.setattr(port, "execute", denied_execute)
    step = plan.steps[0]
    kwargs = {
        "analysis_run_id": auth.run_id,
        "plan_id": plan.plan_id,
        "request_id": plan.request_id,
        "step_id": step.step_id,
        "auth_context": auth,
    }
    first = await execution.execute_step(**kwargs)
    replayed = await execution.execute_step(**kwargs)

    assert first.status == replayed.status == "denied"
    assert first.reason_code == replayed.reason_code == "POLICY_DENIED"


def execution_fingerprint(plan, auth) -> str:
    return AnalysisGraphExecutionService._invocation_fingerprint(plan, auth)
