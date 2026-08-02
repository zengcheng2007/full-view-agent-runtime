from dataclasses import replace
from typing import Protocol

from full_view_agent.application.answer_claims import (
    FINISH_TOOL_DESCRIPTION,
    FINISH_TOOL_ID,
    FINISH_TOOL_INPUT_SCHEMA,
    StructuredFinish,
)
from full_view_agent.application.context_builder import AgentContextBuilder
from full_view_agent.application.errors import BudgetExceeded, ModelContractError
from full_view_agent.application.harness import (
    FinishAction,
    HarnessState,
    Planner,
    ToolAction,
)
from full_view_agent.application.model_provider import (
    ModelProvider,
    ModelRequest,
    ModelToolDefinition,
)
from full_view_agent.domain.models import AuthContext


class ContextBuilder(Protocol):
    async def build(
        self,
        *,
        user_id: str,
        auth_context: AuthContext,
        state: HarnessState,
    ) -> ModelRequest: ...


class ModelPlanner:
    """Turns one provider response into one bounded Harness action."""

    def __init__(
        self,
        *,
        provider: ModelProvider,
        context_builder: ContextBuilder,
        user_id: str,
        auth_context: AuthContext,
        max_total_tokens: int = 32_000,
        initial_total_tokens: int = 0,
        allow_legacy_finish: bool = False,
    ) -> None:
        if max_total_tokens <= 0:
            raise ValueError("max_total_tokens must be positive")
        if initial_total_tokens < 0:
            raise ValueError("initial_total_tokens must be non-negative")
        self._provider = provider
        self._context_builder = context_builder
        self._user_id = user_id
        self._auth_context = auth_context
        self._max_total_tokens = max_total_tokens
        self._total_tokens = initial_total_tokens
        self._allow_legacy_finish = allow_legacy_finish

    @property
    def total_tokens(self) -> int:
        return self._total_tokens

    async def decide(self, state: HarnessState) -> ToolAction | FinishAction:
        request = await self._context_builder.build(
            user_id=self._user_id,
            auth_context=self._auth_context,
            state=state,
        )
        if any(tool.tool_id == FINISH_TOOL_ID for tool in request.tools):
            raise ModelContractError("reserved finish tool cannot be a business tool")
        if not request.tools and not state.tool_results:
            return FinishAction(
                summary="抱歉，当前账号没有可用于该查询的授权能力。",
                legacy=True,
            )
        remaining_tokens = self._max_total_tokens - self._total_tokens
        if remaining_tokens <= 0:
            raise BudgetExceeded("model token budget exceeded")
        request = replace(
            request,
            tools=(
                *request.tools,
                ModelToolDefinition(
                    tool_id=FINISH_TOOL_ID,
                    description=FINISH_TOOL_DESCRIPTION,
                    input_schema=FINISH_TOOL_INPUT_SCHEMA,
                ),
            ),
            max_output_tokens=min(
                request.max_output_tokens or remaining_tokens,
                remaining_tokens,
            ),
        )
        response = await self._provider.complete(request)
        self._total_tokens += response.usage.total_tokens
        if self._total_tokens > self._max_total_tokens:
            raise BudgetExceeded("model token budget exceeded")

        if len(response.tool_calls) > 1:
            raise ModelContractError("model returned more than one tool call")
        if response.tool_calls:
            call = response.tool_calls[0]
            if call.tool_id == FINISH_TOOL_ID:
                try:
                    structured_finish = StructuredFinish.model_validate(call.arguments)
                except ValueError:
                    return FinishAction(
                        summary="结构化完成参数无效。",
                        structured_finish_error="invalid_structured_finish",
                        legacy=False,
                    )
                return FinishAction(
                    summary=structured_finish.summary,
                    structured_finish=structured_finish,
                    legacy=False,
                )
            advertised_tools = {tool.tool_id: tool for tool in request.tools}
            advertised = advertised_tools.get(call.tool_id)
            if advertised is None:
                raise ModelContractError("model selected an unavailable tool")
            server_arguments = advertised.server_arguments
            attempted_server_fields = sorted(
                set(call.arguments).intersection(server_arguments)
            )
            if attempted_server_fields:
                raise ModelContractError(
                    "model attempted to set server-owned tool arguments: "
                    + ", ".join(attempted_server_fields)
                )
            return ToolAction(
                tool_id=call.tool_id,
                arguments={**call.arguments, **server_arguments},
            )

        summary = response.content.strip() if response.content is not None else ""
        if not summary:
            raise ModelContractError("model returned no actionable content")
        return FinishAction(summary=summary, legacy=self._allow_legacy_finish)


class ModelPlannerFactory:
    def __init__(
        self,
        *,
        provider: ModelProvider,
        context_builder: AgentContextBuilder,
        max_total_tokens: int = 32_000,
        initial_total_tokens: int = 0,
        allow_legacy_finish: bool = False,
    ) -> None:
        self._provider = provider
        self._context_builder = context_builder
        self._max_total_tokens = max_total_tokens
        self._initial_total_tokens = initial_total_tokens
        self._allow_legacy_finish = allow_legacy_finish

    def create(self, *, user_id: str, auth_context: AuthContext) -> Planner:
        return ModelPlanner(
            provider=self._provider,
            context_builder=self._context_builder,
            user_id=user_id,
            auth_context=auth_context,
            max_total_tokens=self._max_total_tokens,
            initial_total_tokens=self._initial_total_tokens,
            allow_legacy_finish=self._allow_legacy_finish,
        )
