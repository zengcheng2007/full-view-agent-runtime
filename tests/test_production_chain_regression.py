"""Regression tests for WSZC-16 production chain hardening.

These tests cover the three production-chain gaps identified in the
WSZC-16 audit:

1. V034/V035 migrations must be in the startup migration list.
2. ``display_order`` must drive the real prompt ordering, not only
   the capability_routes preview.
3. ``guidance_examples`` must reach the actual model context, not
   just the API preview surface.
"""

from __future__ import annotations

import pathlib

import pytest

from full_view_agent.application.context_builder import AgentContextBuilder
from full_view_agent.application.session_run_service import SessionRunService
from full_view_agent.application.tool_registry import ToolRegistry
from full_view_agent.application.harness import HarnessState
from full_view_agent.infrastructure.memory_store import InMemoryAgentStore

from .test_policy import population_auth_context
from .test_session_run_service import run_request


# ---------------------------------------------------------------------------
# 1. V034/V035 in startup migration list
# ---------------------------------------------------------------------------


def test_v034_v035_in_startup_migration_list() -> None:
    """postgres_persistence must include V034 + V035 in its migration list."""

    from full_view_agent.infrastructure.postgres_persistence import (
        PostgresAgentPersistence,
    )

    persistence = PostgresAgentPersistence.__new__(PostgresAgentPersistence)
    persistence._p2_migrations_cache = None  # type: ignore[attr-defined]
    migrations = persistence._p2_migration_statements()  # type: ignore[attr-defined]

    assert any("guidance_examples" in stmt or "V034" in stmt for stmt in migrations), (
        "V034 missing from startup migrations"
    )
    assert any("knowledge.search" in stmt or "V035" in stmt for stmt in migrations), (
        "V035 missing from startup migrations"
    )
    migrations_dir = (
        pathlib.Path(__file__).resolve().parents[1] / "scripts" / "migrations"
    )
    assert (migrations_dir / "V034_capability_guidance_fields.sql").is_file()
    assert (migrations_dir / "V035_populate_tool_guidance.sql").is_file()


# ---------------------------------------------------------------------------
# Helpers for tests 2 + 3
# ---------------------------------------------------------------------------


def _default_registry_subset(tool_ids: list[str]) -> ToolRegistry:
    default = ToolRegistry.default()
    manifests = [default.get_manifest(tid) for tid in tool_ids]
    descriptors = [default.get_model_descriptor(tid) for tid in tool_ids]
    return ToolRegistry(manifests=manifests, descriptors=descriptors)


def _auth_for_tools(tool_ids: list[str]):
    dataset_map = {
        "governance.resolve_area": (
            "administrative_area",
            "governance.area.read",
        ),
        "governance.query_population_metrics": (
            "population",
            "governance.population.aggregate.read",
        ),
        "governance.query_event_metrics": (
            "event",
            "governance.event.aggregate.read",
        ),
    }
    base = population_auth_context()
    datasets = set(base.data_scopes.datasets)
    entitlements: list[str] = []
    for tid in tool_ids:
        ds, perm = dataset_map.get(tid, ("population", f"tool.{tid}"))
        datasets.add(ds)
        entitlements.append(perm)
    new_scopes = base.data_scopes.model_copy(
        update={"datasets": sorted(datasets)}
    )
    return base.model_copy(
        update={
            "entitlements": entitlements,
            "data_scopes": new_scopes,
        }
    )


async def _build_with_session(
    tool_ids: list[str],
    *,
    guidance: dict[str, str] | None = None,
    display_order: dict[str, int] | None = None,
    examples: dict[str, list[dict[str, object]]] | None = None,
):
    store = InMemoryAgentStore()
    service = SessionRunService(store)
    session = await service.create_session(user_id="user-01", title="regression")
    run = await service.create_run(
        user_id="user-01",
        session_id=session.session_id,
        request=run_request(),
    )
    registry = _default_registry_subset(tool_ids)
    builder = AgentContextBuilder(
        store=store,
        registry=registry,
        capability_guidance=guidance or {},
    )
    builder.update_capability_guidance(
        guidance or {},
        display_order=display_order,
        examples=examples,
    )
    auth = _auth_for_tools(tool_ids).model_copy(
        update={"session_id": session.session_id, "run_id": run.run_id}
    )
    return "user-01", auth, builder


# ---------------------------------------------------------------------------
# 2. display_order drives the real prompt ordering
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_display_order_orders_real_prompt_lines() -> None:
    """Real AgentContextBuilder prompt must sort by display_order, not
    registry insertion order."""

    tool_ids = [
        "governance.query_event_metrics",
        "governance.resolve_area",
        "governance.query_population_metrics",
    ]
    user_id, auth, builder = await _build_with_session(
        tool_ids,
        guidance={
            "governance.query_event_metrics": "events",
            "governance.resolve_area": "area",
            "governance.query_population_metrics": "population",
        },
        display_order={
            "governance.query_event_metrics": 50,
            "governance.resolve_area": 20,
            "governance.query_population_metrics": 30,
        },
    )
    request = await builder.build(
        user_id=user_id,
        auth_context=auth,
        state=HarnessState(),
    )
    system_content = request.messages[0].content
    start = system_content.index("[RUN_PUBLISHED_CAPABILITIES]")
    block = system_content[start:]
    pos_area = block.index("governance.resolve_area")
    pos_pop = block.index("governance.query_population_metrics")
    pos_event = block.index("governance.query_event_metrics")
    # display_order: area=20 < population=30 < event=50
    assert pos_area < pos_pop < pos_event, (
        f"display_order not honoured in real prompt: area={pos_area}, "
        f"pop={pos_pop}, event={pos_event}"
    )


# ---------------------------------------------------------------------------
# 3. guidance_examples reach the real model context
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_guidance_examples_rendered_in_real_prompt() -> None:
    """Real prompt must include guidance_examples as few-shot lines."""

    tool_ids = ["governance.resolve_area"]
    user_id, auth, builder = await _build_with_session(
        tool_ids,
        guidance={
            "governance.resolve_area": "resolve area names to codes.",
        },
        examples={
            "governance.resolve_area": [
                {
                    "question": "杭州市西湖区",
                    "reasoning": "需要把区划名转换成标准编码",
                    "expected_output": "330106",
                }
            ]
        },
    )
    request = await builder.build(
        user_id=user_id,
        auth_context=auth,
        state=HarnessState(),
    )
    system_content = request.messages[0].content
    assert "杭州市西湖区" in system_content, (
        "guidance_examples not rendered into real model context"
    )
    assert "330106" in system_content
    assert "推理" in system_content
