import os
from uuid import uuid4

import psycopg
import pytest

from full_view_agent.application.capability_service import CapabilityService
from full_view_agent.application.checkpoint_mapping import CheckpointMappingStore
from full_view_agent.application.errors import ReauthenticationRequired
from full_view_agent.application.harness import (
    AgentHarness,
    DeterministicCompletionValidator,
    FinishAction,
    HarnessState,
    ToolAction,
)
from full_view_agent.application.policy import MinimalPolicyAdapter
from full_view_agent.application.session_run_service import SessionRunService
from full_view_agent.application.tool_registry import ToolRegistry
from full_view_agent.domain.models import (
    AuthContext,
    AuthDataScopes,
    AuthorizedAreaScope,
    DataResult,
)
from full_view_agent.infrastructure.checkpoint_mapping_store import (
    InMemoryCheckpointMappingStore,
    PostgresCheckpointMappingStore,
)
from full_view_agent.infrastructure.event_broker import InMemoryEventBroker
from full_view_agent.infrastructure.governance_adapter import (
    InMemoryGovernanceAdapter,
)
from full_view_agent.infrastructure.langgraph_checkpoint import (
    InMemoryCheckpointManager,
    LangGraphCheckpointManager,
    LangGraphPostgresCheckpointManager,
)
from full_view_agent.infrastructure.langgraph_orchestrator import (
    LangGraphOrchestrator,
)
from full_view_agent.infrastructure.memory_store import InMemoryAgentStore

from .test_policy import population_auth_context
from .test_session_run_service import run_request


class CountingAdapter(InMemoryGovernanceAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    async def execute(self, **kwargs: object) -> DataResult:
        self.calls += 1
        return await super().execute(**kwargs)


class SingleToolPlanner:
    async def decide(self, state: HarnessState) -> ToolAction | FinishAction:
        if state.tool_results:
            return FinishAction(summary="查询完成")
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


class StaticPlannerFactory:
    def create(
        self,
        *,
        user_id: str,
        auth_context: AuthContext,
    ) -> SingleToolPlanner:
        del user_id, auth_context
        return SingleToolPlanner()


class StaticAuth:
    async def get(self, *, user_id: str, run_id: str) -> AuthContext:
        del user_id, run_id
        return population_auth_context()


class MutableAuth(StaticAuth):
    def __init__(self) -> None:
        self.context = population_auth_context()

    async def get(self, *, user_id: str, run_id: str) -> AuthContext:
        del user_id, run_id
        return self.context


class InterruptAfterToolHarness(AgentHarness):
    def __init__(self, *, capability: CapabilityService) -> None:
        super().__init__(
            tool_executor=capability,
            validator=DeterministicCompletionValidator(),
        )
        self._must_interrupt = True

    def observe_once(self, **kwargs):
        if self._must_interrupt:
            self._must_interrupt = False
            raise ReauthenticationRequired("interrupt after durable tool node")
        return super().observe_once(**kwargs)


class InterruptBeforeToolHarness(AgentHarness):
    def __init__(self, *, capability: CapabilityService) -> None:
        super().__init__(
            tool_executor=capability,
            validator=DeterministicCompletionValidator(),
        )
        self._must_interrupt = True

    async def authorize_and_execute_once(self, **kwargs):
        if self._must_interrupt:
            self._must_interrupt = False
            raise ReauthenticationRequired("interrupt before tool admission")
        return await super().authorize_and_execute_once(**kwargs)


def postgres_test_dsn() -> str:
    dsn = os.getenv("FULL_VIEW_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("FULL_VIEW_TEST_DATABASE_URL is not configured")
    return dsn


async def exercise_safe_resume(
    *,
    checkpoint_manager: LangGraphCheckpointManager,
    checkpoint_mappings: CheckpointMappingStore,
    restarted_checkpoint_manager: LangGraphCheckpointManager | None = None,
    restarted_checkpoint_mappings: CheckpointMappingStore | None = None,
) -> None:
    store = InMemoryAgentStore()
    events = InMemoryEventBroker()
    service = SessionRunService(store)
    registry = ToolRegistry.default()
    adapter = CountingAdapter()
    capability = CapabilityService(
        registry=registry,
        policy=MinimalPolicyAdapter(),
        adapter=adapter,
    )
    harness = InterruptAfterToolHarness(capability=capability)
    def build_orchestrator(
        manager: LangGraphCheckpointManager,
        mappings: CheckpointMappingStore,
    ) -> LangGraphOrchestrator:
        return LangGraphOrchestrator(
            service=service,
            store=store,
            events=events,
            auth_context_provider=StaticAuth(),
            capability=capability,
            registry=registry,
            planner_factory=StaticPlannerFactory(),
            harness=harness,
            checkpoint_manager=manager,
            checkpoint_mappings=mappings,
        )

    session = await service.create_session(user_id="u", title="safe resume")
    run = await service.create_run(
        user_id="u",
        session_id=session.session_id,
        request=run_request("web-msg-safe-resume"),
    )

    await build_orchestrator(
        checkpoint_manager,
        checkpoint_mappings,
    ).execute(user_id="u", run_id=run.run_id)
    waiting = await store.get_run(user_id="u", run_id=run.run_id)
    pending = store.input_requests[run.run_id]
    assert waiting.status == "waiting_input"
    assert adapter.calls == 1

    restarted = build_orchestrator(
        restarted_checkpoint_manager or checkpoint_manager,
        restarted_checkpoint_mappings or checkpoint_mappings,
    )
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
    assert adapter.calls == 1
    assert event_types.count("tool.started") == 1
    assert event_types.count("tool.completed") == 1
    assert event_types.count("result.available") == 1
    assert event_types.count("run.completed") == 1


async def test_resume_from_safe_checkpoint_does_not_repeat_completed_tool() -> None:
    await exercise_safe_resume(
        checkpoint_manager=InMemoryCheckpointManager(),
        checkpoint_mappings=InMemoryCheckpointMappingStore(),
    )


@pytest.mark.db
async def test_postgres_checkpoint_survives_orchestrator_and_manager_restart() -> None:
    suffix = uuid4().hex[:12]
    product_schema = f"fva_mapping_test_{suffix}"
    framework_schema = f"fva_graph_test_{suffix}"
    try:
        await exercise_safe_resume(
            checkpoint_manager=LangGraphPostgresCheckpointManager(
                dsn=postgres_test_dsn(),
                schema=framework_schema,
            ),
            checkpoint_mappings=PostgresCheckpointMappingStore(
                dsn=postgres_test_dsn(),
                schema=product_schema,
            ),
            restarted_checkpoint_manager=LangGraphPostgresCheckpointManager(
                dsn=postgres_test_dsn(),
                schema=framework_schema,
            ),
            restarted_checkpoint_mappings=PostgresCheckpointMappingStore(
                dsn=postgres_test_dsn(),
                schema=product_schema,
            ),
        )
    finally:
        async with await psycopg.AsyncConnection.connect(
            postgres_test_dsn(),
        ) as connection:
            await connection.execute(
                f'DROP SCHEMA IF EXISTS "{framework_schema}" CASCADE'
            )
            await connection.execute(
                f'DROP SCHEMA IF EXISTS "{product_schema}" CASCADE'
            )


async def test_resume_rechecks_current_scope_before_pending_tool() -> None:
    store = InMemoryAgentStore()
    events = InMemoryEventBroker()
    service = SessionRunService(store)
    registry = ToolRegistry.default()
    adapter = CountingAdapter()
    capability = CapabilityService(
        registry=registry,
        policy=MinimalPolicyAdapter(),
        adapter=adapter,
    )
    harness = InterruptBeforeToolHarness(capability=capability)
    auth = MutableAuth()
    checkpoint_manager = InMemoryCheckpointManager()
    checkpoint_mappings = InMemoryCheckpointMappingStore()

    def build_orchestrator() -> LangGraphOrchestrator:
        return LangGraphOrchestrator(
            service=service,
            store=store,
            events=events,
            auth_context_provider=auth,
            capability=capability,
            registry=registry,
            planner_factory=StaticPlannerFactory(),
            harness=harness,
            checkpoint_manager=checkpoint_manager,
            checkpoint_mappings=checkpoint_mappings,
        )

    session = await service.create_session(user_id="u", title="scope recheck")
    run = await service.create_run(
        user_id="u",
        session_id=session.session_id,
        request=run_request("web-msg-scope-recheck"),
    )
    await build_orchestrator().execute(user_id="u", run_id=run.run_id)
    waiting = await store.get_run(user_id="u", run_id=run.run_id)
    pending = store.input_requests[run.run_id]
    assert waiting.status == "waiting_input"
    assert adapter.calls == 0
    mapping = await checkpoint_mappings.get_mapping(
        user_id="u",
        run_id=run.run_id,
    )
    async with checkpoint_manager.saver() as saver:
        checkpoint = await saver.aget_tuple(
            {
                "configurable": {
                    "thread_id": mapping.thread_id,
                    "checkpoint_ns": mapping.checkpoint_ns,
                }
            }
        )
    assert checkpoint is not None
    assert any(
        channel == "__interrupt__"
        for _task_id, channel, _value in checkpoint.pending_writes
    )

    original = auth.context
    auth.context = original.model_copy(
        update={
            "data_scopes": AuthDataScopes(
                areas=[
                    AuthorizedAreaScope(
                        area_code="330105",
                        include_descendants=True,
                    )
                ],
                datasets=original.data_scopes.datasets,
                field_policy_set=original.data_scopes.field_policy_set,
            )
        }
    )
    restarted = build_orchestrator()
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
    assert adapter.calls == 0
