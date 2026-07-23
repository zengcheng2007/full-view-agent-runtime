"""Orchestrator contract tests.

These tests verify the OrchestrationPort contract: any implementation
(Native, LangGraph, etc.) must satisfy the same external behaviour.
They exercise the port through the NativeOrchestrator as the reference
implementation, and are designed to be reusable for LangGraphOrchestrator
without modification.
"""


import pytest

from full_view_agent.application.capability_service import (
    CapabilityService,
    ToolAdapter,
)
from full_view_agent.application.errors import (
    ReauthenticationRequired,
)
from full_view_agent.application.harness import (
    AgentHarness,
    DeterministicCompletionValidator,
    FinishAction,
    HarnessLimits,
    HarnessState,
    ToolAction,
)
from full_view_agent.application.native_orchestrator import NativeOrchestrator
from full_view_agent.application.policy import MinimalPolicyAdapter
from full_view_agent.application.ports import (
    OrchestrationPort,
)
from full_view_agent.application.session_run_service import (
    SessionRunService,
)
from full_view_agent.application.tool_registry import ToolRegistry
from full_view_agent.domain.models import (
    AuthContext,
    DataResult,
    PolicyDecision,
)
from full_view_agent.infrastructure.event_broker import InMemoryEventBroker
from full_view_agent.infrastructure.governance_adapter import (
    InMemoryGovernanceAdapter,
)
from full_view_agent.infrastructure.memory_store import InMemoryAgentStore

from .test_policy import population_auth_context
from .test_session_run_service import run_request

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class _StaticAuth:
    def __init__(self, auth_context: AuthContext) -> None:
        self._auth = auth_context

    async def get(self, *, user_id: str, run_id: str) -> AuthContext:
        del user_id, run_id
        return self._auth


class _AlwaysDenyAdapter(ToolAdapter):
    """Adapter that denies every tool call."""

    async def execute(
        self,
        *,
        manifest: object,
        arguments: object,
        policy_decision: PolicyDecision,
        auth_context: AuthContext,
    ) -> DataResult:
        del manifest, arguments, policy_decision, auth_context
        raise AssertionError("should not be called – policy denies first")


class _FailingAdapter(ToolAdapter):
    """Adapter that returns a failed tool result."""

    async def execute(
        self,
        *,
        manifest: object,
        arguments: object,
        policy_decision: PolicyDecision,
        auth_context: AuthContext,
    ) -> DataResult:
        del manifest, arguments, policy_decision, auth_context
        raise RuntimeError("downstream service unavailable")


class _ReauthAdapter(ToolAdapter):
    """Adapter that raises ReauthenticationRequired."""

    async def execute(
        self,
        *,
        manifest: object,
        arguments: object,
        policy_decision: PolicyDecision,
        auth_context: AuthContext,
    ) -> DataResult:
        del manifest, arguments, policy_decision, auth_context
        raise ReauthenticationRequired("credential expired")


class _BudgetExhaustedPlanner:
    """Planner that always requests a tool call, exhausting budgets."""

    async def decide(self, state: HarnessState) -> ToolAction | FinishAction:
        return ToolAction(
            tool_id="governance.query_population_metrics",
            arguments={
                "query": {
                    "metrics": ["person_count"],
                    "scope": {"area_code": "330106"},
                    "filters": [],
                    "group_by": ["street"],
                }
            },
        )


class _FinishOnlyPlanner:
    """Planner that finishes immediately with a valid direct answer."""

    async def decide(self, state: HarnessState) -> ToolAction | FinishAction:
        del state
        return FinishAction(
            summary="无法通过工具获取数据，当前无法完成该查询。"
        )


class _SingleToolPlanner:
    """Planner that calls one tool then finishes."""

    async def decide(self, state: HarnessState) -> ToolAction | FinishAction:
        if state.tool_results:
            return FinishAction(summary="查询完成")
        return ToolAction(
            tool_id="governance.query_population_metrics",
            arguments={
                "query": {
                    "metrics": ["person_count"],
                    "scope": {"area_code": "330106"},
                    "filters": [
                        {
                            "field": "person_category",
                            "operator": "eq",
                            "value": "solitary_elderly",
                        }
                    ],
                    "group_by": ["street"],
                }
            },
        )


def _build_orchestrator(
    *,
    store: InMemoryAgentStore | None = None,
    events: InMemoryEventBroker | None = None,
    adapter: ToolAdapter | None = None,
    planner: object | None = None,
    harness: AgentHarness | None = None,
    auth_context: AuthContext | None = None,
) -> tuple[NativeOrchestrator, InMemoryAgentStore, InMemoryEventBroker]:
    store = store or InMemoryAgentStore()
    events = events or InMemoryEventBroker()
    auth = auth_context or population_auth_context()
    service = SessionRunService(store)
    registry = ToolRegistry.default()
    capability = CapabilityService(
        registry=registry,
        policy=MinimalPolicyAdapter(),
        adapter=adapter or InMemoryGovernanceAdapter(),
    )

    class _PlannerFactory:
        def __init__(self, p: object) -> None:
            self._p = p

        def create(self, *, user_id: str, auth_context: AuthContext) -> object:
            del user_id, auth_context
            return self._p

    orchestrator = NativeOrchestrator(
        service=service,
        store=store,
        events=events,
        auth_context_provider=_StaticAuth(auth),
        capability=capability,
        registry=registry,
        planner_factory=_PlannerFactory(planner or _SingleToolPlanner()),
        harness=harness,
    )
    return orchestrator, store, events


async def _create_run(
    store: InMemoryAgentStore,
    user_id: str = "orch-contract-user",
) -> tuple[str, str]:
    """Create a session and run, return (session_id, run_id)."""
    service = SessionRunService(store)
    session = await service.create_session(user_id=user_id, title="contract")
    run = await service.create_run(
        user_id=user_id,
        session_id=session.session_id,
        request=run_request(),
    )
    return session.session_id, run.run_id


# ---------------------------------------------------------------------------
# Contract: success
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_contract_success_emits_run_completed_and_result() -> None:
    orch, store, events = _build_orchestrator()
    _sid, run_id = await _create_run(store)

    await orch.execute(user_id="orch-contract-user", run_id=run_id)

    terminal = await store.get_run(
        user_id="orch-contract-user", run_id=run_id
    )
    assert terminal.status == "completed"
    assert terminal.outcome == "success"

    published = await events.list_events(run_id=run_id)
    types = [e.type for e in published]
    assert "run.started" in types
    assert "tool.started" in types
    assert "tool.completed" in types
    assert "result.available" in types
    assert "run.completed" in types


# ---------------------------------------------------------------------------
# Contract: denial
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_contract_denial_emits_run_completed_with_denied_outcome() -> None:
    base = population_auth_context()
    from full_view_agent.domain.models import (
        AuthDataScopes,
        AuthorizedAreaScope,
    )

    denied_auth = base.model_copy(
        update={
            "data_scopes": AuthDataScopes(
                areas=[
                    AuthorizedAreaScope(
                        area_code="330105", include_descendants=True
                    )
                ],
                datasets=base.data_scopes.datasets,
                field_policy_set=base.data_scopes.field_policy_set,
            )
        }
    )
    orch, store, events = _build_orchestrator(
        auth_context=denied_auth,
    )
    _sid, run_id = await _create_run(store)

    await orch.execute(user_id="orch-contract-user", run_id=run_id)

    terminal = await store.get_run(
        user_id="orch-contract-user", run_id=run_id
    )
    assert terminal.status == "completed"
    assert terminal.outcome == "denied"

    published = await events.list_events(run_id=run_id)
    types = [e.type for e in published]
    assert "run.completed" in types
    assert "tool.completed" in types


# ---------------------------------------------------------------------------
# Contract: tool failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_contract_tool_failure_emits_run_failed() -> None:
    orch, store, events = _build_orchestrator(
        adapter=_FailingAdapter(),
    )
    _sid, run_id = await _create_run(store)

    await orch.execute(user_id="orch-contract-user", run_id=run_id)

    terminal = await store.get_run(
        user_id="orch-contract-user", run_id=run_id
    )
    assert terminal.status == "failed"

    published = await events.list_events(run_id=run_id)
    types = [e.type for e in published]
    assert "run.failed" in types


# ---------------------------------------------------------------------------
# Contract: waiting for input (reauthentication)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_contract_reauth_emits_input_required() -> None:
    orch, store, events = _build_orchestrator(
        adapter=_ReauthAdapter(),
    )
    _sid, run_id = await _create_run(store)

    await orch.execute(user_id="orch-contract-user", run_id=run_id)

    terminal = await store.get_run(
        user_id="orch-contract-user", run_id=run_id
    )
    assert terminal.status == "waiting_input"

    published = await events.list_events(run_id=run_id)
    types = [e.type for e in published]
    assert "input.required" in types
    assert "run.waiting" in types


# ---------------------------------------------------------------------------
# Contract: cancellation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_contract_cancel_before_execution() -> None:
    orch, store, events = _build_orchestrator()
    _sid, run_id = await _create_run(store)

    # Cancel before orchestrator runs
    service = SessionRunService(store)
    await service.cancel_run(user_id="orch-contract-user", run_id=run_id)

    await orch.execute(user_id="orch-contract-user", run_id=run_id)

    terminal = await store.get_run(
        user_id="orch-contract-user", run_id=run_id
    )
    assert terminal.status in {"cancelled", "completed", "failed"}


# ---------------------------------------------------------------------------
# Contract: budget exceeded
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_contract_budget_exceeded_emits_run_failed() -> None:
    harness = AgentHarness(
        tool_executor=CapabilityService(
            registry=ToolRegistry.default(),
            policy=MinimalPolicyAdapter(),
            adapter=InMemoryGovernanceAdapter(),
        ),
        validator=DeterministicCompletionValidator(),
        limits=HarnessLimits(
            max_model_turns=1,
            max_tool_calls=1,
            max_consecutive_failures=3,
            max_no_progress=3,
            max_elapsed_seconds=120.0,
            repeated_call_limit=2,
        ),
    )
    orch, store, events = _build_orchestrator(
        harness=harness,
        planner=_BudgetExhaustedPlanner(),
    )
    _sid, run_id = await _create_run(store)

    await orch.execute(user_id="orch-contract-user", run_id=run_id)

    terminal = await store.get_run(
        user_id="orch-contract-user", run_id=run_id
    )
    assert terminal.status == "failed"

    published = await events.list_events(run_id=run_id)
    types = [e.type for e in published]
    assert "run.failed" in types


# ---------------------------------------------------------------------------
# Contract: completion validation (direct answer)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_contract_direct_answer_validates_completion() -> None:
    orch, store, events = _build_orchestrator(
        planner=_FinishOnlyPlanner(),
    )
    _sid, run_id = await _create_run(store)

    await orch.execute(user_id="orch-contract-user", run_id=run_id)

    terminal = await store.get_run(
        user_id="orch-contract-user", run_id=run_id
    )
    assert terminal.status == "completed"
    assert terminal.outcome == "success"

    published = await events.list_events(run_id=run_id)
    types = [e.type for e in published]
    assert "run.completed" in types
    assert "tool.started" not in types


# ---------------------------------------------------------------------------
# Contract: OrchestrationPort conformance
# ---------------------------------------------------------------------------


def test_native_orchestrator_implements_port() -> None:
    orch, _store, _events = _build_orchestrator()
    assert isinstance(orch, OrchestrationPort)
