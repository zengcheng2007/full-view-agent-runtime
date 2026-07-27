import pytest

from full_view_agent.application.errors import BudgetExceeded, ModelContractError
from full_view_agent.application.harness import FinishAction, HarnessState, ToolAction
from full_view_agent.application.model_planner import ModelPlanner
from full_view_agent.application.model_provider import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ModelToolCall,
    ModelToolDefinition,
    ModelUsage,
)

from .test_policy import population_auth_context


class StaticContextBuilder:
    def __init__(self) -> None:
        self.request = ModelRequest(
            messages=(ModelMessage(role="user", content="查询独居老人数量"),),
            tools=(
                ModelToolDefinition(
                    tool_id="governance.query_population_metrics",
                    description="查询人口指标",
                    input_schema={"type": "object"},
                ),
            ),
        )

    async def build(self, **_kwargs) -> ModelRequest:
        return self.request


class EmptyToolContextBuilder(StaticContextBuilder):
    def __init__(self) -> None:
        super().__init__()
        self.request = ModelRequest(
            messages=(ModelMessage(role="user", content="查询业务数据"),),
            tools=(),
        )


class QueueModelProvider:
    def __init__(self, *responses: ModelResponse) -> None:
        self.responses = list(responses)
        self.requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        return self.responses.pop(0)


def tool_response(*, tool_id: str, tokens: int = 10) -> ModelResponse:
    return ModelResponse(
        content=None,
        tool_calls=(
            ModelToolCall(
                tool_id=tool_id,
                arguments={"query": {"metrics": ["person_count"]}},
            ),
        ),
        finish_reason="tool_calls",
        usage=ModelUsage(total_tokens=tokens),
    )


@pytest.mark.asyncio
async def test_model_planner_returns_one_advertised_tool_action() -> None:
    provider = QueueModelProvider(
        tool_response(tool_id="governance.query_population_metrics")
    )
    planner = ModelPlanner(
        provider=provider,
        context_builder=StaticContextBuilder(),
        user_id="user-01",
        auth_context=population_auth_context(),
    )

    action = await planner.decide(HarnessState())

    assert isinstance(action, ToolAction)
    assert action.tool_id == "governance.query_population_metrics"
    assert action.arguments == {"query": {"metrics": ["person_count"]}}
    assert provider.requests[0].messages[0].content == "查询独居老人数量"


@pytest.mark.asyncio
async def test_model_planner_returns_finish_action_for_nonblank_text() -> None:
    provider = QueueModelProvider(
        ModelResponse(
            content="人口指标查询已完成",
            tool_calls=(),
            finish_reason="stop",
        )
    )
    planner = ModelPlanner(
        provider=provider,
        context_builder=StaticContextBuilder(),
        user_id="user-01",
        auth_context=population_auth_context(),
    )

    action = await planner.decide(HarnessState())

    assert action == FinishAction(summary="人口指标查询已完成")


@pytest.mark.asyncio
async def test_model_planner_stops_without_calling_model_when_no_tools_are_authorized() -> None:
    provider = QueueModelProvider()
    planner = ModelPlanner(
        provider=provider,
        context_builder=EmptyToolContextBuilder(),
        user_id="user-01",
        auth_context=population_auth_context(),
    )

    action = await planner.decide(HarnessState())

    assert action == FinishAction(summary="抱歉，当前账号没有可用于该查询的授权能力。")
    assert provider.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        tool_response(tool_id="governance.delete_population_data"),
        ModelResponse(
            content=None,
            tool_calls=(
                ModelToolCall(tool_id="first", arguments={}),
                ModelToolCall(tool_id="second", arguments={}),
            ),
            finish_reason="tool_calls",
        ),
        ModelResponse(content="  ", tool_calls=(), finish_reason="stop"),
    ],
)
async def test_model_planner_rejects_invalid_model_actions(
    response: ModelResponse,
) -> None:
    planner = ModelPlanner(
        provider=QueueModelProvider(response),
        context_builder=StaticContextBuilder(),
        user_id="user-01",
        auth_context=population_auth_context(),
    )

    with pytest.raises(ModelContractError):
        await planner.decide(HarnessState())


@pytest.mark.asyncio
async def test_model_planner_enforces_cumulative_token_budget() -> None:
    provider = QueueModelProvider(
        tool_response(
            tool_id="governance.query_population_metrics",
            tokens=60,
        )
    )
    planner = ModelPlanner(
        provider=provider,
        context_builder=StaticContextBuilder(),
        user_id="user-01",
        auth_context=population_auth_context(),
        max_total_tokens=50,
    )

    with pytest.raises(BudgetExceeded, match="model token budget"):
        await planner.decide(HarnessState())

    assert getattr(provider.requests[0], "max_output_tokens", None) == 50
