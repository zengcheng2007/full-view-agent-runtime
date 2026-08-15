"""LangGraph must not reinterpret a checkpointed semantic action after catalog drift."""

import pytest

from full_view_agent.application.errors import ReauthenticationRequired
from full_view_agent.application.harness import (
    AgentHarness,
    DeterministicCompletionValidator,
    FinishAction,
    HarnessState,
    ToolAction,
)
from full_view_agent.application.semantic_wiring import (
    SemanticCapabilityStack,
    build_semantic_capability_stack,
)
from full_view_agent.application.session_run_service import SessionRunService
from full_view_agent.domain.models import AuthContext, DataResult
from full_view_agent.infrastructure.checkpoint_mapping_store import (
    InMemoryCheckpointMappingStore,
)
from full_view_agent.infrastructure.event_broker import InMemoryEventBroker
from full_view_agent.infrastructure.governance_adapter import (
    InMemoryGovernanceAdapter,
)
from full_view_agent.infrastructure.langgraph_checkpoint import (
    InMemoryCheckpointManager,
)
from full_view_agent.infrastructure.langgraph_orchestrator import (
    LangGraphOrchestrator,
)
from full_view_agent.infrastructure.memory_store import InMemoryAgentStore
from full_view_agent.semantic.action_resolver import SEMANTIC_QUERY_TOOL_ID
from full_view_agent.semantic.catalog import SemanticCatalog

from .test_policy import population_auth_context
from .test_semantic_wiring import _published_population_registry
from .test_session_run_service import run_request


class _CountingAdapter(InMemoryGovernanceAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    async def execute(self, **kwargs: object) -> DataResult:
        self.calls += 1
        return await super().execute(**kwargs)


class _SemanticPlanner:
    def __init__(
        self, *, catalog_version: str, catalog_fingerprint: str
    ) -> None:
        self._catalog_version = catalog_version
        self._catalog_fingerprint = catalog_fingerprint

    async def decide(self, state: HarnessState) -> ToolAction | FinishAction:
        if state.tool_results:
            return FinishAction(summary="语义动作已过期，未执行查询。")
        return ToolAction(
            tool_id=SEMANTIC_QUERY_TOOL_ID,
            arguments={
                "catalog_version": self._catalog_version,
                "catalog_fingerprint": self._catalog_fingerprint,
                "spec": {
                    "subject": "population",
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
                },
            },
        )


class _PlannerFactory:
    def __init__(
        self, *, catalog_version: str, catalog_fingerprint: str
    ) -> None:
        self._catalog_version = catalog_version
        self._catalog_fingerprint = catalog_fingerprint

    def create(self, *, user_id: str, auth_context: AuthContext) -> _SemanticPlanner:
        del user_id, auth_context
        return _SemanticPlanner(
            catalog_version=self._catalog_version,
            catalog_fingerprint=self._catalog_fingerprint,
        )


class _StaticAuth:
    async def get(self, *, user_id: str, run_id: str) -> AuthContext:
        del user_id, run_id
        return population_auth_context()


class _InterruptBeforeSemanticHarness(AgentHarness):
    def __init__(self, *, stack: SemanticCapabilityStack) -> None:
        super().__init__(
            tool_executor=stack.executor,
            validator=DeterministicCompletionValidator(),
            call_fingerprinter=stack.fingerprinter,
        )
        self._must_interrupt = True

    async def authorize_and_execute_once(self, **kwargs):
        if self._must_interrupt:
            self._must_interrupt = False
            raise ReauthenticationRequired("interrupt before semantic admission")
        return await super().authorize_and_execute_once(**kwargs)


class _InterruptAfterSemanticHarness(AgentHarness):
    def __init__(self, *, stack: SemanticCapabilityStack) -> None:
        super().__init__(
            tool_executor=stack.executor,
            validator=DeterministicCompletionValidator(),
            call_fingerprinter=stack.fingerprinter,
        )
        self._must_interrupt = True

    def observe_once(self, **kwargs):
        if self._must_interrupt:
            self._must_interrupt = False
            raise ReauthenticationRequired("interrupt after semantic tool")
        return super().observe_once(**kwargs)


@pytest.mark.parametrize(
    ("drift_kind", "expected_code"),
    [
        ("version", "CATALOG_VERSION_MISMATCH"),
        ("content", "CATALOG_FINGERPRINT_MISMATCH"),
    ],
)
@pytest.mark.asyncio
async def test_resume_rejects_checkpointed_semantic_action_after_catalog_drift(
    drift_kind: str,
    expected_code: str,
) -> None:
    store = InMemoryAgentStore()
    events = InMemoryEventBroker()
    service = SessionRunService(store)
    registry = _published_population_registry()
    adapter = _CountingAdapter()
    checkpoint_manager = InMemoryCheckpointManager()
    checkpoint_mappings = InMemoryCheckpointMappingStore()
    original_catalog = SemanticCatalog.default()
    original_stack = build_semantic_capability_stack(
        registry=registry,
        adapter=adapter,
        catalog=original_catalog,
    )

    def orchestrator(
        *,
        stack: SemanticCapabilityStack,
        harness: AgentHarness,
        planner_catalog_version: str,
        planner_catalog_fingerprint: str,
    ) -> LangGraphOrchestrator:
        return LangGraphOrchestrator(
            service=service,
            store=store,
            events=events,
            auth_context_provider=_StaticAuth(),
            capability=stack.capability,
            registry=registry,
            planner_factory=_PlannerFactory(
                catalog_version=planner_catalog_version,
                catalog_fingerprint=planner_catalog_fingerprint,
            ),
            harness=harness,
            checkpoint_manager=checkpoint_manager,
            checkpoint_mappings=checkpoint_mappings,
        )

    session = await service.create_session(user_id="u", title="semantic version pin")
    run = await service.create_run(
        user_id="u",
        session_id=session.session_id,
        request=run_request("web-msg-semantic-version-pin"),
    )
    await orchestrator(
        stack=original_stack,
        harness=_InterruptBeforeSemanticHarness(stack=original_stack),
        planner_catalog_version=original_catalog.catalog_version,
        planner_catalog_fingerprint=original_catalog.execution_fingerprint,
    ).execute(user_id="u", run_id=run.run_id)

    waiting = await store.get_run(user_id="u", run_id=run.run_id)
    pending = store.input_requests[run.run_id]
    assert waiting.status == "waiting_input"
    assert adapter.calls == 0

    drifted_subjects = original_catalog.subjects
    if drift_kind == "content":
        population = drifted_subjects["population"]
        drifted_subjects["population"] = population.model_copy(
            update={
                "metrics": (
                    population.metrics[0].model_copy(
                        update={"definition_version": "2.0-unversioned"}
                    ),
                )
            }
        )
    drifted_catalog = SemanticCatalog(
        catalog_version=(
            "0.2.0-resume-drift"
            if drift_kind == "version"
            else original_catalog.catalog_version
        ),
        supported_spec_versions=original_catalog.supported_spec_versions,
        subjects=drifted_subjects,
        bindings=original_catalog.bindings,
    )
    restarted_stack = build_semantic_capability_stack(
        registry=registry,
        adapter=adapter,
        catalog=drifted_catalog,
    )
    restarted = orchestrator(
        stack=restarted_stack,
        harness=restarted_stack.build_harness(),
        planner_catalog_version=drifted_catalog.catalog_version,
        planner_catalog_fingerprint=drifted_catalog.execution_fingerprint,
    )
    await restarted.resume(
        user_id="u",
        run_id=run.run_id,
        input_request_id=pending.input_request_id,
        run_state_version=pending.run_state_version,
    )
    await restarted.execute(user_id="u", run_id=run.run_id)

    completed = await store.get_run(user_id="u", run_id=run.run_id)
    assert completed.status == "completed"
    assert completed.outcome == "denied"
    assert completed.completion_reason_code == expected_code
    assert adapter.calls == 0


@pytest.mark.asyncio
async def test_resume_does_not_repeat_completed_semantic_tool() -> None:
    store = InMemoryAgentStore()
    events = InMemoryEventBroker()
    service = SessionRunService(store)
    registry = _published_population_registry()
    adapter = _CountingAdapter()
    checkpoint_manager = InMemoryCheckpointManager()
    checkpoint_mappings = InMemoryCheckpointMappingStore()
    catalog = SemanticCatalog.default()
    stack = build_semantic_capability_stack(
        registry=registry,
        adapter=adapter,
        catalog=catalog,
    )

    def orchestrator(harness: AgentHarness) -> LangGraphOrchestrator:
        return LangGraphOrchestrator(
            service=service,
            store=store,
            events=events,
            auth_context_provider=_StaticAuth(),
            capability=stack.capability,
            registry=registry,
            planner_factory=_PlannerFactory(
                catalog_version=catalog.catalog_version,
                catalog_fingerprint=catalog.execution_fingerprint,
            ),
            harness=harness,
            checkpoint_manager=checkpoint_manager,
            checkpoint_mappings=checkpoint_mappings,
        )

    session = await service.create_session(user_id="u", title="semantic safe resume")
    run = await service.create_run(
        user_id="u",
        session_id=session.session_id,
        request=run_request("web-msg-semantic-safe-resume"),
    )
    await orchestrator(
        _InterruptAfterSemanticHarness(stack=stack)
    ).execute(user_id="u", run_id=run.run_id)

    waiting = await store.get_run(user_id="u", run_id=run.run_id)
    pending = store.input_requests[run.run_id]
    assert waiting.status == "waiting_input"
    assert adapter.calls == 1

    restarted = orchestrator(stack.build_harness())
    await restarted.resume(
        user_id="u",
        run_id=run.run_id,
        input_request_id=pending.input_request_id,
        run_state_version=pending.run_state_version,
    )
    await restarted.execute(user_id="u", run_id=run.run_id)

    completed = await store.get_run(user_id="u", run_id=run.run_id)
    event_types = [
        event.type for event in await events.list_events(run_id=run.run_id)
    ]
    assert completed.status == "completed"
    assert completed.outcome == "success"
    assert adapter.calls == 1
    assert event_types.count("tool.started") == 1
    assert event_types.count("tool.completed") == 1
    assert event_types.count("result.available") == 1
