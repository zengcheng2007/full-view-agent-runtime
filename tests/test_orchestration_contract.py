"""Orchestrator contract tests – parameterised for any OrchestrationPort.

All test bodies receive an ``OrchestrationPort`` via the ``orch_factory``
fixture.  R2 adds a LangGraph factory through ``conftest.py``
parametrization without touching any test body.
"""

from __future__ import annotations

import asyncio
from typing import Protocol

import pytest

from full_view_agent.application.answer_claims import (
    FINISH_TOOL_ID,
    AnswerClaim,
    StructuredFinish,
)
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
from full_view_agent.application.model_planner import ModelPlanner
from full_view_agent.application.model_provider import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ModelToolCall,
    ModelToolDefinition,
)
from full_view_agent.application.native_orchestrator import (
    NativeOrchestrator,
)
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
from full_view_agent.infrastructure.langgraph_orchestrator import (
    LangGraphOrchestrator,
)
from full_view_agent.infrastructure.memory_store import InMemoryAgentStore

from .test_policy import population_auth_context
from .test_session_run_service import run_request

# ---------------------------------------------------------------------------
# Real type alias for the orchestrator factory
# ---------------------------------------------------------------------------

OrchFactoryResult = tuple[OrchestrationPort, InMemoryAgentStore, InMemoryEventBroker]


class OrchFactory(Protocol):
    """Callable signature every orchestrator factory must satisfy."""

    def __call__(
        self,
        *,
        adapter: ToolAdapter | None = ...,
        planner: object | None = ...,
        harness: AgentHarness | None = ...,
        auth_context: AuthContext | None = ...,
    ) -> OrchFactoryResult: ...

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
    """Adapter that blocks until cancelled, allowing deterministic
    mid-run cancellation via the port's schedule/cancel path."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def execute(self, **kw: object) -> DataResult:
        self.entered.set()
        try:
            await asyncio.Event().wait()  # block forever
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        raise RuntimeError("unreachable")  # pragma: no cover


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


class _StructuredSingleToolPlanner(_SingleToolPlanner):
    async def decide(self, state: HarnessState) -> ToolAction | FinishAction:
        if not state.tool_results:
            return await super().decide(state)
        data_result = state.tool_results[-1].data_result
        assert data_result is not None
        row = data_result.data.rows[0]  # type: ignore[union-attr]
        finish = StructuredFinish(
            kind="claims",
            summary="模型自由正文不会作为最终事实文本。",
            claims=[
                AnswerClaim(
                    claim_id="claim-1",
                    result_id=data_result.result_id,
                    result_fingerprint=data_result.result_fingerprint,
                    collection="rows",
                    row_locator={"area_code": row.area_code},
                    field="person_count",
                    operation="value",
                    value=row.person_count,
                )
            ],
        )
        return FinishAction(
            summary=finish.summary,
            structured_finish=finish,
            legacy=False,
        )


class _GroundedFollowupPlanner(_SingleToolPlanner):
    async def decide(self, state: HarnessState) -> ToolAction | FinishAction:
        if state.inherited_result_ids:
            finish = StructuredFinish(
                kind="reference_only",
                summary="模型不能复述尚未 hydration 的历史数字。",
                claims=[],
            )
            return FinishAction(
                summary=finish.summary,
                structured_finish=finish,
                legacy=False,
            )
        return await super().decide(state)


class _UnsafeInheritedFollowupPlanner(_SingleToolPlanner):
    async def decide(self, state: HarnessState) -> ToolAction | FinishAction:
        if state.inherited_result_ids:
            return FinishAction(
                summary="北山街道有9999人。",
                legacy=False,
            )
        return await super().decide(state)


class _MalformedFinishContextBuilder:
    def __init__(self) -> None:
        self.feedback_codes: list[str | None] = []

    async def build(self, *, state: HarnessState, **_kwargs: object) -> ModelRequest:
        self.feedback_codes.append(state.completion_feedback_code)
        return ModelRequest(
            messages=(ModelMessage(role="user", content="查询人口指标"),),
            tools=(
                ModelToolDefinition(
                    tool_id="governance.query_population_metrics",
                    description="查询人口指标",
                    input_schema={"type": "object"},
                ),
            ),
        )


class _MalformedFinishProvider:
    def __init__(self) -> None:
        query = ModelResponse(
            content=None,
            tool_calls=(
                ModelToolCall(
                    tool_id="governance.query_population_metrics",
                    arguments={
                        "query": {
                            "metrics": ["person_count"],
                            "scope": {"area_code": "330106"},
                            "filters": [],
                            "group_by": ["street"],
                        }
                    },
                ),
            ),
            finish_reason="tool_calls",
        )
        invalid_claims = {
            "kind": "claims",
            "summary": "伪造人口为9999人。",
            "claims": [
                {
                    "claim_id": "claim-1",
                    "collection": "rows",
                    "row_locator": {},
                    "field": "person_count",
                    "operation": "not-an-operation",
                    "value": 9999,
                }
            ],
        }
        malformed = ModelResponse(
            content=None,
            tool_calls=(
                ModelToolCall(tool_id=FINISH_TOOL_ID, arguments=invalid_claims),
            ),
            finish_reason="tool_calls",
        )
        self.responses = [query, malformed, malformed]

    async def complete(self, _request: ModelRequest) -> ModelResponse:
        return self.responses.pop(0)


# ---------------------------------------------------------------------------
# Factory fixture – swap impl via conftest parametrization in R2
# ---------------------------------------------------------------------------


def _orchestrator_factory(
    *,
    orchestrator_type: type[NativeOrchestrator] = NativeOrchestrator,
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

        def create(
            self, *, user_id: str, auth_context: AuthContext,
        ) -> object:
            del user_id, auth_context
            return self._p

    orch: OrchestrationPort = orchestrator_type(
        service=service,
        store=store,
        events=events,
        auth_context_provider=_StaticAuth(auth),
        capability=capability,
        registry=registry,
        planner_factory=_PF(planner or _SingleToolPlanner()),  # type: ignore[arg-type]
        harness=harness,
    )
    return orch, store, events


def _native_factory(**kwargs: object) -> OrchFactoryResult:
    return _orchestrator_factory(**kwargs)


def _langgraph_factory(**kwargs: object) -> OrchFactoryResult:
    return _orchestrator_factory(
        orchestrator_type=LangGraphOrchestrator,
        **kwargs,
    )


@pytest.mark.asyncio
async def test_langgraph_drives_harness_one_step_at_a_time() -> None:
    class RecordingHarness(AgentHarness):
        def __init__(self) -> None:
            super().__init__(
                tool_executor=CapabilityService(
                    registry=ToolRegistry.default(),
                    policy=MinimalPolicyAdapter(),
                    adapter=InMemoryGovernanceAdapter(),
                ),
                validator=DeterministicCompletionValidator(),
            )
            self.plan_calls = 0
            self.execute_calls = 0
            self.observe_calls = 0
            self.validate_calls = 0

        async def plan_action_once(self, **kwargs):
            self.plan_calls += 1
            return await super().plan_action_once(**kwargs)

        async def authorize_and_execute_once(self, **kwargs):
            self.execute_calls += 1
            return await super().authorize_and_execute_once(**kwargs)

        def observe_once(self, **kwargs):
            self.observe_calls += 1
            return super().observe_once(**kwargs)

        async def validate_once(self, **kwargs):
            self.validate_calls += 1
            return await super().validate_once(**kwargs)

    harness = RecordingHarness()
    orch, store, _events = _langgraph_factory(harness=harness)
    run_id = await _make_run(store)

    await orch.execute(user_id="u", run_id=run_id)

    assert harness.plan_calls == 2
    assert harness.execute_calls == 1
    assert harness.observe_calls == 1
    assert harness.validate_calls == 2
    run = await store.get_run(user_id="u", run_id=run_id)
    assert run.status == "completed"


@pytest.fixture(params=[_native_factory, _langgraph_factory], ids=["native", "langgraph"])
def orch_factory(request: pytest.FixtureRequest) -> OrchFactory:
    """Every OrchestrationPort implementation must meet the same contract."""
    return request.param


async def _make_run(store: InMemoryAgentStore) -> str:
    svc = SessionRunService(store)
    s = await svc.create_session(user_id="u", title="c")
    r = await svc.create_run(
        user_id="u", session_id=s.session_id, request=run_request(),
    )
    return r.run_id


@pytest.mark.asyncio
async def test_followup_can_reuse_verified_session_result_without_loop(
    orch_factory: OrchFactory,
) -> None:
    orch, store, events = orch_factory(planner=_GroundedFollowupPlanner())
    service = SessionRunService(store)
    session = await service.create_session(user_id="u", title="多轮结果复用")
    first = await service.create_run(
        user_id="u",
        session_id=session.session_id,
        request=run_request("web-msg-grounded-first"),
    )
    await orch.execute(user_id="u", run_id=first.run_id)
    first_run = await store.get_run(user_id="u", run_id=first.run_id)
    assert first_run.status == "completed"

    second = await service.create_run(
        user_id="u",
        session_id=session.session_id,
        request=run_request("web-msg-grounded-followup"),
    )
    await orch.execute(user_id="u", run_id=second.run_id)

    second_run = await store.get_run(user_id="u", run_id=second.run_id)
    assert second_run.status == "completed"
    assert second_run.outcome == "success"
    second_events = await events.list_events(run_id=second.run_id)
    assert "tool.started" not in [event.type for event in second_events]
    messages = await store.list_messages(user_id="u", session_id=session.session_id)
    followup_answer = next(
        message
        for message in messages
        if message.role == "assistant" and message.run_id == second.run_id
    )
    assert followup_answer.evidence_ids
    assert any(item.type == "result_reference" for item in followup_answer.content)


@pytest.mark.asyncio
async def test_unstructured_inherited_followup_revises_once_then_stops_safely(
    orch_factory: OrchFactory,
) -> None:
    orch, store, events = orch_factory(planner=_UnsafeInheritedFollowupPlanner())
    service = SessionRunService(store)
    session = await service.create_session(user_id="u", title="历史结果安全停止")
    first = await service.create_run(
        user_id="u",
        session_id=session.session_id,
        request=run_request("web-msg-unsafe-first"),
    )
    await orch.execute(user_id="u", run_id=first.run_id)
    second = await service.create_run(
        user_id="u",
        session_id=session.session_id,
        request=run_request("web-msg-unsafe-followup"),
    )

    await orch.execute(user_id="u", run_id=second.run_id)

    run = await store.get_run(user_id="u", run_id=second.run_id)
    assert run.status == "completed"
    messages = await store.list_messages(user_id="u", session_id=session.session_id)
    answer = next(
        message
        for message in messages
        if message.role == "assistant" and message.run_id == second.run_id
    )
    assert answer.content[0].text == (
        "抱歉，当前回答仍包含无法由查询结果核验的内容，已停止生成结论。"
    )
    types = [event.type for event in await events.list_events(run_id=second.run_id)]
    assert "run.failed" not in types


@pytest.mark.asyncio
async def test_malformed_structured_finish_revises_then_stops_across_orchestrators(
    orch_factory: OrchFactory,
) -> None:
    context_builder = _MalformedFinishContextBuilder()
    planner = ModelPlanner(
        provider=_MalformedFinishProvider(),
        context_builder=context_builder,
        user_id="u",
        auth_context=population_auth_context(),
    )
    orch, store, events = orch_factory(planner=planner)
    run_id = await _make_run(store)

    await orch.execute(user_id="u", run_id=run_id)

    run = await store.get_run(user_id="u", run_id=run_id)
    assert run.status == "completed"
    messages = await store.list_messages(user_id="u", session_id=run.session_id)
    answer = next(message for message in messages if message.role == "assistant")
    assert answer.content[0].text == (
        "抱歉，当前回答仍包含无法由查询结果核验的内容，已停止生成结论。"
    )
    assert context_builder.feedback_codes == [None, None, "invalid_structured_finish"]
    types = [event.type for event in await events.list_events(run_id=run_id)]
    assert "run.failed" not in types


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


@pytest.mark.asyncio
async def test_structured_finish_is_identical_across_orchestrators(
    orch_factory: OrchFactory,
) -> None:
    orch, store, _events = orch_factory(planner=_StructuredSingleToolPlanner())
    rid = await _make_run(store)

    await orch.execute(user_id="u", run_id=rid)

    run = await store.get_run(user_id="u", run_id=rid)
    messages = await store.list_messages(user_id="u", session_id=run.session_id)
    answer = next(message for message in messages if message.role == "assistant")
    assert answer.content[0].type == "text"
    assert answer.content[0].text == "330106001的person_count为128。"  # type: ignore[union-attr]


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

    # Start via public schedule path (registers task in _run_tasks)
    orch.schedule(user_id="u", run_id=rid)
    # Wait until the tool has actually entered execute()
    await asyncio.wait_for(blocking.entered.wait(), timeout=2)
    # Verify run.started was published
    types_before = [e.type for e in await events.list_events(run_id=rid)]
    assert "run.started" in types_before
    assert "tool.started" in types_before

    # Cancel while tool is blocked inside the scheduled task
    await orch.cancel(user_id="u", run_id=rid)
    # Wait for CancelledError to propagate through the adapter
    await asyncio.wait_for(blocking.cancelled.wait(), timeout=2)

    t = await store.get_run(user_id="u", run_id=rid)
    assert t.status == "cancelled"
    types = [e.type for e in await events.list_events(run_id=rid)]
    assert types.count("run.started") == 1
    assert types.count("tool.started") == 1
    assert types.count("run.cancelled") == 1
    assert "tool.completed" not in types
    assert "result.available" not in types
    assert "run.completed" not in types


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


# ---------------------------------------------------------------------------
# 9  Native task exception observability (Native-specific)
# ---------------------------------------------------------------------------


class _ExplodingOrchestrator(NativeOrchestrator):
    """Test subclass whose execute always raises to the task boundary."""

    async def execute(self, *, user_id: str, run_id: str) -> None:
        raise RuntimeError("boom from test subclass")


@pytest.mark.asyncio
async def test_native_task_exception_observability() -> None:
    store = InMemoryAgentStore()
    events = InMemoryEventBroker()
    auth = population_auth_context()
    service = SessionRunService(store)
    registry = ToolRegistry.default()
    capability = CapabilityService(
        registry=registry,
        policy=MinimalPolicyAdapter(),
        adapter=InMemoryGovernanceAdapter(),
    )

    class _PF:
        def create(self, **kw: object) -> object:
            return _SingleToolPlanner()

    orch = _ExplodingOrchestrator(
        service=service,
        store=store,
        events=events,
        auth_context_provider=_StaticAuth(auth),
        capability=capability,
        registry=registry,
        planner_factory=_PF(),  # type: ignore[arg-type]
    )
    svc = SessionRunService(store)
    s = await svc.create_session(user_id="u", title="c")
    r = await svc.create_run(
        user_id="u", session_id=s.session_id, request=run_request(),
    )

    # schedule creates the asyncio task; the exception propagates to
    # the task boundary where the done callback captures it.
    orch.schedule(user_id="u", run_id=r.run_id)
    # Give the event loop a tick to run the task to completion
    await asyncio.sleep(0.05)

    # Exception must be recorded exactly once
    assert len(orch.task_failures) == 1
    assert "boom from test subclass" in orch.task_failures[0]


@pytest.mark.asyncio
async def test_native_cancelled_error_not_recorded() -> None:
    """CancelledError from cancel must NOT appear in task_failures."""
    blocking = _BlockingAdapter()
    store = InMemoryAgentStore()
    events = InMemoryEventBroker()
    auth = population_auth_context()
    service = SessionRunService(store)
    registry = ToolRegistry.default()
    capability = CapabilityService(
        registry=registry,
        policy=MinimalPolicyAdapter(),
        adapter=blocking,
    )

    class _PF:
        def create(self, **kw: object) -> object:
            return _SingleToolPlanner()

    orch = NativeOrchestrator(
        service=service,
        store=store,
        events=events,
        auth_context_provider=_StaticAuth(auth),
        capability=capability,
        registry=registry,
        planner_factory=_PF(),  # type: ignore[arg-type]
    )
    svc = SessionRunService(store)
    s = await svc.create_session(user_id="u", title="c")
    r = await svc.create_run(
        user_id="u", session_id=s.session_id, request=run_request(),
    )

    orch.schedule(user_id="u", run_id=r.run_id)
    await asyncio.wait_for(blocking.entered.wait(), timeout=2)
    await orch.cancel(user_id="u", run_id=r.run_id)
    await asyncio.wait_for(blocking.cancelled.wait(), timeout=2)
    # Give the done callback a tick
    await asyncio.sleep(0.05)

    assert orch.task_failures == []
