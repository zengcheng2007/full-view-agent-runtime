"""Orchestrator contract tests – reusable for any OrchestrationPort impl.

These tests exercise the OrchestrationPort contract through a factory
fixture.  R2 can add a LangGraph factory without copying or rewriting
any test body.  The NativeOrchestrator runs as the reference impl.
"""

import asyncio

import pytest

from full_view_agent.application.capability_service import (
    CapabilityService,
    ToolAdapter,
)
from full_view_agent.application.errors import ReauthenticationRequired
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
from full_view_agent.application.ports import OrchestrationPort
from full_view_agent.application.session_run_service import SessionRunService
from full_view_agent.application.tool_registry import ToolRegistry
from full_view_agent.domain.models import (
    AuthContext,
    AuthDataScopes,
    AuthorizedAreaScope,
    DataResult,
)
from full_view_agent.infrastructure.event_broker import InMemoryEventBroker
from full_view_agent.infrastructure.governance_adapter import (
    InMemoryGovernanceAdapter,
)
from full_view_agent.infrastructure.memory_store import InMemoryAgentStore

from .test_policy import population_auth_context
from .test_session_run_service import run_request

# ---------------------------------------------------------------------------
# Orchestrator factory fixture – swap implementation without touching tests
# ---------------------------------------------------------------------------


class _StaticAuth:
    def __init__(self, auth_context: AuthContext) -> None:
        self._auth = auth_context

    async def get(self, *, user_id: str, run_id: str) -> AuthContext:
        del user_id, run_id
        return self._auth


class _FailingAdapter(ToolAdapter):
    async def execute(self, **kw: object) -> DataResult:
        raise RuntimeError("downstream unavailable")


class _ReauthAdapter(ToolAdapter):
    async def execute(self, **kw: object) -> DataResult:
        raise ReauthenticationRequired("credential expired")


class _BudgetExhaustedPlanner:
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
    async def decide(self, state: HarnessState) -> ToolAction | FinishAction:
        del state
        return FinishAction(
            summary="无法通过工具获取数据，当前无法完成该查询。"
        )


class _HallucinatedFinishPlanner:
    """Planner that claims success with fabricated numbers – must be
    rejected by DeterministicCompletionValidator."""

    async def decide(self, state: HarnessState) -> ToolAction | FinishAction:
        del state
        return FinishAction(summary="西湖区独居老人共 9999 人")


class _SingleToolPlanner:
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
    adapter: ToolAdapter | None = None,
    planner: object | None = None,
    harness: AgentHarness | None = None,
    auth_context: AuthContext | None = None,
) -> tuple[NativeOrchestrator, InMemoryAgentStore, InMemoryEventBroker]:
    """Factory: returns (orchestrator, store, events).

    To add a LangGraph implementation, create an identical factory that
    returns a LangGraphOrchestrator instead.  All test bodies below
    remain unchanged.
    """
    store = InMemoryAgentStore()
    events = InMemoryEventBroker()
    auth = auth_context or population_auth_context()
    service = SessionRunService(store)
    registry = ToolRegistry.default()
    capability = CapabilityService(
        registry=registry,
        policy=MinimalPolicyAdapter(),
        adapter=adapter or InMemoryGovernanceAdapter(),
    )

    class _PF:
        def __init__(self, p: object) -> None:
            self._p = p

        def create(self, *, user_id: str, auth_context: AuthContext) -> object:
            del user_id, auth_context
            return self._p

    orch = NativeOrchestrator(
        service=service,
        store=store,
        events=events,
        auth_context_provider=_StaticAuth(auth),
        capability=capability,
        registry=registry,
        planner_factory=_PF(planner or _SingleToolPlanner()),
        harness=harness,
    )
    return orch, store, events


async def _create_run(
    store: InMemoryAgentStore,
    user_id: str = "orch-user",
) -> tuple[str, str]:
    service = SessionRunService(store)
    session = await service.create_session(user_id=user_id, title="c")
    run = await service.create_run(
        user_id=user_id,
        session_id=session.session_id,
        request=run_request(),
    )
    return session.session_id, run.run_id


# ---------------------------------------------------------------------------
# 1. Success
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_contract_success() -> None:
    orch, store, events = _build_orchestrator()
    _, rid = await _create_run(store)
    await orch.execute(user_id="orch-user", run_id=rid)
    t = await store.get_run(user_id="orch-user", run_id=rid)
    assert t.status == "completed" and t.outcome == "success"
    types = [e.type for e in await events.list_events(run_id=rid)]
    for required in ("run.started", "tool.started", "tool.completed",
                     "result.available", "run.completed"):
        assert required in types


# ---------------------------------------------------------------------------
# 2. Denial
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_contract_denial() -> None:
    base = population_auth_context()
    denied = base.model_copy(update={
        "data_scopes": AuthDataScopes(
            areas=[AuthorizedAreaScope(
                area_code="330105", include_descendants=True,
            )],
            datasets=base.data_scopes.datasets,
            field_policy_set=base.data_scopes.field_policy_set,
        ),
    })
    orch, store, events = _build_orchestrator(auth_context=denied)
    _, rid = await _create_run(store)
    await orch.execute(user_id="orch-user", run_id=rid)
    t = await store.get_run(user_id="orch-user", run_id=rid)
    assert t.status == "completed" and t.outcome == "denied"
    types = [e.type for e in await events.list_events(run_id=rid)]
    assert "run.completed" in types


# ---------------------------------------------------------------------------
# 3. Tool failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_contract_tool_failure() -> None:
    orch, store, events = _build_orchestrator(adapter=_FailingAdapter())
    _, rid = await _create_run(store)
    await orch.execute(user_id="orch-user", run_id=rid)
    t = await store.get_run(user_id="orch-user", run_id=rid)
    assert t.status == "failed"
    types = [e.type for e in await events.list_events(run_id=rid)]
    assert "run.failed" in types


# ---------------------------------------------------------------------------
# 4. Waiting for input (reauth)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_contract_waiting_input() -> None:
    orch, store, events = _build_orchestrator(adapter=_ReauthAdapter())
    _, rid = await _create_run(store)
    await orch.execute(user_id="orch-user", run_id=rid)
    t = await store.get_run(user_id="orch-user", run_id=rid)
    assert t.status == "waiting_input"
    types = [e.type for e in await events.list_events(run_id=rid)]
    assert "input.required" in types and "run.waiting" in types


# ---------------------------------------------------------------------------
# 5a. Pre-cancel: terminal run must be no-op
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_contract_pre_cancel_is_noop() -> None:
    orch, store, events = _build_orchestrator()
    _, rid = await _create_run(store)
    # Cancel before execute
    await orch.cancel(user_id="orch-user", run_id=rid)
    # Now execute – must be no-op
    await orch.execute(user_id="orch-user", run_id=rid)
    t = await store.get_run(user_id="orch-user", run_id=rid)
    assert t.status == "cancelled"
    types = [e.type for e in await events.list_events(run_id=rid)]
    assert "run.resumed" not in types
    assert "tool.started" not in types
    assert "tool.completed" not in types
    assert "result.available" not in types


# ---------------------------------------------------------------------------
# 5b. Mid-run cancel
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_contract_mid_run_cancel() -> None:
    orch, store, events = _build_orchestrator()
    _, rid = await _create_run(store)
    # Start execution in background
    task = asyncio.create_task(
        orch.execute(user_id="orch-user", run_id=rid)
    )
    # Cancel immediately
    await orch.cancel(user_id="orch-user", run_id=rid)
    await task
    t = await store.get_run(user_id="orch-user", run_id=rid)
    assert t.status == "cancelled"
    types = [e.type for e in await events.list_events(run_id=rid)]
    assert "run.cancelled" in types
    # After cancel, no complete/result events
    assert "run.completed" not in types


# ---------------------------------------------------------------------------
# 6. Budget exceeded
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_contract_budget_exceeded() -> None:
    harness = AgentHarness(
        tool_executor=CapabilityService(
            registry=ToolRegistry.default(),
            policy=MinimalPolicyAdapter(),
            adapter=InMemoryGovernanceAdapter(),
        ),
        validator=DeterministicCompletionValidator(),
        limits=HarnessLimits(
            max_model_turns=1, max_tool_calls=1,
            max_consecutive_failures=3, max_no_progress=3,
            max_elapsed_seconds=120.0, repeated_call_limit=2,
        ),
    )
    orch, store, events = _build_orchestrator(
        harness=harness, planner=_BudgetExhaustedPlanner(),
    )
    _, rid = await _create_run(store)
    await orch.execute(user_id="orch-user", run_id=rid)
    t = await store.get_run(user_id="orch-user", run_id=rid)
    assert t.status == "failed"
    types = [e.type for e in await events.list_events(run_id=rid)]
    assert "run.failed" in types


# ---------------------------------------------------------------------------
# 7a. Completion validation – valid direct answer (positive)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_contract_completion_valid_direct_answer() -> None:
    orch, store, _ = _build_orchestrator(planner=_FinishOnlyPlanner())
    _, rid = await _create_run(store)
    await orch.execute(user_id="orch-user", run_id=rid)
    t = await store.get_run(user_id="orch-user", run_id=rid)
    assert t.status == "completed" and t.outcome == "success"


# ---------------------------------------------------------------------------
# 7b. Completion validation – hallucinated numbers (negative)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_contract_completion_rejects_hallucinated_numbers() -> None:
    orch, store, _ = _build_orchestrator(
        planner=_HallucinatedFinishPlanner(),
    )
    _, rid = await _create_run(store)
    await orch.execute(user_id="orch-user", run_id=rid)
    t = await store.get_run(user_id="orch-user", run_id=rid)
    # Must NOT be success – validator rejects fabricated numbers
    assert t.outcome != "success"
    assert t.status in ("failed", "completed")


# ---------------------------------------------------------------------------
# 8. Port conformance
# ---------------------------------------------------------------------------


def test_native_implements_port() -> None:
    orch, _, _ = _build_orchestrator()
    assert isinstance(orch, OrchestrationPort)
