"""Tests for orchestrator factory configuration."""

import pytest

from full_view_agent.application import orchestrator_factory
from full_view_agent.application.capability_service import CapabilityService
from full_view_agent.application.harness import DefaultToolCallFingerprinter
from full_view_agent.application.native_orchestrator import NativeOrchestrator
from full_view_agent.application.orchestrator_factory import (
    create_orchestrator,
)
from full_view_agent.application.ports import OrchestrationPort
from full_view_agent.application.semantic_executor import (
    SemanticToolCallFingerprinter,
    SemanticToolExecutor,
)
from full_view_agent.application.semantic_wiring import (
    build_semantic_capability_stack,
)
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


def _kwargs(**overrides: object) -> dict:
    store = InMemoryAgentStore()
    base: dict[str, object] = {
        "service": SessionRunService(store),
        "store": store,
        "events": InMemoryEventBroker(),
        "auth_context_provider": _DummyAuth(),
        "governance_adapter": InMemoryGovernanceAdapter(),
        "tool_registry": ToolRegistry.default(),
    }
    base.update(overrides)
    return base


def test_default_is_langgraph_after_r3_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    from full_view_agent.infrastructure.langgraph_orchestrator import (
        LangGraphOrchestrator,
    )

    monkeypatch.delenv("FULL_VIEW_ORCHESTRATOR", raising=False)
    orch = create_orchestrator(**_kwargs())
    assert isinstance(orch, LangGraphOrchestrator)


def test_explicit_native(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FULL_VIEW_ORCHESTRATOR", "native")
    orch = create_orchestrator(**_kwargs())
    assert isinstance(orch, OrchestrationPort)


def test_langgraph_selects_langgraph_orchestrator(monkeypatch: pytest.MonkeyPatch) -> None:
    from full_view_agent.infrastructure.langgraph_orchestrator import (
        LangGraphOrchestrator,
    )

    monkeypatch.setenv("FULL_VIEW_ORCHESTRATOR", "langgraph")
    orch = create_orchestrator(**_kwargs())
    assert isinstance(orch, LangGraphOrchestrator)


def test_langgraph_uses_postgres_checkpoint_dependencies_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from full_view_agent.infrastructure.checkpoint_mapping_store import (
        PostgresCheckpointMappingStore,
    )
    from full_view_agent.infrastructure.langgraph_checkpoint import (
        LangGraphPostgresCheckpointManager,
    )

    monkeypatch.setenv("FULL_VIEW_ORCHESTRATOR", "langgraph")
    monkeypatch.setenv(
        "FULL_VIEW_DATABASE_URL",
        "postgresql://agent:secret@database/agent",
    )
    monkeypatch.setenv("FULL_VIEW_POSTGRES_SCHEMA", "product_schema")
    monkeypatch.setenv(
        "FULL_VIEW_LANGGRAPH_POSTGRES_SCHEMA",
        "framework_schema",
    )

    orch = create_orchestrator(**_kwargs())

    assert isinstance(
        orch._checkpoint_manager,  # noqa: SLF001
        LangGraphPostgresCheckpointManager,
    )
    assert orch._checkpoint_manager.schema == "framework_schema"  # noqa: SLF001
    assert isinstance(
        orch._checkpoint_mappings,  # noqa: SLF001
        PostgresCheckpointMappingStore,
    )


def test_unknown_value_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FULL_VIEW_ORCHESTRATOR", "bogus")
    with pytest.raises(RuntimeError, match="Unknown"):
        create_orchestrator(**_kwargs())


def test_evidence_source_system_passed_explicitly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("FULL_VIEW_ORCHESTRATOR", raising=False)
    orch = create_orchestrator(
        **_kwargs(evidence_source_system="geo-qxst")
    )
    assert isinstance(orch, OrchestrationPort)
    assert orch._evidence_source_system == "geo-qxst"  # noqa: SLF001  # type: ignore[attr-defined]


def test_memory_adapter_default_evidence_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("FULL_VIEW_ORCHESTRATOR", raising=False)
    orch = create_orchestrator(**_kwargs())
    assert orch._evidence_source_system == "in_memory_fixture"  # noqa: SLF001  # type: ignore[attr-defined]


@pytest.mark.parametrize("mode", ["native", "langgraph"])
def test_factory_injects_one_shared_tool_observation_service(
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    marker = object()
    monkeypatch.setenv("FULL_VIEW_ORCHESTRATOR", mode)
    monkeypatch.setattr(
        orchestrator_factory,
        "ToolObservationService",
        lambda **_kwargs: marker,
    )

    orch = create_orchestrator(**_kwargs())

    assert orch._observation_service is marker  # noqa: SLF001


# ---------------------------------------------------------------------------
# S1-A Native freeze：语义接线必须同时到达 Native 与 LangGraph 编排器；
# 未提供语义栈时保持 S1-A 之前的直接能力接线，行为不发生漂移。
# ---------------------------------------------------------------------------


def _semantic_stack():
    return build_semantic_capability_stack(
        registry=ToolRegistry.default(),
        adapter=InMemoryGovernanceAdapter(),
    )


def test_native_factory_wires_semantic_executor_and_fingerprinter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FULL_VIEW_ORCHESTRATOR", "native")
    stack = _semantic_stack()

    orch = create_orchestrator(**_kwargs(), semantic_stack=stack)

    # Native 路径必须是原生编排器本体，不得借道 LangGraph 子类链路。
    assert type(orch) is NativeOrchestrator
    assert orch._capability is stack.capability  # noqa: SLF001
    harness = orch._harness  # noqa: SLF001
    assert isinstance(harness._tool_executor, SemanticToolExecutor)  # noqa: SLF001
    assert isinstance(  # noqa: SLF001
        harness._call_fingerprinter,  # noqa: SLF001
        SemanticToolCallFingerprinter,
    )


def test_langgraph_factory_wires_semantic_executor_and_fingerprinter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from full_view_agent.infrastructure.langgraph_orchestrator import (
        LangGraphOrchestrator,
    )

    monkeypatch.setenv("FULL_VIEW_ORCHESTRATOR", "langgraph")
    stack = _semantic_stack()

    orch = create_orchestrator(**_kwargs(), semantic_stack=stack)

    assert isinstance(orch, LangGraphOrchestrator)
    assert orch._capability is stack.capability  # noqa: SLF001
    harness = orch._harness  # noqa: SLF001
    assert isinstance(harness._tool_executor, SemanticToolExecutor)  # noqa: SLF001
    assert isinstance(  # noqa: SLF001
        harness._call_fingerprinter,  # noqa: SLF001
        SemanticToolCallFingerprinter,
    )


def test_native_factory_without_stack_keeps_direct_capability_wiring(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FULL_VIEW_ORCHESTRATOR", "native")

    orch = create_orchestrator(**_kwargs())

    assert type(orch) is NativeOrchestrator
    assert isinstance(orch._capability, CapabilityService)  # noqa: SLF001
    harness = orch._harness  # noqa: SLF001
    # S1-A 之前的直通接线：执行器即能力服务本身，循环指纹为原始动作指纹。
    assert harness._tool_executor is orch._capability  # noqa: SLF001
    assert isinstance(  # noqa: SLF001
        harness._call_fingerprinter,  # noqa: SLF001
        DefaultToolCallFingerprinter,
    )
