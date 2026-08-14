"""Orchestrator composition factory.

Selects the concrete OrchestrationPort implementation based on
``FULL_VIEW_ORCHESTRATOR``.  The API / RuntimeContainer depend only on
OrchestrationPort – never on a concrete orchestrator class.
"""

import os
from typing import Any

from full_view_agent.application.analysis_plan_repository import AnalysisPlanRepository
from full_view_agent.application.analysis_planner import AnalysisPlanner
from full_view_agent.application.analysis_run_binding import AnalysisRunBindingStore
from full_view_agent.application.capability_service import (
    AuthContextRefresher,
    CapabilityService,
    DenialLedger,
    DynamicToolAdapter,
    ToolAdapter,
)
from full_view_agent.application.model_provider import ModelProvider
from full_view_agent.application.native_orchestrator import (
    NativeOrchestrator,
    RunPlannerFactory,
)
from full_view_agent.application.policy import MinimalPolicyAdapter
from full_view_agent.application.ports import (
    AgentStore,
    AnalysisOrchestratorPort,
    EventPublisher,
    OrchestrationPort,
)
from full_view_agent.application.run_capability_snapshot import (
    RunCapabilitySnapshotService,
)
from full_view_agent.application.runtime_workflow_registry import (
    RuntimeWorkflowRegistry,
)
from full_view_agent.application.semantic_wiring import SemanticCapabilityStack
from full_view_agent.application.session_run_service import SessionRunService
from full_view_agent.application.tool_observation_service import ToolObservationService
from full_view_agent.application.tool_registry import ToolRegistry


def create_orchestrator(
    *,
    service: SessionRunService,
    store: AgentStore,
    events: EventPublisher,
    auth_context_provider: Any,
    governance_adapter: ToolAdapter,
    tool_registry: ToolRegistry,
    evidence_source_system: str = "in_memory_fixture",
    auth_context_refresher: AuthContextRefresher | None = None,
    denial_ledger: DenialLedger | None = None,
    model_provider: ModelProvider | None = None,
    planner_factory: RunPlannerFactory | None = None,
    semantic_stack: SemanticCapabilityStack | None = None,
    dynamic_tool_adapter: DynamicToolAdapter | None = None,
    run_capability_snapshot_service: RunCapabilitySnapshotService | None = None,
    runtime_workflow_registry: RuntimeWorkflowRegistry | None = None,
) -> OrchestrationPort:
    """Build the orchestrator selected by ``FULL_VIEW_ORCHESTRATOR``.

    ``evidence_source_system`` must be passed explicitly by the
    composition root (``"geo-qxst"`` for HTTP, ``"in_memory_fixture"``
    for memory).  No class-name reflection.

    LangGraph is the R3 default. Set ``native`` explicitly only for the
    time-limited rollback window.

    When ``semantic_stack`` is provided, both orchestrators share the
    same S1-A semantic executor and canonical-action loop fingerprint
    via one Harness; without it, behavior is exactly the pre-S1-A
    direct CapabilityService wiring.
    """
    mode = os.getenv("FULL_VIEW_ORCHESTRATOR", "langgraph").lower()
    if semantic_stack is not None:
        capability: CapabilityService = semantic_stack.capability
        harness = semantic_stack.build_harness()
    else:
        capability = CapabilityService(
            registry=tool_registry,
            policy=MinimalPolicyAdapter(),
            adapter=governance_adapter,
            auth_context_refresher=auth_context_refresher,
            denial_ledger=denial_ledger,
            dynamic_tool_adapter=dynamic_tool_adapter,
        )
        harness = None
    observation_service = ToolObservationService(
        store=store,
        events=events,
        registry=tool_registry,
        evidence_source_system=evidence_source_system,
    )
    if mode == "native":
        return NativeOrchestrator(
            service=service,
            store=store,
            events=events,
            auth_context_provider=auth_context_provider,
            capability=capability,
            harness=harness,
            registry=tool_registry,
            planner_factory=planner_factory,
            evidence_source_system=evidence_source_system,
            observation_service=observation_service,
            snapshot_service=run_capability_snapshot_service,
            run_harness_factory=(
                semantic_stack.build_harness_for_registry
                if semantic_stack is not None
                else None
            ),
            runtime_workflow_registry=runtime_workflow_registry,
        )
    if mode == "langgraph":
        from full_view_agent.infrastructure.checkpoint_mapping_store import (
            PostgresCheckpointMappingStore,
        )
        from full_view_agent.infrastructure.langgraph_checkpoint import (
            LangGraphPostgresCheckpointManager,
        )
        from full_view_agent.infrastructure.langgraph_orchestrator import (
            LangGraphOrchestrator,
        )

        database_url = os.getenv("FULL_VIEW_DATABASE_URL")
        checkpoint_manager = None
        checkpoint_mappings = None
        if database_url:
            checkpoint_manager = LangGraphPostgresCheckpointManager(
                dsn=database_url,
                schema=os.getenv(
                    "FULL_VIEW_LANGGRAPH_POSTGRES_SCHEMA",
                    "full_view_agent_langgraph",
                ),
            )
            checkpoint_mappings = PostgresCheckpointMappingStore(
                dsn=database_url,
                schema=os.getenv(
                    "FULL_VIEW_POSTGRES_SCHEMA",
                    "full_view_agent",
                ),
            )
        return LangGraphOrchestrator(
            service=service,
            store=store,
            events=events,
            auth_context_provider=auth_context_provider,
            capability=capability,
            harness=harness,
            registry=tool_registry,
            planner_factory=planner_factory,
            evidence_source_system=evidence_source_system,
            observation_service=observation_service,
            checkpoint_manager=checkpoint_manager,
            checkpoint_mappings=checkpoint_mappings,
            snapshot_service=run_capability_snapshot_service,
            run_harness_factory=(
                semantic_stack.build_harness_for_registry
                if semantic_stack is not None
                else None
            ),
            runtime_workflow_registry=runtime_workflow_registry,
        )
    raise RuntimeError(
        f"Unknown FULL_VIEW_ORCHESTRATOR={mode!r}; "
        "expected 'langgraph' or rollback mode 'native'"
    )


def create_analysis_orchestrator(
    *,
    service: SessionRunService,
    store: AgentStore,
    events: EventPublisher,
    planner: AnalysisPlanner,
    plan_repository: AnalysisPlanRepository,
    semantic_stack: SemanticCapabilityStack,
    tool_registry: ToolRegistry,
    evidence_source_system: str,
    database_url: str | None,
    postgres_schema: str = "full_view_agent",
    binding_store: AnalysisRunBindingStore | None = None,
) -> AnalysisOrchestratorPort:
    """Compose the dedicated trusted-plan graph with durable production stores."""
    from full_view_agent.application.analysis_graph_execution_service import (
        AnalysisGraphExecutionService,
    )
    from full_view_agent.application.analysis_observation_validator import (
        AgentStoreAnalysisObservationValidator,
    )
    from full_view_agent.infrastructure.analysis_run_binding_store import (
        InMemoryAnalysisRunBindingStore,
        PostgresAnalysisRunBindingStore,
    )
    from full_view_agent.infrastructure.analysis_run_lease import (
        InMemoryAnalysisRunLeaseManager,
        PostgresAnalysisRunLeaseManager,
    )
    from full_view_agent.infrastructure.analysis_step_ledger_store import (
        InMemoryAnalysisStepLedgerStore,
        PostgresAnalysisStepLedgerStore,
    )
    from full_view_agent.infrastructure.checkpoint_mapping_store import (
        InMemoryCheckpointMappingStore,
        PostgresCheckpointMappingStore,
    )
    from full_view_agent.infrastructure.langgraph_analysis_orchestrator import (
        LangGraphAnalysisOrchestrator,
    )
    from full_view_agent.infrastructure.langgraph_checkpoint import (
        InMemoryCheckpointManager,
        LangGraphPostgresCheckpointManager,
    )

    observation_service = ToolObservationService(
        store=store,
        events=events,
        registry=tool_registry,
        evidence_source_system=evidence_source_system,
    )
    observation_validator = AgentStoreAnalysisObservationValidator(store)
    if database_url:
        binding_store = binding_store or PostgresAnalysisRunBindingStore(
            dsn=database_url, schema=postgres_schema
        )
        step_ledger = PostgresAnalysisStepLedgerStore(
            dsn=database_url,
            schema=postgres_schema,
            observation_validator=observation_validator,
        )
        lease_manager = PostgresAnalysisRunLeaseManager(dsn=database_url)
        checkpoint_manager = LangGraphPostgresCheckpointManager(
            dsn=database_url,
            schema=os.getenv(
                "FULL_VIEW_ANALYSIS_LANGGRAPH_POSTGRES_SCHEMA",
                "full_view_agent_analysis_langgraph",
            ),
        )
        checkpoint_mappings = PostgresCheckpointMappingStore(
            dsn=database_url, schema=postgres_schema
        )
    else:
        binding_store = binding_store or InMemoryAnalysisRunBindingStore()
        step_ledger = InMemoryAnalysisStepLedgerStore(
            observation_validator=observation_validator
        )
        lease_manager = InMemoryAnalysisRunLeaseManager()
        checkpoint_manager = InMemoryCheckpointManager()
        checkpoint_mappings = InMemoryCheckpointMappingStore()

    execution = AnalysisGraphExecutionService(
        catalog=semantic_stack.catalog,
        planner=planner,
        plan_repository=plan_repository,
        resolver=semantic_stack.resolver,
        semantic_executor=semantic_stack.executor,
        result_store=store,
        observation_service=observation_service,
        binding_store=binding_store,
        step_ledger=step_ledger,
    )
    return LangGraphAnalysisOrchestrator(
        execution=execution,
        lifecycle=service,
        lease_manager=lease_manager,
        checkpoint_manager=checkpoint_manager,
        checkpoint_mappings=checkpoint_mappings,
    )
