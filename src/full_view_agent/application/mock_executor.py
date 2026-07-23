"""Backwards-compatible re-export.

The production loop has been migrated to NativeOrchestrator. This module
kept as a thin alias so existing imports (mostly tests) keep working
during the transition. Do not add new code here.
"""

from full_view_agent.application.native_orchestrator import (
    AuthContextProvider,
    CapabilityExecutor,
    MockRunExecutor,
    NativeOrchestrator,
    RunPlannerFactory,
    _action_area_codes,
)

__all__ = [
    "AuthContextProvider",
    "CapabilityExecutor",
    "MockRunExecutor",
    "NativeOrchestrator",
    "RunPlannerFactory",
    "_action_area_codes",
]
