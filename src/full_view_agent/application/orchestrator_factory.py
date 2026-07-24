"""Orchestrator composition factory.

Selects the concrete OrchestrationPort implementation based on
``FULL_VIEW_ORCHESTRATOR``.  The API / RuntimeContainer depend only on
OrchestrationPort – never on a concrete orchestrator class.
"""

import os
from typing import Any

from full_view_agent.application.capability_service import (
    AuthContextRefresher,
    CapabilityService,
    DenialLedger,
    ToolAdapter,
)
from full_view_agent.application.model_planner import ModelPlannerFactory
from full_view_agent.application.model_provider import ModelProvider
from full_view_agent.application.native_orchestrator import NativeOrchestrator
from full_view_agent.application.policy import MinimalPolicyAdapter
from full_view_agent.application.ports import (
    AgentStore,
    EventPublisher,
    OrchestrationPort,
)
from full_view_agent.application.session_run_service import SessionRunService
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
    planner_factory: ModelPlannerFactory | None = None,
) -> OrchestrationPort:
    """Build the orchestrator selected by ``FULL_VIEW_ORCHESTRATOR``.

    ``evidence_source_system`` must be passed explicitly by the
    composition root (``"geo-qxst"`` for HTTP, ``"in_memory_fixture"``
    for memory).  No class-name reflection.

    Currently only ``native`` is accepted.  ``langgraph`` is reserved
    for R2 and will raise ``RuntimeError`` if requested.
    """
    mode = os.getenv("FULL_VIEW_ORCHESTRATOR", "native").lower()
    if mode == "native":
        return NativeOrchestrator(
            service=service,
            store=store,
            events=events,
            auth_context_provider=auth_context_provider,
            capability=CapabilityService(
                registry=tool_registry,
                policy=MinimalPolicyAdapter(),
                adapter=governance_adapter,
                auth_context_refresher=auth_context_refresher,
                denial_ledger=denial_ledger,
            ),
            registry=tool_registry,
            planner_factory=planner_factory,
            evidence_source_system=evidence_source_system,
        )
    if mode == "langgraph":
        raise RuntimeError(
            "FULL_VIEW_ORCHESTRATOR=langgraph is reserved for R2; "
            "not yet implemented"
        )
    raise RuntimeError(
        f"Unknown FULL_VIEW_ORCHESTRATOR={mode!r}; "
        "expected 'native' (or 'langgraph' in R2)"
    )
