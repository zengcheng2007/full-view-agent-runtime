"""Tests for orchestrator factory configuration."""

import os

import pytest

from full_view_agent.application.orchestrator_factory import (
    create_orchestrator,
)
from full_view_agent.application.ports import OrchestrationPort
from full_view_agent.application.session_run_service import SessionRunService
from full_view_agent.application.tool_registry import ToolRegistry
from full_view_agent.infrastructure.event_broker import InMemoryEventBroker
from full_view_agent.infrastructure.governance_adapter import (
    InMemoryGovernanceAdapter,
)
from full_view_agent.infrastructure.memory_store import InMemoryAgentStore


class _DummyAuth:
    async def get(self, *, user_id: str, run_id: str):  # noqa: D102
        del user_id, run_id
        raise NotImplementedError


def _kwargs() -> dict:
    store = InMemoryAgentStore()
    return {
        "service": SessionRunService(store),
        "store": store,
        "events": InMemoryEventBroker(),
        "auth_context_provider": _DummyAuth(),
        "governance_adapter": InMemoryGovernanceAdapter(),
        "tool_registry": ToolRegistry.default(),
    }


def test_default_is_native() -> None:
    os.environ.pop("FULL_VIEW_ORCHESTRATOR", None)
    orch = create_orchestrator(**_kwargs())
    assert isinstance(orch, OrchestrationPort)


def test_explicit_native() -> None:
    os.environ["FULL_VIEW_ORCHESTRATOR"] = "native"
    orch = create_orchestrator(**_kwargs())
    assert isinstance(orch, OrchestrationPort)
    os.environ.pop("FULL_VIEW_ORCHESTRATOR", None)


def test_langgraph_raises() -> None:
    os.environ["FULL_VIEW_ORCHESTRATOR"] = "langgraph"
    with pytest.raises(RuntimeError, match="reserved for R2"):
        create_orchestrator(**_kwargs())
    os.environ.pop("FULL_VIEW_ORCHESTRATOR", None)


def test_unknown_value_raises() -> None:
    os.environ["FULL_VIEW_ORCHESTRATOR"] = "bogus"
    with pytest.raises(RuntimeError, match="Unknown"):
        create_orchestrator(**_kwargs())
    os.environ.pop("FULL_VIEW_ORCHESTRATOR", None)
