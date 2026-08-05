import logging
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
    ANALYSIS_INTENT_TOOL_ID,
    AnalysisIntentAction,
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
from full_view_agent.domain.analysis_intent import (
    AnalysisIntentV1,
    CurrentAreaScopeIntent,
)
from full_view_agent.domain.models import AuthContext

logger = logging.getLogger(__name__)


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

    async def decide(
        self, state: HarnessState
    ) -> ToolAction | FinishAction | AnalysisIntentAction:
        request = await self._context_builder.build(
            user_id=self._user_id,
            auth_context=self._auth_context,
            state=state,
        )
        if any(tool.tool_id == FINISH_TOOL_ID for tool in request.tools):
            raise ModelContractError("reserved finish tool cannot be a business tool")
        if not request.tools and not state.tool_results:
            # Server-authored authorization stop: no model text is being trusted.
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
            intent_stop = _specialized_subject_intent_stop(
                call=call,
                advertised=advertised,
                request=request,
            )
            if intent_stop is not None:
                return intent_stop
            if call.tool_id == ANALYSIS_INTENT_TOOL_ID:
                # This virtual capability is not a Tool. Validate exactly the
                # model-owned payload and never merge server-owned arguments.
                try:
                    intent = AnalysisIntentV1.model_validate(call.arguments)
                except ValueError as exc:
                    raise ModelContractError(
                        "model returned an invalid analysis intent"
                    ) from exc
                if isinstance(intent.scope, CurrentAreaScopeIntent):
                    # Defense in depth: the presentation schema only exposes
                    # named_area, but a provider may ignore it. Reject
                    # current_area at the model boundary until a trusted
                    # TrustedRunScopeProvider exists — before any action is
                    # returned, so no Tool/adapter side effect can follow.
                    raise ModelContractError(
                        "model returned an unsupported current-area analysis intent"
                    )
                return AnalysisIntentAction(intent=intent)
            server_arguments = advertised.server_arguments
            attempted_server_fields = sorted(
                set(call.arguments).intersection(server_arguments)
            )
            if attempted_server_fields:
                logger.warning(
                    "ignoring model-supplied server-owned tool arguments: %s",
                    ", ".join(attempted_server_fields),
                )
            return ToolAction(
                tool_id=call.tool_id,
                arguments={**call.arguments, **server_arguments},
            )

        summary = response.content.strip() if response.content is not None else ""
        if not summary:
            raise ModelContractError("model returned no actionable content")
        return FinishAction(summary=summary, legacy=self._allow_legacy_finish)


def _specialized_subject_intent_stop(
    *,
    call: object,
    advertised: ModelToolDefinition,
    request: ModelRequest,
) -> FinishAction | None:
    """Stop silent broad-to-specialized substitutions at the model boundary."""
    if not advertised.subject_intent_terms:
        return None
    arguments = getattr(call, "arguments", None)
    if not isinstance(arguments, dict):
        return None
    spec = arguments.get("spec")
    if not isinstance(spec, dict):
        return None
    subject = spec.get("subject")
    if not isinstance(subject, str):
        return None
    required_terms = advertised.subject_intent_terms.get(subject, ())
    if not required_terms:
        return None
    latest_user_message = next(
        (
            message.content or ""
            for message in reversed(request.messages)
            if message.role == "user"
        ),
        "",
    )
    if any(term in latest_user_message for term in required_terms):
        return None
    if subject == "population" and required_terms == ("独居老人",):
        return FinishAction(
            summary=(
                "当前接入的人口数据能力仅支持独居老人统计，不能把一般人口查询"
                "替换为独居老人数据。请明确查询独居老人，或先接入总人口指标能力。"
            ),
            legacy=True,
        )
    return FinishAction(
        summary=(
            "当前请求没有明确包含该专用能力要求的业务对象，系统未执行查询。"
            "请明确查询对象后重试。"
        ),
        legacy=True,
    )


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
