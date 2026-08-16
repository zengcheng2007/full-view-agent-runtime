from __future__ import annotations

from datetime import UTC, datetime

import pytest

from full_view_agent.application.capability_service import CapabilityService
from full_view_agent.application.harness import FinishAction, HarnessState, ToolAction
from full_view_agent.application.native_orchestrator import NativeOrchestrator
from full_view_agent.application.policy import MinimalPolicyAdapter
from full_view_agent.application.run_capability_snapshot import RunCapabilitySnapshot
from full_view_agent.application.semantic_wiring import (
    SemanticCapabilityStack,
    build_semantic_capability_stack,
)
from full_view_agent.application.session_run_service import SessionRunService
from full_view_agent.application.tool_registry import ToolRegistry
from full_view_agent.domain.models import AuthContext
from full_view_agent.infrastructure.event_broker import InMemoryEventBroker
from full_view_agent.infrastructure.governance_adapter import InMemoryGovernanceAdapter
from full_view_agent.infrastructure.memory_store import InMemoryAgentStore

from .test_policy import population_auth_context
from .test_session_run_service import run_request


class _StaticAuthContextProvider:
    def __init__(self, auth_context: AuthContext) -> None:
        self._auth_context = auth_context

    async def get(self, *, user_id: str, run_id: str) -> AuthContext:
        del user_id, run_id
        return self._auth_context


class _PopulationPlanner:
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


class _PopulationPlannerFactory:
    def __init__(self) -> None:
        self.bound_registry: ToolRegistry | None = None
        self.bound_run_id: str | None = None

    async def for_run(
        self,
        *,
        run_id: str,
        registry: ToolRegistry,
        requested_mode: object = None,
        latest_user_text: str = "",
        execution_policy: object = None,
    ) -> _PopulationPlannerFactory:
        del requested_mode, latest_user_text, execution_policy
        self.bound_run_id = run_id
        self.bound_registry = registry
        return self

    def for_registry(self, registry: ToolRegistry) -> _PopulationPlannerFactory:
        self.bound_registry = registry
        return self

    def create(self, *, user_id: str, auth_context: AuthContext) -> _PopulationPlanner:
        del user_id, auth_context
        return _PopulationPlanner()


class _SemanticPopulationPlanner:
    def __init__(self, stack: SemanticCapabilityStack) -> None:
        self._stack = stack

    async def decide(self, state: HarnessState) -> ToolAction | FinishAction:
        if state.tool_results:
            return FinishAction(summary="查询完成")
        return ToolAction(
            tool_id="governance.semantic_query",
            arguments={
                "catalog_version": self._stack.catalog.catalog_version,
                "catalog_fingerprint": self._stack.catalog.execution_fingerprint,
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


class _SemanticPopulationPlannerFactory:
    def __init__(self, stack: SemanticCapabilityStack) -> None:
        self._stack = stack

    def create(
        self, *, user_id: str, auth_context: AuthContext
    ) -> _SemanticPopulationPlanner:
        del user_id, auth_context
        return _SemanticPopulationPlanner(self._stack)


class _PinnedSnapshotService:
    def __init__(self, registry: ToolRegistry) -> None:
        self._registry = registry

    async def create_snapshot_for_run(
        self,
        run_id: str,
        base_registry: ToolRegistry,
        app_id: str | None = None,
    ) -> RunCapabilitySnapshot:
        del base_registry, app_id
        return RunCapabilitySnapshot(
            run_id=run_id,
            created_at=datetime.now(UTC),
            tool_registry=self._registry,
            tool_versions={"governance.query_population_metrics": "2.0.0"},
        )

    def remove_snapshot(self, run_id: str) -> None:
        del run_id


@pytest.mark.asyncio
async def test_native_run_executes_the_registry_pinned_by_its_snapshot() -> None:
    base_registry = ToolRegistry.default()
    tool_id = "governance.query_population_metrics"
    pinned_registry = base_registry.merge_dynamic(
        manifests=[
            base_registry.get_manifest(tool_id).model_copy(
                update={"tool_version": "2.0.0"}
            )
        ],
        descriptors=[
            base_registry.get_model_descriptor(tool_id).model_copy(
                update={"tool_version": "2.0.0"}
            )
        ],
    )
    store = InMemoryAgentStore()
    events = InMemoryEventBroker()
    service = SessionRunService(store)
    capability = CapabilityService(
        registry=base_registry,
        policy=MinimalPolicyAdapter(),
        adapter=InMemoryGovernanceAdapter(),
    )
    planner_factory = _PopulationPlannerFactory()
    orchestrator = NativeOrchestrator(
        service=service,
        store=store,
        events=events,
        auth_context_provider=_StaticAuthContextProvider(population_auth_context()),
        capability=capability,
        registry=base_registry,
        planner_factory=planner_factory,
        snapshot_service=_PinnedSnapshotService(pinned_registry),  # type: ignore[arg-type]
    )
    session = await service.create_session(user_id="u", title="snapshot execution")
    run = await service.create_run(
        user_id="u",
        session_id=session.session_id,
        request=run_request(),
    )

    await orchestrator.execute(user_id="u", run_id=run.run_id)

    completed = next(
        event
        for event in await events.list_events(run_id=run.run_id)
        if event.type == "tool.completed"
    )
    tool_result = completed.data["tool_result"]
    assert isinstance(tool_result, dict)
    assert tool_result["tool_version"] == "2.0.0"
    assert planner_factory.bound_registry is pinned_registry
    assert planner_factory.bound_run_id == run.run_id


@pytest.mark.asyncio
async def test_snapshot_execution_preserves_the_semantic_query_pipeline() -> None:
    base_registry = ToolRegistry.default()
    # A distinct immutable registry object exercises the run-snapshot branch
    # without changing the canonical semantic catalog bindings.
    pinned_registry = base_registry.merge_dynamic(manifests=[], descriptors=[])
    store = InMemoryAgentStore()
    events = InMemoryEventBroker()
    service = SessionRunService(store)
    stack = build_semantic_capability_stack(
        registry=base_registry,
        adapter=InMemoryGovernanceAdapter(),
    )
    orchestrator = NativeOrchestrator(
        service=service,
        store=store,
        events=events,
        auth_context_provider=_StaticAuthContextProvider(population_auth_context()),
        capability=stack.capability,
        harness=stack.build_harness(),
        registry=base_registry,
        planner_factory=_SemanticPopulationPlannerFactory(stack),
        snapshot_service=_PinnedSnapshotService(pinned_registry),  # type: ignore[arg-type]
        run_harness_factory=stack.build_harness_for_registry,
    )
    session = await service.create_session(user_id="u", title="semantic snapshot")
    run = await service.create_run(
        user_id="u",
        session_id=session.session_id,
        request=run_request(),
    )

    await orchestrator.execute(user_id="u", run_id=run.run_id)

    completed = [
        event
        for event in await events.list_events(run_id=run.run_id)
        if event.type == "tool.completed"
    ]
    assert completed
    tool_result = completed[0].data["tool_result"]
    assert isinstance(tool_result, dict)
    assert tool_result["tool_id"] == "governance.query_population_metrics"
    assert tool_result["semantic_lineage"]["subject"] == "population"
