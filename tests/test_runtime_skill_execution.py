import pytest

from full_view_agent.application.context_builder import AgentContextBuilder
from full_view_agent.application.dynamic_skill_workflow_bridge import (
    RuntimeSkillContract,
)
from full_view_agent.application.errors import ModelContractError
from full_view_agent.application.harness import HarnessState, ToolAction
from full_view_agent.application.model_planner import ModelPlanner
from full_view_agent.application.model_provider import (
    ModelRequest,
    ModelResponse,
    ModelToolCall,
    ModelUsage,
)
from full_view_agent.application.runtime_skill_registry import (
    SKILL_INVOKE_TOOL_ID,
    RuntimeSkillRegistry,
)
from full_view_agent.application.session_run_service import SessionRunService
from full_view_agent.application.tool_registry import ToolRegistry
from full_view_agent.infrastructure.memory_store import InMemoryAgentStore
from full_view_agent.semantic.catalog import SemanticCatalog
from full_view_agent.semantic.presenter import SemanticToolPresenter

from .test_policy import population_auth_context
from .test_session_run_service import run_request


def _population_skill(*, skill_id: str = "skill.population-analysis") -> RuntimeSkillContract:
    return RuntimeSkillContract(
        skill_id=skill_id,
        version="1.0.0",
        guidance="先确认区域，再查询人口指标；只依据查询结果回答。",
        allowed_tool_ids=("governance.query_population_metrics",),
        applicable_questions=("人口分布", "独居老人分析"),
    )


def _semantic_skill() -> RuntimeSkillContract:
    return RuntimeSkillContract(
        skill_id="skill.semantic-analysis",
        version="1.0.0",
        guidance="使用受控语义查询完成用户要求的分组和排序。",
        allowed_tool_ids=("governance.semantic_query",),
        applicable_questions=("区域指标分析",),
    )


@pytest.mark.asyncio
async def test_published_skill_hides_covered_tool_behind_server_validated_entry() -> None:
    store = InMemoryAgentStore()
    service = SessionRunService(store)
    session = await service.create_session(user_id="skill-user", title="Skill")
    run = await service.create_run(
        user_id="skill-user",
        session_id=session.session_id,
        request=run_request(),
    )
    auth_context = population_auth_context().model_copy(
        update={"session_id": session.session_id, "run_id": run.run_id}
    )
    skills = RuntimeSkillRegistry((_population_skill(),))

    request = await AgentContextBuilder(
        store=store,
        registry=ToolRegistry.default(),
        skill_registry=skills,
    ).build(
        user_id="skill-user",
        auth_context=auth_context,
        state=HarnessState(),
    )

    assert [tool.tool_id for tool in request.tools] == [SKILL_INVOKE_TOOL_ID]
    skill_tool = request.tools[0]
    assert skill_tool.skill_tool_allowlists == {
        "skill.population-analysis": ("governance.query_population_metrics",)
    }
    assert set(skill_tool.wrapped_tool_definitions) == {
        "governance.query_population_metrics"
    }
    system_text = "\n".join(
        message.content or "" for message in request.messages if message.role == "system"
    )
    assert "skill.population-analysis" in system_text
    assert "先确认区域" in system_text
    assert "人口分布" in system_text


@pytest.mark.asyncio
async def test_context_builder_uses_run_pinned_skill_after_live_disable() -> None:
    store = InMemoryAgentStore()
    service = SessionRunService(store)
    session = await service.create_session(user_id="skill-user", title="Skill pin")
    run = await service.create_run(
        user_id="skill-user",
        session_id=session.session_id,
        request=run_request(),
    )
    auth_context = population_auth_context().model_copy(
        update={"session_id": session.session_id, "run_id": run.run_id}
    )
    live = RuntimeSkillRegistry((_population_skill(),))
    pinned = live.snapshot()
    live.replace(())
    builder = AgentContextBuilder(
        store=store,
        registry=ToolRegistry.default(),
        skill_registry=live,
    ).for_registry(ToolRegistry.default(), skill_registry=pinned)

    request = await builder.build(
        user_id="skill-user",
        auth_context=auth_context,
        state=HarnessState(),
    )

    assert [tool.tool_id for tool in request.tools] == [SKILL_INVOKE_TOOL_ID]


@pytest.mark.asyncio
async def test_skill_can_wrap_the_authorized_semantic_facade_in_production_shape() -> None:
    store = InMemoryAgentStore()
    service = SessionRunService(store)
    session = await service.create_session(user_id="skill-user", title="Skill")
    run = await service.create_run(
        user_id="skill-user",
        session_id=session.session_id,
        request=run_request(),
    )
    auth_context = population_auth_context().model_copy(
        update={"session_id": session.session_id, "run_id": run.run_id}
    )

    request = await AgentContextBuilder(
        store=store,
        registry=ToolRegistry.default(),
        semantic_presenter=SemanticToolPresenter(catalog=SemanticCatalog.default()),
        skill_registry=RuntimeSkillRegistry((_semantic_skill(),)),
    ).build(
        user_id="skill-user",
        auth_context=auth_context,
        state=HarnessState(),
    )

    assert [tool.tool_id for tool in request.tools] == [SKILL_INVOKE_TOOL_ID]
    assert set(request.tools[0].wrapped_tool_definitions) == {
        "governance.semantic_query"
    }


class _SkillCallingProvider:
    def __init__(self, *, skill_id: str, tool_id: str) -> None:
        self._skill_id = skill_id
        self._tool_id = tool_id

    async def complete(self, request: ModelRequest) -> ModelResponse:
        del request
        return ModelResponse(
            content=None,
            tool_calls=(
                ModelToolCall(
                    tool_id=SKILL_INVOKE_TOOL_ID,
                    arguments={
                        "skill_id": self._skill_id,
                        "tool_id": self._tool_id,
                        "arguments": {
                            "query": {"metrics": ["solitary_elderly_count"]}
                        },
                    },
                ),
            ),
            finish_reason="tool_calls",
            usage=ModelUsage(total_tokens=10),
        )


@pytest.mark.asyncio
async def test_model_planner_unwraps_valid_skill_call_to_real_tool_action() -> None:
    store = InMemoryAgentStore()
    service = SessionRunService(store)
    session = await service.create_session(user_id="skill-user", title="Skill")
    run = await service.create_run(
        user_id="skill-user",
        session_id=session.session_id,
        request=run_request(),
    )
    auth_context = population_auth_context().model_copy(
        update={"session_id": session.session_id, "run_id": run.run_id}
    )
    context_builder = AgentContextBuilder(
        store=store,
        registry=ToolRegistry.default(),
        skill_registry=RuntimeSkillRegistry((_population_skill(),)),
    )

    action = await ModelPlanner(
        provider=_SkillCallingProvider(
            skill_id="skill.population-analysis",
            tool_id="governance.query_population_metrics",
        ),
        context_builder=context_builder,
        user_id="skill-user",
        auth_context=auth_context,
    ).decide(HarnessState())

    assert isinstance(action, ToolAction)
    assert action.tool_id == "governance.query_population_metrics"
    assert action.arguments == {
        "query": {"metrics": ["solitary_elderly_count"]}
    }


@pytest.mark.asyncio
async def test_model_planner_rejects_tool_outside_selected_skill_allowlist() -> None:
    store = InMemoryAgentStore()
    service = SessionRunService(store)
    session = await service.create_session(user_id="skill-user", title="Skill")
    run = await service.create_run(
        user_id="skill-user",
        session_id=session.session_id,
        request=run_request(),
    )
    auth_context = population_auth_context().model_copy(
        update={"session_id": session.session_id, "run_id": run.run_id}
    )

    with pytest.raises(ModelContractError, match="not allowed by selected skill"):
        await ModelPlanner(
            provider=_SkillCallingProvider(
                skill_id="skill.population-analysis",
                tool_id="governance.resolve_area",
            ),
            context_builder=AgentContextBuilder(
                store=store,
                registry=ToolRegistry.default(),
                skill_registry=RuntimeSkillRegistry((_population_skill(),)),
            ),
            user_id="skill-user",
            auth_context=auth_context,
        ).decide(HarnessState())


def test_for_registry_pins_skill_snapshot_for_the_run() -> None:
    source = RuntimeSkillRegistry((_population_skill(),))
    builder = AgentContextBuilder(
        store=InMemoryAgentStore(),
        registry=ToolRegistry.default(),
        skill_registry=source,
    )

    pinned = builder.for_registry(ToolRegistry.default())
    source.replace((_population_skill(skill_id="skill.changed"),))

    assert [item.skill_id for item in pinned.skill_registry_snapshot()] == [
        "skill.population-analysis"
    ]
    assert [item.skill_id for item in builder.skill_registry_snapshot()] == [
        "skill.changed"
    ]
