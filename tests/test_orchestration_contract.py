"""Orchestrator contract tests – parameterised for any OrchestrationPort.

All test bodies receive an ``OrchestrationPort`` via the ``orch_factory``
fixture.  R2 adds a LangGraph factory through ``conftest.py``
parametrization without touching any test body.
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
# Helpers
# ---------------------------------------------------------------------------


class _StaticAuth:
    def __init__(self, ctx: AuthContext) -> None:
        self._ctx = ctx

    async def get(self, *, user_id: str, run_id: str) -> AuthContext:
        del user_id, run_id
        return self._ctx


class _FailingAdapter(ToolAdapter):
    async def execute(self, **kw: object) -> DataResult:
        raise RuntimeError("downstream unavailable")


class _ReauthAdapter(ToolAdapter):
    async def execute(self, **kw: object) -> DataResult:
        raise ReauthenticationRequired("credential expired")


class _BlockingAdapter(ToolAdapter):
    """Adapter that blocks until an external event is set, allowing
    deterministic mid-run cancellation."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.proceed = asyncio.Event()

    async def execute(self, **kw: object) -> DataResult:
        self.entered.set()
        await self.proceed.wait()
        raise RuntimeError("should not reach here after cancel")


class _BudgetPlanner:
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
            summary="无法通过工具获取数据，当前无法完成该查询。",
        )


class _HallucinatedPlanner:
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


# ---------------------------------------------------------------------------
# Factory fixture – swap impl via conftest parametrization in R2
# ---------------------------------------------------------------------------

OrchFactory = "type"  # alias for readability


def _native_factory(
    *,
    adapter: ToolAdapter | None = None,
    planner: object | None = None,
    harness: AgentHarness | None = None,
    auth_context: AuthContext | None = None,
) -> tuple[OrchestrationPort, InMemoryAgentStore, InMemoryEventBroker]:
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

    orch: OrchestrationPort = NativeOrchestrator(
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


@pytest.fixture()
def orch_factory() -> type[_native_factory]:
    """Yield the orchestrator factory.  R2: parametrize with LangGraph."""
    return _native_factory


async def _make_run(store: InMemoryAgentStore) -> str:
    svc = SessionRunService(store)
    s = await svc.create_session(user_id="u", title="c")
    r = await svc.create_run(
        user_id="u", session_id=s.session_id, request=run_request(),
    )
    return r.run_id


# ---------------------------------------------------------------------------
# 1  Success
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_success(orch_factory: OrchFactory) -> None:
    orch, store, events = orch_factory()
    rid = await _make_run(store)
    await orch.execute(user_id="u", run_id=rid)
    t = await store.get_run(user_id="u", run_id=rid)
    assert t.status == "completed" and t.outcome == "success"
    types = {e.type for e in await events.list_events(run_id=rid)}
    assert {"run.started", "tool.started", "tool.completed",
            "result.available", "run.completed"} <= types


# ---------------------------------------------------------------------------
# 2  Denial
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_denial(orch_factory: OrchFactory) -> None:
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
    orch, store, events = orch_factory(auth_context=denied)
    rid = await _make_run(store)
    await orch.execute(user_id="u", run_id=rid)
    t = await store.get_run(user_id="u", run_id=rid)
    assert t.status == "completed" and t.outcome == "denied"


# ---------------------------------------------------------------------------
# 3  Tool failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tool_failure(orch_factory: OrchFactory) -> None:
    orch, store, events = orch_factory(adapter=_FailingAdapter())
    rid = await _make_run(store)
    await orch.execute(user_id="u", run_id=rid)
    t = await store.get_run(user_id="u", run_id=rid)
    assert t.status == "failed"
    assert "run.failed" in {e.type for e in await events.list_events(run_id=rid)}


# ---------------------------------------------------------------------------
# 4  Waiting input
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_waiting_input(orch_factory: OrchFactory) -> None:
    orch, store, events = orch_factory(adapter=_ReauthAdapter())
    rid = await _make_run(store)
    await orch.execute(user_id="u", run_id=rid)
    t = await store.get_run(user_id="u", run_id=rid)
    assert t.status == "waiting_input"
    types = {e.type for e in await events.list_events(run_id=rid)}
    assert {"input.required", "run.waiting"} <= types


# ---------------------------------------------------------------------------
# 5a  Pre-cancel → execute is no-op
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pre_cancel_noop(orch_factory: OrchFactory) -> None:
    orch, store, events = orch_factory()
    rid = await _make_run(store)
    await orch.cancel(user_id="u", run_id=rid)
    await orch.execute(user_id="u", run_id=rid)
    t = await store.get_run(user_id="u", run_id=rid)
    assert t.status == "cancelled"
    types = {e.type for e in await events.list_events(run_id=rid)}
    assert "tool.started" not in types
    assert "run.resumed" not in types
    assert "result.available" not in types


# ---------------------------------------------------------------------------
# 5b  Mid-run cancel with blocking adapter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mid_run_cancel(orch_factory: OrchFactory) -> None:
    blocking = _BlockingAdapter()
    orch, store, events = orch_factory(adapter=blocking)
    rid = await _make_run(store)

    # Start execute in background
    task = asyncio.create_task(orch.execute(user_id="u", run_id=rid))
    # Wait until the tool has actually entered execute()
    await asyncio.wait_for(blocking.entered.wait(), timeout=2)
    # Verify run.started was published
    started = [e for e in await events.list_events(run_id=rid)
               if e.type == "run.started"]
    assert len(started) == 1

    # Now cancel while tool is blocked
    await orch.cancel(user_id="u", run_id=rid)
    # Unblock the adapter so the task can finish
    blocking.proceed.set()
    await asyncio.wait_for(task, timeout=2)

    t = await store.get_run(user_id="u", run_id=rid)
    assert t.status == "cancelled"
    types = [e.type for e in await events.list_events(run_id=rid)]
    assert "run.cancelled" in types
    assert "run.completed" not in types
    assert "result.available" not in types
    # No extra tool.completed after cancel
    tool_completed = [e for e in types if e == "tool.completed"]
    assert len(tool_completed) == 0


# ---------------------------------------------------------------------------
# 6  Budget exceeded
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_budget_exceeded(orch_factory: OrchFactory) -> None:
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
    orch, store, events = orch_factory(
        harness=harness, planner=_BudgetPlanner(),
    )
    rid = await _make_run(store)
    await orch.execute(user_id="u", run_id=rid)
    t = await store.get_run(user_id="u", run_id=rid)
    assert t.status == "failed"
    assert "run.failed" in {e.type for e in await events.list_events(run_id=rid)}


# ---------------------------------------------------------------------------
# 7a  Completion validation – valid direct answer
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_completion_valid(orch_factory: OrchFactory) -> None:
    orch, store, _ = orch_factory(planner=_FinishOnlyPlanner())
    rid = await _make_run(store)
    await orch.execute(user_id="u", run_id=rid)
    t = await store.get_run(user_id="u", run_id=rid)
    assert t.status == "completed" and t.outcome == "success"


# ---------------------------------------------------------------------------
# 7b  Completion validation – hallucinated numbers MUST fail
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_completion_rejects_hallucination(
    orch_factory: OrchFactory,
) -> None:
    orch, store, events = orch_factory(planner=_HallucinatedPlanner())
    rid = await _make_run(store)
    await orch.execute(user_id="u", run_id=rid)
    t = await store.get_run(user_id="u", run_id=rid)
    # Validator rejects → must be failed, not completed
    assert t.status == "failed"
    assert t.outcome == "failed"
    types = {e.type for e in await events.list_events(run_id=rid)}
    assert "run.completed" not in types
    assert "result.available" not in types


# ---------------------------------------------------------------------------
# 8  Port conformance
# ---------------------------------------------------------------------------


def test_port_conformance(orch_factory: OrchFactory) -> None:
    orch, _, _ = orch_factory()
    assert isinstance(orch, OrchestrationPort)
