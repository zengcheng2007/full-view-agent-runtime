import hashlib
import json
import logging
from collections.abc import Callable
from copy import deepcopy
from dataclasses import replace
from typing import Protocol

from full_view_agent.application.answer_claims import (
    FINISH_TOOL_DESCRIPTION,
    FINISH_TOOL_ID,
    FINISH_TOOL_INPUT_SCHEMA,
    AnswerClaim,
    StructuredFinish,
)
from full_view_agent.application.context_builder import AgentContextBuilder
from full_view_agent.application.errors import (
    BudgetExceeded,
    ModelContractError,
    ModelProviderTimeout,
    ModelProviderUnavailable,
)
from full_view_agent.application.harness import (
    ANALYSIS_INTENT_TOOL_ID,
    AnalysisIntentAction,
    FinishAction,
    HarnessState,
    Planner,
    ToolAction,
)
from full_view_agent.application.model_config_repository import (
    ModelConfigSnapshot,
)
from full_view_agent.application.model_config_repository import (
    RunModelBindingRepository as _RunModelBindingRepository,
)
from full_view_agent.application.model_provider import (
    ModelProvider,
    ModelRequest,
    ModelToolDefinition,
)
from full_view_agent.application.ports import EventPublisher
from full_view_agent.application.runtime_skill_registry import (
    SKILL_INVOKE_TOOL_ID,
    RuntimeSkillRegistry,
)
from full_view_agent.application.tool_registry import ToolRegistry
from full_view_agent.domain.agent_definition import RunAgentReleaseSnapshot
from full_view_agent.domain.analysis_intent import (
    AnalysisIntentV1,
    CurrentAreaScopeIntent,
)
from full_view_agent.domain.capability import ModelConfigWithKey
from full_view_agent.domain.models import (
    AuthContext,
    HousingAreaGroupTable,
    HousingLeaseTypeTable,
    PopulationRankingTable,
)
from full_view_agent.domain.prompt_template import RuntimePromptSnapshot
from full_view_agent.semantic import SEMANTIC_QUERY_TOOL_ID

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
        max_output_tokens: int = 32_000,
        initial_total_tokens: int = 0,
        allow_legacy_finish: bool = False,
        event_publisher: EventPublisher | None = None,
    ) -> None:
        if max_total_tokens <= 0:
            raise ValueError("max_total_tokens must be positive")
        if max_output_tokens <= 0:
            raise ValueError("max_output_tokens must be positive")
        if initial_total_tokens < 0:
            raise ValueError("initial_total_tokens must be non-negative")
        self._provider = provider
        self._context_builder = context_builder
        self._user_id = user_id
        self._auth_context = auth_context
        self._max_total_tokens = max_total_tokens
        self._max_output_tokens = max_output_tokens
        self._total_tokens = initial_total_tokens
        self._allow_legacy_finish = allow_legacy_finish
        self._event_publisher = event_publisher

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
        intent_stop = _specialized_request_intent_stop(request)
        if intent_stop is not None and not state.tool_results:
            return intent_stop
        if any(tool.tool_id == FINISH_TOOL_ID for tool in request.tools):
            raise ModelContractError("reserved finish tool cannot be a business tool")
        if not request.tools and not state.tool_results:
            # Server-authored authorization stop: no model text is being trusted.
            return FinishAction(
                summary="抱歉，当前账号没有可用于该查询的授权能力。",
                legacy=True,
            )
        deterministic_finish = _supported_housing_total_finish(
            request=request,
            state=state,
        )
        if deterministic_finish is None:
            deterministic_finish = _supported_housing_city_street_max_finish(
                request=request,
                state=state,
            )
        if deterministic_finish is None:
            deterministic_finish = _supported_population_city_ranking_finish(
                request=request,
                state=state,
            )
        if deterministic_finish is not None:
            return deterministic_finish
        deterministic_followup = _supported_housing_stock_followup(
            request=request,
            state=state,
        )
        if deterministic_followup is None:
            deterministic_followup = _supported_housing_next_area_followup(
                request=request,
                state=state,
            )
        if deterministic_followup is None:
            deterministic_followup = _supported_housing_city_street_followup(
                request=request,
                state=state,
            )
        if deterministic_followup is None:
            deterministic_followup = _supported_population_city_ranking_followup(
                request=request,
                state=state,
            )
        if deterministic_followup is None:
            deterministic_followup = _supported_event_finish_rate_followup(
                request=request,
                state=state,
            )
        if deterministic_followup is not None:
            return deterministic_followup
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
                self._max_output_tokens,
            ),
        )
        model_turn = state.model_turns + 1
        await self._publish_model_trace(
            event_type="model.requested",
            data=_redacted_model_request_trace(request, model_turn=model_turn),
        )
        try:
            response = await self._provider.complete(request)
        except Exception as exc:
            error_code = getattr(exc, "code", "model_provider_error")
            await self._publish_model_trace(
                event_type="model.failed",
                data={
                    "model_turn": model_turn,
                    "error_code": (
                        error_code
                        if isinstance(error_code, str)
                        else "model_provider_error"
                    ),
                },
            )
            raise
        await self._publish_model_trace(
            event_type="model.responded",
            data=_redacted_model_response_trace(response, model_turn=model_turn),
        )
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
            selected_tool_id = call.tool_id
            raw_arguments = call.arguments
            if call.tool_id == SKILL_INVOKE_TOOL_ID:
                selected_tool_id, raw_arguments, advertised = _unwrap_skill_call(
                    call.arguments,
                    advertised=advertised,
                )
            model_arguments = _remove_unrequested_specialized_filters(
                arguments=raw_arguments,
                advertised=advertised,
                request=request,
            )
            effective_call = replace(
                call,
                tool_id=selected_tool_id,
                arguments=model_arguments,
            )
            intent_stop = _specialized_subject_intent_stop(
                call=effective_call,
                advertised=advertised,
                request=request,
            )
            if intent_stop is not None:
                return intent_stop
            if selected_tool_id == ANALYSIS_INTENT_TOOL_ID:
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
            server_prefixes = {
                f"{name.split('_', maxsplit=1)[0]}_" for name in server_arguments
            }
            attempted_server_fields = sorted(
                name
                for name in model_arguments
                if name in server_arguments
                or any(name.startswith(prefix) for prefix in server_prefixes)
            )
            if attempted_server_fields:
                logger.warning(
                    "ignoring model-supplied server-owned tool arguments: %s",
                    ", ".join(attempted_server_fields),
                )
            return ToolAction(
                tool_id=selected_tool_id,
                arguments={
                    **{
                        name: value
                        for name, value in model_arguments.items()
                        if name not in attempted_server_fields
                    },
                    **server_arguments,
                },
            )

        summary = response.content.strip() if response.content is not None else ""
        if not summary:
            raise ModelContractError("model returned no actionable content")
        return FinishAction(summary=summary, legacy=self._allow_legacy_finish)

    async def _publish_model_trace(
        self,
        *,
        event_type: str,
        data: dict[str, object],
    ) -> None:
        if self._event_publisher is None:
            return
        model_turn = data.get("model_turn")
        if not isinstance(model_turn, int):
            raise ValueError("model trace requires an integer model_turn")
        try:
            await self._event_publisher.publish(
                event_type=event_type,
                session_id=self._auth_context.session_id,
                run_id=self._auth_context.run_id,
                data=data,
                idempotency_key=(
                    f"model-trace:{self._auth_context.run_id}:"
                    f"{model_turn}:{event_type}"
                ),
            )
        except Exception:
            logger.exception("failed to persist redacted model planning trace")


def _redacted_model_request_trace(
    request: ModelRequest,
    *,
    model_turn: int,
) -> dict[str, object]:
    descriptor_surface = [
        {
            "tool_id": tool.tool_id,
            "description": tool.description,
            "input_schema": tool.input_schema,
        }
        for tool in request.tools
    ]
    canonical = json.dumps(
        descriptor_surface,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    catalog_version: str | None = None
    catalog_fingerprint: str | None = None
    for tool in request.tools:
        version = tool.server_arguments.get("catalog_version")
        fingerprint = tool.server_arguments.get("catalog_fingerprint")
        if isinstance(version, str):
            catalog_version = version
        if isinstance(fingerprint, str):
            catalog_fingerprint = fingerprint
    data: dict[str, object] = {
        "model_turn": model_turn,
        "prompt_version": request.prompt_version,
        "message_count": len(request.messages),
        "tool_ids": [tool.tool_id for tool in request.tools],
        "tool_descriptor_fingerprint": f"sha256:{hashlib.sha256(canonical).hexdigest()}",
    }
    if catalog_version is not None:
        data["catalog_version"] = catalog_version
    if catalog_fingerprint is not None:
        data["catalog_fingerprint"] = catalog_fingerprint
    return data


def _redacted_model_response_trace(
    response: object,
    *,
    model_turn: int,
) -> dict[str, object]:
    from full_view_agent.application.model_provider import ModelResponse

    if not isinstance(response, ModelResponse):
        raise TypeError("response must be ModelResponse")
    return {
        "model_turn": model_turn,
        "finish_reason": response.finish_reason,
        "selected_tool_ids": [call.tool_id for call in response.tool_calls],
        "content_present": bool(response.content and response.content.strip()),
        "usage": {
            "prompt_tokens": response.usage.prompt_tokens,
            "completion_tokens": response.usage.completion_tokens,
            "total_tokens": response.usage.total_tokens,
        },
    }


_HOUSING_INTENT_TERMS = ("出租房", "租赁房", "租赁住房")
_HOUSING_TOTAL_TERMS = ("总共", "总数", "一共", "合计", "有多少", "多少出租房")
_HOUSING_TOTAL_EXCLUSION_TERMS = (
    "街道",
    "村社",
    "社区",
    "网格",
    "分布",
    "类型",
    "用途",
    "住宅",
    "商铺",
    "公寓",
    "群租",
    "工业",
    "最多",
    "最少",
    "排名",
    "排行",
    "排序",
    "楼幢",
    "户室",
    "趋势",
    "同比",
    "环比",
)
_STREET_GROUP_TERMS = ("街道", "各街道")
_INTRINSIC_DESCENDING_TERMS = ("从高到低", "降序", "比较多", "最多", "排名", "排行")
_UNSUPPORTED_HOUSING_CONSTRAINT_TERMS = (
    "从低到高",
    "升序",
    "住宅",
    "商铺",
    "公寓",
    "群租",
    "租赁类型",
    "户型",
    "今年",
    "去年",
    "时间范围",
)


def _supported_housing_total_finish(
    *,
    request: ModelRequest,
    state: HarnessState,
) -> FinishAction | None:
    """Finish an exact regional rental total from the complete lease-type table.

    The housing endpoint returns one row per lease type.  A question asking for
    the regional total must therefore use a sum claim; accepting a value claim
    from one row silently changes the requested aggregation grain.
    """

    latest_user_message = _latest_user_message(request)
    if not any(term in latest_user_message for term in _HOUSING_INTENT_TERMS):
        return None
    if not any(term in latest_user_message for term in _HOUSING_TOTAL_TERMS):
        return None
    if any(term in latest_user_message for term in _HOUSING_TOTAL_EXCLUSION_TERMS):
        return None

    candidates = []
    for tool_result in state.tool_results:
        data_result = tool_result.data_result
        data = getattr(data_result, "data", None)
        if (
            tool_result.status not in {"success", "partial"}
            or data_result is None
            or data_result.data_schema_ref
            != "schema://data/housing-lease-type-table/1.0.0"
            or getattr(data_result, "truncated", False)
            or not isinstance(data, HousingLeaseTypeTable)
        ):
            continue
        candidates.append((data_result, data))
    if len(candidates) != 1:
        return None

    result, data = candidates[0]
    total = sum(row.dwelling_count for row in data.rows)
    structured = StructuredFinish(
        kind="claims",
        summary=f"出租房总数为{total}套。",
        claims=[
            AnswerClaim(
                claim_id="housing-total",
                result_id=result.result_id,
                result_fingerprint=result.result_fingerprint,
                collection="rows",
                row_locator={},
                field="dwelling_count",
                operation="sum",
                value=total,
            )
        ],
    )
    return FinishAction(
        summary=structured.summary,
        structured_finish=structured,
        legacy=False,
        server_authored=True,
    )


def _is_city_wide_housing_street_max_request(message: str) -> bool:
    return (
        any(term in message for term in _HOUSING_INTENT_TERMS)
        and any(term in message for term in _STREET_GROUP_TERMS)
        and any(term in message for term in ("全市", "全杭州", "杭州市"))
        and any(term in message for term in ("最多", "排名", "排行", "从高到低"))
        and not any(
            term in message for term in _UNSUPPORTED_HOUSING_CONSTRAINT_TERMS
        )
    )


def _supported_housing_city_street_max_finish(
    *, request: ModelRequest, state: HarnessState
) -> FinishAction | None:
    if not _is_city_wide_housing_street_max_request(_latest_user_message(request)):
        return None
    candidates = []
    for tool_result in state.tool_results:
        data_result = tool_result.data_result
        data = getattr(data_result, "data", None)
        if (
            tool_result.status not in {"success", "partial"}
            or data_result is None
            or data_result.data_schema_ref
            != "schema://data/housing-area-group-table/1.0.0"
            or getattr(data_result, "truncated", False)
            or not isinstance(data, HousingAreaGroupTable)
            or not data.rows
            or any(len(row.area_code) != 9 for row in data.rows)
        ):
            continue
        candidates.append((data_result, data))
    if len(candidates) != 1:
        return None
    result, data = candidates[0]
    winner = max(data.rows, key=lambda row: (row.dwelling_count, row.area_code))
    structured = StructuredFinish(
        kind="claims",
        summary=f"{winner.area_name}的出租房数量为{winner.dwelling_count}套，且为全市最大值。",
        claims=[
            AnswerClaim(
                claim_id="housing-city-street-max",
                result_id=result.result_id,
                result_fingerprint=result.result_fingerprint,
                collection="rows",
                row_locator={"area_name": winner.area_name},
                field="dwelling_count",
                operation="is_max",
                value=winner.dwelling_count,
            )
        ],
    )
    return FinishAction(
        summary=structured.summary,
        structured_finish=structured,
        legacy=False,
        server_authored=True,
    )


def _supported_housing_city_street_followup(
    *, request: ModelRequest, state: HarnessState
) -> ToolAction | None:
    if len(state.tool_results) != 1 or not _is_city_wide_housing_street_max_request(
        _latest_user_message(request)
    ):
        return None
    area_result = state.tool_results[0]
    area_data = getattr(getattr(area_result, "data_result", None), "data", None)
    candidates = getattr(area_data, "candidates", None)
    area_code = getattr(area_data, "resolved_area_code", None)
    if (
        area_result.tool_id != "governance.resolve_area"
        or area_result.status not in {"success", "partial"}
        or not isinstance(area_code, str)
        or len(area_code) != 4
        or not isinstance(candidates, list)
        or len(candidates) != 1
        or getattr(candidates[0], "level", None) != "city"
    ):
        return None
    semantic_tool = _find_advertised_tool(request, SEMANTIC_QUERY_TOOL_ID)
    if semantic_tool is None or not all(
        marker in semantic_tool.description
        for marker in (
            "housing",
            "group_by=['descendant_street']",
            "结果按出租房数量从高到低返回",
        )
    ):
        return None
    return ToolAction(
        tool_id=SEMANTIC_QUERY_TOOL_ID,
        arguments={
            **semantic_tool.server_arguments,
            "spec": {
                "subject": "housing",
                "metrics": ["dwelling_count"],
                "scope": {"area_code": area_code},
                "group_by": ["descendant_street"],
                "filters": [],
                "output": "table",
            },
        },
    )


_POPULATION_CITY_TERMS = ("全市", "全杭州", "杭州市")
_POPULATION_RANKING_TERMS = ("最多", "最少", "排名", "排行", "前十", "最高", "最低")
_POPULATION_SPECIALIZED_TERMS = ("独居", "空巢", "年龄", "性别", "男性", "女性")


def _population_city_max_request(message: str) -> str | None:
    has_population_intent = "人口" in message or any(
        term in message for term in ("人最多", "人最高")
    )
    if (
        not has_population_intent
        or not any(term in message for term in _POPULATION_CITY_TERMS)
        or not any(term in message for term in ("最多", "最高"))
        or any(term in message for term in _POPULATION_SPECIALIZED_TERMS)
    ):
        return None
    if "社区" in message or "村社" in message:
        level = "community"
    elif "街道" in message:
        level = "street"
    elif any(term in message for term in ("区县", "城区", "哪个区")):
        level = "district"
    else:
        return None
    return level


def _supported_population_city_ranking_finish(
    *, request: ModelRequest, state: HarnessState
) -> FinishAction | None:
    level = _population_city_max_request(_latest_user_message(request))
    if level is None:
        return None
    expected_length = {"district": 6, "street": 9, "community": 12}[level]
    candidates = []
    for tool_result in state.tool_results:
        data_result = tool_result.data_result
        data = getattr(data_result, "data", None)
        if (
            tool_result.status not in {"success", "partial"}
            or data_result is None
            or data_result.data_schema_ref
            != "schema://data/population-ranking-table/1.0.0"
            or not isinstance(data, PopulationRankingTable)
            or not data.rows
            or any(len(row.area_code) != expected_length for row in data.rows)
        ):
            continue
        candidates.append((data_result, data))
    if len(candidates) != 1:
        return None
    result, data = candidates[0]
    winner = data.rows[0]
    if winner.rank != 1:
        return None
    structured = StructuredFinish(
        kind="claims",
        summary=(
            f"{winner.area_name}人口数为{winner.person_count}人，"
            "且为本次查询中人口最多的区域。"
        ),
        claims=[
            AnswerClaim(
                claim_id=f"population-city-{level}-rank",
                result_id=result.result_id,
                result_fingerprint=result.result_fingerprint,
                collection="rows",
                row_locator={"area_code": winner.area_code},
                field="rank",
                operation="value",
                value=1,
            ),
            AnswerClaim(
                claim_id=f"population-city-{level}-count",
                result_id=result.result_id,
                result_fingerprint=result.result_fingerprint,
                collection="rows",
                row_locator={"area_code": winner.area_code},
                field="person_count",
                operation="value",
                value=winner.person_count,
            ),
        ],
    )
    return FinishAction(
        summary=structured.summary,
        structured_finish=structured,
        legacy=False,
        server_authored=True,
    )


def _supported_population_city_ranking_followup(
    *, request: ModelRequest, state: HarnessState
) -> ToolAction | None:
    """Route a proven city population ranking to one bounded semantic query."""

    if len(state.tool_results) != 1:
        return None
    message = _latest_user_message(request)
    has_population_intent = "人口" in message or any(
        term in message for term in ("人最多", "人最少", "人最高", "人最低")
    )
    if (
        not has_population_intent
        or not any(term in message for term in _POPULATION_CITY_TERMS)
        or not any(term in message for term in _POPULATION_RANKING_TERMS)
        or any(term in message for term in _POPULATION_SPECIALIZED_TERMS)
    ):
        return None
    if "社区" in message or "村社" in message:
        group_by = "descendant_community"
    elif "街道" in message:
        group_by = "descendant_street"
    elif any(term in message for term in ("区县", "城区", "哪个区")):
        group_by = "district"
    else:
        return None

    area_result = state.tool_results[0]
    area_data = getattr(area_result.data_result, "data", None)
    candidates = getattr(area_data, "candidates", None)
    area_code = getattr(area_data, "resolved_area_code", None)
    if (
        area_result.tool_id != "governance.resolve_area"
        or area_result.status not in {"success", "partial"}
        or not isinstance(area_code, str)
        or len(area_code) != 4
        or not isinstance(candidates, list)
        or len(candidates) != 1
        or getattr(candidates[0], "level", None) != "city"
    ):
        return None

    semantic_tool = _find_advertised_tool(request, SEMANTIC_QUERY_TOOL_ID)
    marker = f"group_by=['{group_by}']"
    if (
        semantic_tool is None
        or "population" not in semantic_tool.description
        or marker not in semantic_tool.description
    ):
        return None
    descending = not any(term in message for term in ("最少", "最低"))
    limit = 10 if "前十" in message else 1
    return ToolAction(
        tool_id=SEMANTIC_QUERY_TOOL_ID,
        arguments={
            **semantic_tool.server_arguments,
            "spec": {
                "subject": "population",
                "metrics": ["person_count"],
                "scope": {"area_code": area_code},
                "group_by": [group_by],
                "filters": [],
                "order_by": [
                    {
                        "field": "person_count",
                        "direction": "desc" if descending else "asc",
                    }
                ],
                "limit": limit,
                "output": "table",
            },
        },
    )


_HOUSING_STOCK_BUILDING_TERMS = ("楼幢", "楼栋")
_HOUSING_STOCK_ROOM_TERMS = ("户室",)
_HOUSING_STOCK_OVERVIEW_TERMS = ("房屋存量总览", "房屋存量概况")
_UNSUPPORTED_HOUSING_STOCK_TERMS = (
    "按街道",
    "按区县",
    "按社区",
    "按网格",
    "各街道",
    "分组",
    "汇总",
    "用途",
    "类型",
    "分类",
    "出租",
    "租赁",
    "分布",
    "排序",
    "排行",
    "排名",
    "从高到低",
    "从低到高",
    "同比",
    "环比",
    "趋势",
    "今年",
    "去年",
    "本月",
    "近一",
    "最近",
    "截至",
    "年内",
    "季度",
    "时间",
    "筛选",
    "仅",
    "只看",
    "住宅",
    "商铺",
    "公寓",
    "大于",
    "小于",
    "以上",
    "以下",
    "分析",
    "研判",
    "综合",
)


def _supported_housing_stock_followup(
    *,
    request: ModelRequest,
    state: HarnessState,
) -> ToolAction | None:
    """Compile the exact catalog-declared stock pair after one area result."""

    if len(state.tool_results) != 1 or state.tool_results[0].status != "success":
        return None
    area_code = _single_resolved_district_code(state)
    if area_code is None:
        return None

    latest_user_message = _latest_user_message(request)
    explicitly_requests_pair = all(
        (
            any(
                term in latest_user_message
                for term in _HOUSING_STOCK_BUILDING_TERMS
            ),
            any(term in latest_user_message for term in _HOUSING_STOCK_ROOM_TERMS),
        )
    )
    explicitly_requests_overview = any(
        term in latest_user_message for term in _HOUSING_STOCK_OVERVIEW_TERMS
    )
    if (
        not (explicitly_requests_pair or explicitly_requests_overview)
        or any(
            term in latest_user_message
            for term in _UNSUPPORTED_HOUSING_STOCK_TERMS
        )
    ):
        return None

    semantic_tool = _find_advertised_tool(request, SEMANTIC_QUERY_TOOL_ID)
    if semantic_tool is None or not all(
        marker in semantic_tool.description
        for marker in (
            "housing",
            "building_count",
            "room_count",
            "group_by=[]",
            "区域房屋存量总览（楼幢总数与户室总数）",
        )
    ):
        return None
    return ToolAction(
        tool_id=SEMANTIC_QUERY_TOOL_ID,
        arguments={
            **semantic_tool.server_arguments,
            "spec": {
                "subject": "housing",
                "metrics": ["building_count", "room_count"],
                "scope": {"area_code": area_code},
                "group_by": [],
                "filters": [],
                "output": "table",
            },
        },
    )


def _supported_housing_next_area_followup(
    *,
    request: ModelRequest,
    state: HarnessState,
) -> ToolAction | None:
    """Compile one proven district-to-street housing request deterministically.

    The production adapter already guarantees ``next_area`` housing rows are
    returned by dwelling count descending.  Once a district has been resolved,
    asking the model to rediscover this exact catalog path can incorrectly end
    the run as unsupported.  Keep this shortcut deliberately narrow: explicit
    housing intent, street grouping, descending/comparison language, one
    unambiguous district result, and an actually advertised semantic Tool.
    """

    if len(state.tool_results) != 1:
        return None
    latest_user_message = _latest_user_message(request)
    if not all(
        (
            any(term in latest_user_message for term in _HOUSING_INTENT_TERMS),
            any(term in latest_user_message for term in _STREET_GROUP_TERMS),
            any(term in latest_user_message for term in _INTRINSIC_DESCENDING_TERMS),
        )
    ) or any(term in latest_user_message for term in _UNSUPPORTED_HOUSING_CONSTRAINT_TERMS):
        return None

    area_result = state.tool_results[0]
    if area_result.tool_id != "governance.resolve_area" or area_result.status not in {
        "success",
        "partial",
    }:
        return None
    data_result = area_result.data_result
    area_data = getattr(data_result, "data", None)
    candidates = getattr(area_data, "candidates", None)
    area_code = getattr(area_data, "resolved_area_code", None)
    if (
        not isinstance(area_code, str)
        or not isinstance(candidates, list)
        or len(candidates) != 1
        or getattr(candidates[0], "level", None) != "district"
    ):
        return None

    semantic_tool = _find_advertised_tool(request, SEMANTIC_QUERY_TOOL_ID)
    if semantic_tool is None or not all(
        marker in semantic_tool.description
        for marker in (
            "housing",
            "group_by=['next_area']",
            "结果按出租房数量从高到低返回",
        )
    ):
        return None
    return ToolAction(
        tool_id=SEMANTIC_QUERY_TOOL_ID,
        arguments={
            **semantic_tool.server_arguments,
            "spec": {
                "subject": "housing",
                "metrics": ["dwelling_count"],
                "scope": {"area_code": area_code},
                "group_by": ["next_area"],
                "filters": [],
                "output": "table",
            },
        },
    )


_EVENT_INTENT_TERMS = ("事件", "网格事件")
_EVENT_LEVEL_TERMS = ("三级", "三个层级", "分层", "市区镇街")
_UNSUPPORTED_EVENT_CONSTRAINT_TERMS = (
    "今年",
    "去年",
    "本月",
    "近一",
    "时间范围",
    "同比",
    "环比",
    "趋势",
    "类型",
    "类别",
    "民生",
    "矛盾",
    "总量",
    "数量",
    "件数",
    "办结数",
    "未办结",
    "低于",
    "高于",
)


def _supported_event_finish_rate_followup(
    *,
    request: ModelRequest,
    state: HarnessState,
) -> ToolAction | None:
    """Compile the exact verified three-level event snapshot after area resolution."""

    if len(state.tool_results) != 1:
        return None
    latest_user_message = _latest_user_message(request)
    explicitly_three_level = any(
        term in latest_user_message for term in _EVENT_LEVEL_TERMS
    ) or all(
        term in latest_user_message for term in ("网格", "社区", "街道")
    )
    if (
        not any(term in latest_user_message for term in _EVENT_INTENT_TERMS)
        or "办结率" not in latest_user_message
        or not explicitly_three_level
        or any(
            term in latest_user_message
            for term in _UNSUPPORTED_EVENT_CONSTRAINT_TERMS
        )
    ):
        return None

    area_code = _single_resolved_district_code(state)
    if area_code is None:
        return None
    semantic_tool = _find_advertised_tool(request, SEMANTIC_QUERY_TOOL_ID)
    if semantic_tool is None or not all(
        marker in semantic_tool.description
        for marker in (
            "event（网格事件指标）",
            "['finish_rate']",
            "按网格、村社、镇街层级返回办结率快照",
        )
    ):
        return None
    return ToolAction(
        tool_id=SEMANTIC_QUERY_TOOL_ID,
        arguments={
            **semantic_tool.server_arguments,
            "spec": {
                "subject": "event",
                "metrics": ["finish_rate"],
                "scope": {"area_code": area_code},
                "group_by": [],
                "filters": [],
                "output": "table",
            },
        },
    )


def _single_resolved_district_code(state: HarnessState) -> str | None:
    if len(state.tool_results) != 1:
        return None
    area_result = state.tool_results[0]
    if area_result.tool_id != "governance.resolve_area" or area_result.status not in {
        "success",
        "partial",
    }:
        return None
    area_data = getattr(area_result.data_result, "data", None)
    candidates = getattr(area_data, "candidates", None)
    area_code = getattr(area_data, "resolved_area_code", None)
    if (
        not isinstance(area_code, str)
        or not isinstance(candidates, list)
        or len(candidates) != 1
        or getattr(candidates[0], "level", None) != "district"
    ):
        return None
    return area_code


def _latest_user_message(request: ModelRequest) -> str:
    return next(
        (
            message.content or ""
            for message in reversed(request.messages)
            if message.role == "user"
        ),
        "",
    )


def _find_advertised_tool(
    request: ModelRequest,
    tool_id: str,
) -> ModelToolDefinition | None:
    for advertised in request.tools:
        if advertised.tool_id == tool_id:
            return advertised
        wrapped = advertised.wrapped_tool_definitions.get(tool_id)
        if wrapped is not None and any(
            tool_id in allowed
            for allowed in advertised.skill_tool_allowlists.values()
        ):
            return wrapped
    return None


def _unwrap_skill_call(
    arguments: dict[str, object],
    *,
    advertised: ModelToolDefinition,
) -> tuple[str, dict[str, object], ModelToolDefinition]:
    skill_id = arguments.get("skill_id")
    tool_id = arguments.get("tool_id")
    tool_arguments = arguments.get("arguments")
    if not isinstance(skill_id, str) or not isinstance(tool_id, str):
        raise ModelContractError("model returned an invalid skill selection")
    if not isinstance(tool_arguments, dict):
        raise ModelContractError("model returned invalid skill tool arguments")
    allowed = advertised.skill_tool_allowlists.get(skill_id)
    if allowed is None:
        raise ModelContractError("model selected an unavailable skill")
    if tool_id not in allowed:
        raise ModelContractError("tool is not allowed by selected skill")
    wrapped = advertised.wrapped_tool_definitions.get(tool_id)
    if wrapped is None:
        raise ModelContractError("selected skill tool is unavailable")
    return tool_id, tool_arguments, wrapped


def _remove_unrequested_specialized_filters(
    *,
    arguments: dict[str, object],
    advertised: ModelToolDefinition,
    request: ModelRequest,
) -> dict[str, object]:
    """Prevent an optional specialist filter from narrowing a broad request."""
    if not advertised.specialized_filter_intent_terms:
        return arguments
    normalized = deepcopy(arguments)
    spec = normalized.get("spec")
    if not isinstance(spec, dict):
        return normalized
    subject = spec.get("subject")
    filters = spec.get("filters")
    if not isinstance(subject, str) or not isinstance(filters, list):
        return normalized
    rules = advertised.specialized_filter_intent_terms.get(subject, {})
    if not rules:
        return normalized
    latest_user_message = next(
        (
            message.content or ""
            for message in reversed(request.messages)
            if message.role == "user"
        ),
        "",
    )
    accepted: list[object] = []
    for item in filters:
        if not isinstance(item, dict):
            accepted.append(item)
            continue
        key = f"{item.get('field')}:{item.get('operator')}:{item.get('value')}"
        required_terms = rules.get(key, ())
        if required_terms and not any(
            term in latest_user_message for term in required_terms
        ):
            logger.warning(
                "removed unrequested specialized filter %s for subject %s",
                key,
                subject,
            )
            continue
        accepted.append(item)
    spec["filters"] = accepted
    return normalized


def _specialized_subject_intent_stop(
    *,
    call: object,
    advertised: ModelToolDefinition,
    request: ModelRequest,
) -> FinishAction | None:
    """Stop silent broad-to-specialized substitutions at the model boundary."""
    if not advertised.subject_intent_terms and not advertised.required_intent_terms:
        return None
    arguments = getattr(call, "arguments", None)
    if not isinstance(arguments, dict):
        return None
    required_terms = advertised.required_intent_terms
    spec = arguments.get("spec")
    if isinstance(spec, dict) and isinstance(spec.get("subject"), str):
        required_terms = advertised.subject_intent_terms.get(
            str(spec["subject"]), required_terms
        )
    goals = arguments.get("goals")
    if isinstance(goals, list):
        required_terms = tuple(
            dict.fromkeys(
                term
                for goal in goals
                if isinstance(goal, str)
                for term in advertised.subject_intent_terms.get(goal, ())
            )
        ) or required_terms
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
    if required_terms == ("独居老人",):
        structured = StructuredFinish(
            kind="capability",
            summary=(
                "当前接入的人口数据能力仅支持独居老人统计，不能把一般人口查询"
                "替换为独居老人数据。请明确查询独居老人，或先接入总人口指标能力。"
            ),
            limitations=["unsupported_requested_constraint"],
        )
        return FinishAction(
            summary=structured.summary,
            structured_finish=structured,
            legacy=False,
            server_authored=True,
        )
    return FinishAction(
        summary=(
            "当前请求没有明确包含该专用能力要求的业务对象，系统未执行查询。"
            "请明确查询对象后重试。"
        ),
        legacy=True,
    )


def _specialized_request_intent_stop(request: ModelRequest) -> FinishAction | None:
    """Reject a known broad request before model finish-type variability matters."""

    latest_user_message = next(
        (
            message.content or ""
            for message in reversed(request.messages)
            if message.role == "user"
        ),
        "",
    )
    governance_power_terms = ("治理力量", "网格力量")
    personal_detail_terms = ("人员", "明细", "姓名", "电话", "联系方式")
    if any(term in latest_user_message for term in governance_power_terms) and any(
        term in latest_user_message for term in personal_detail_terms
    ):
        structured = StructuredFinish(
            kind="capability",
            summary=(
                "当前能力仅支持治理力量分类汇总，不提供人员明细、姓名、电话或"
                "联系方式。"
            ),
            limitations=["unsupported_requested_constraint"],
        )
        return FinishAction(
            summary=structured.summary,
            structured_finish=structured,
            legacy=False,
            server_authored=True,
        )
    for tool in request.tools:
        for subject, trigger_terms in tool.subject_trigger_terms.items():
            required_terms = tool.subject_intent_terms.get(subject, ())
            if not required_terms or not any(
                term in latest_user_message for term in trigger_terms
            ):
                continue
            if any(term in latest_user_message for term in required_terms):
                continue
            if subject == "population" and required_terms == ("独居老人",):
                structured = StructuredFinish(
                    kind="capability",
                    summary=(
                        "当前接入的人口数据能力仅支持独居老人统计，不能把一般人口查询"
                        "替换为独居老人数据。请明确查询独居老人，或先接入总人口指标能力。"
                    ),
                    limitations=["unsupported_requested_constraint"],
                )
                return FinishAction(
                    summary=structured.summary,
                    structured_finish=structured,
                    legacy=False,
                    server_authored=True,
                )
    return None


class ModelPlannerFactory:
    def __init__(
        self,
        *,
        provider: ModelProvider,
        context_builder: AgentContextBuilder,
        max_total_tokens: int = 32_000,
        max_output_tokens: int = 32_000,
        initial_total_tokens: int = 0,
        allow_legacy_finish: bool = False,
        event_publisher: EventPublisher | None = None,
    ) -> None:
        self._provider = provider
        self._context_builder = context_builder
        self._max_total_tokens = max_total_tokens
        self._max_output_tokens = max_output_tokens
        self._initial_total_tokens = initial_total_tokens
        self._allow_legacy_finish = allow_legacy_finish
        self._event_publisher = event_publisher

    def for_registry(self, registry: ToolRegistry) -> "ModelPlannerFactory":
        """Bind every planner created for a Run to its immutable tool surface."""

        return ModelPlannerFactory(
            provider=self._provider,
            context_builder=self._context_builder.for_registry(registry),
            max_total_tokens=self._max_total_tokens,
            max_output_tokens=self._max_output_tokens,
            initial_total_tokens=self._initial_total_tokens,
            allow_legacy_finish=self._allow_legacy_finish,
            event_publisher=self._event_publisher,
        )

    def for_skill_registry(
        self, skill_registry: RuntimeSkillRegistry
    ) -> "ModelPlannerFactory":
        return ModelPlannerFactory(
            provider=self._provider,
            context_builder=self._context_builder.for_registry(
                self._context_builder.registry_snapshot(),
                skill_registry=skill_registry,
            ),
            max_total_tokens=self._max_total_tokens,
            max_output_tokens=self._max_output_tokens,
            initial_total_tokens=self._initial_total_tokens,
            allow_legacy_finish=self._allow_legacy_finish,
            event_publisher=self._event_publisher,
        )

    def with_provider(
        self,
        provider: ModelProvider,
        *,
        max_output_tokens: int | None = None,
    ) -> "ModelPlannerFactory":
        return ModelPlannerFactory(
            provider=provider,
            context_builder=self._context_builder,
            max_total_tokens=self._max_total_tokens,
            max_output_tokens=max_output_tokens or self._max_output_tokens,
            initial_total_tokens=self._initial_total_tokens,
            allow_legacy_finish=self._allow_legacy_finish,
            event_publisher=self._event_publisher,
        )

    def for_prompt_snapshot(
        self, prompt_snapshot: RuntimePromptSnapshot | None
    ) -> "ModelPlannerFactory":
        return ModelPlannerFactory(
            provider=self._provider,
            context_builder=self._context_builder.for_registry(
                self._context_builder.registry_snapshot(),
                prompt_snapshot=prompt_snapshot,
            ),
            max_total_tokens=self._max_total_tokens,
            max_output_tokens=self._max_output_tokens,
            initial_total_tokens=self._initial_total_tokens,
            allow_legacy_finish=self._allow_legacy_finish,
            event_publisher=self._event_publisher,
        )

    def create(self, *, user_id: str, auth_context: AuthContext) -> Planner:
        return ModelPlanner(
            provider=self._provider,
            context_builder=self._context_builder,
            user_id=user_id,
            auth_context=auth_context,
            max_total_tokens=self._max_total_tokens,
            max_output_tokens=self._max_output_tokens,
            initial_total_tokens=self._initial_total_tokens,
            allow_legacy_finish=self._allow_legacy_finish,
            event_publisher=self._event_publisher,
        )


class _RunFailoverModelProvider:
    """Run-pinned ordered provider chain.

    Only transport/provider availability failures advance the chain. Contract
    errors are deliberately surfaced because another model must not hide a
    broken prompt or Tool contract. Once a fallback wins, subsequent turns of
    the same Run stay on it.
    """

    def __init__(
        self,
        candidates: tuple[tuple[ModelProvider, ModelConfigSnapshot], ...],
        *,
        run_id: str,
        repository: _RunModelBindingRepository,
    ) -> None:
        if not candidates:
            raise ValueError("model failover chain requires at least one candidate")
        self._candidates = candidates
        self._active_index = 0
        self._run_id = run_id
        self._repository = repository

    async def complete(self, request: ModelRequest):
        last_error: ModelProviderTimeout | ModelProviderUnavailable | None = None
        for index in range(self._active_index, len(self._candidates)):
            provider, snapshot = self._candidates[index]
            bounded_request = replace(
                request,
                max_output_tokens=min(
                    request.max_output_tokens or snapshot.max_output_tokens,
                    snapshot.max_output_tokens,
                ),
            )
            try:
                response = await provider.complete(bounded_request)
            except (ModelProviderTimeout, ModelProviderUnavailable) as exc:
                last_error = exc
                continue
            if index != self._active_index:
                await self._repository.promote_binding(
                    self._run_id,
                    snapshot,
                )
            self._active_index = index
            return response
        if last_error is not None:
            raise last_error
        raise RuntimeError("model failover chain exhausted without an error")


class ModelConfigResolver(Protocol):
    async def resolve_for_runtime(self) -> ModelConfigWithKey | None:
        """Return the current default model config (for new Runs)."""
        ...

    async def load_by_id(self, config_id: str) -> ModelConfigWithKey | None:
        """Return the config with the given ``config_id``, or None.

        This is used to resume an old Run with its original config, even
        if the runtime default has changed. The config is resolved from
        a secure source (environment, vault) using the config_id — the
        plaintext API key is never persisted.
        """
        ...

    async def capture_snapshot_for_runtime(self) -> ModelConfigSnapshot | None:
        """Capture the currently enabled config as an immutable snapshot.

        The snapshot carries the encrypted key material captured at call
        time, so later key rotation on the source row does not affect
        already-bound Runs. Returns None when no config is enabled or
        the key store does not expose its raw ciphertext.
        """
        ...

    async def capture_snapshot_by_id(
        self, config_id: str, expected_version: int
    ) -> ModelConfigSnapshot | None:
        """Capture one exact public-pool model version for an Agent release."""
        ...

    def materialise_snapshot(
        self, snapshot: ModelConfigSnapshot
    ) -> ModelConfigWithKey:
        """Decrypt ``snapshot`` and return a ``ModelConfigWithKey``.

        The plaintext key is never carried by the snapshot — only the
        ciphertext captured at binding time. The decryption key is a
        process-secret held by the service.
        """
        ...


class _RunAgentReleaseReader(Protocol):
    async def get_run_snapshot(
        self, run_id: str
    ) -> RunAgentReleaseSnapshot | None: ...


class RunBoundModelPlannerFactory:
    """Resolve model configuration once per Run and retain that immutable binding.

    The binding is persisted via a ``ModelConfigRepository`` so that:
    1. A Run keeps its original config even after the runtime default changes.
    2. The binding survives process restart (when the repository is DB-backed).
    3. Only the ``config_id`` is persisted — the plaintext API key is resolved
       at runtime from a secure source using the config_id.
    """

    def __init__(
        self,
        *,
        base_factory: ModelPlannerFactory,
        config_resolver: ModelConfigResolver,
        provider_builder: Callable[[ModelConfigWithKey], ModelProvider],
        config_repository: _RunModelBindingRepository | None = None,
        agent_release_repository: _RunAgentReleaseReader | None = None,
    ) -> None:
        from full_view_agent.application.model_config_repository import (
            InMemoryRunModelBindingRepository,
        )

        self._base_factory = base_factory
        # Preserve the established read-only composition seam used by wiring
        # validation while Run-specific clones are created internally.
        self._context_builder = base_factory._context_builder
        self._config_resolver = config_resolver
        self._provider_builder = provider_builder
        self._config_repository: _RunModelBindingRepository = (
            config_repository or InMemoryRunModelBindingRepository()
        )
        self._agent_release_repository = agent_release_repository
        self._bindings: dict[str, ModelPlannerFactory] = {}

    def create(self, *, user_id: str, auth_context: AuthContext) -> Planner:
        """Compatibility path; orchestrators should call ``for_run`` first.

        This bypasses Run-specific binding and uses the base factory's
        provider. It is retained for backward compatibility but should not
        be used in production — orchestrators must call ``for_run`` to
        ensure the Run is bound to its original config.
        """
        return self._base_factory.create(
            user_id=user_id,
            auth_context=auth_context,
        )

    async def for_run(
        self,
        *,
        run_id: str,
        registry: ToolRegistry,
    ) -> ModelPlannerFactory:
        # In-memory cache: survives within-process resume but not restart.
        existing = self._bindings.get(run_id)
        if existing is not None:
            return existing

        # Persisted binding: survives restart (when repository is DB-backed).
        # Stores only (config_id, config_version); the plaintext API key
        # is never persisted — the snapshot carries the ciphertext
        # captured at binding time.
        config: ModelConfigWithKey | None = None
        candidate_configs: list[tuple[ModelConfigWithKey, ModelConfigSnapshot]] = []
        release_snapshot = (
            await self._agent_release_repository.get_run_snapshot(run_id)
            if self._agent_release_repository is not None
            else None
        )
        binding = await self._config_repository.load_binding(run_id)
        if binding is not None:
            snapshot = await self._config_repository.load_snapshot(
                config_id=binding.config_id,
                config_version=binding.config_version,
            )
            if snapshot is None:
                # Fail closed: a binding with no snapshot means the
                # Run was bound to a version that has since been purged
                # (or the snapshot insert failed). Substituting the
                # current config would silently violate the Run-pinned
                # binding semantic.
                raise RuntimeError(
                    f"run {run_id} is bound to config {binding.config_id}"
                    f" v{binding.config_version} but no snapshot exists;"
                    " refusing to substitute a different config"
                )
            config = self._config_resolver.materialise_snapshot(snapshot)
            candidate_configs.append((config, snapshot))
            if release_snapshot is not None:
                winner_seen = False
                for model_ref in sorted(
                    release_snapshot.model_refs, key=lambda item: item.order
                ):
                    if not winner_seen:
                        winner_seen = (
                            model_ref.model_config_id == binding.config_id
                            and model_ref.config_version == binding.config_version
                        )
                        continue
                    fallback_snapshot = await self._config_repository.load_snapshot(
                        config_id=model_ref.model_config_id,
                        config_version=model_ref.config_version,
                    )
                    if fallback_snapshot is not None:
                        candidate_configs.append(
                            (
                                self._config_resolver.materialise_snapshot(
                                    fallback_snapshot
                                ),
                                fallback_snapshot,
                            )
                        )
        else:
            # New Run: capture a snapshot of the current default and
            # persist the binding. Two distinct "None" paths:
            #
            # 1. ``capture_snapshot_for_runtime`` returns None because
            #    no config is currently enabled. This is a "no config
            #    available" state — the Run proceeds with the base
            #    factory's provider (dev fallback where the operator
            #    injected a provider directly, or the deterministic
            #    planner path). We do NOT wrap this in a binding so
            #    subsequent ``for_run`` calls can still try again if a
            #    config becomes enabled later.
            #
            # 2. ``capture_snapshot_for_runtime`` raises — this is a
            #    capture failure (e.g. the key store cannot materialise
            #    the ciphertext). We must NOT silently fall back to the
            #    base provider; doing so would bypass the Run-pinned
            #    binding semantic.
            try:
                if release_snapshot is not None:
                    ordered_models = sorted(
                        release_snapshot.model_refs, key=lambda item: item.order
                    )
                    if not ordered_models or ordered_models[0].role != "primary":
                        raise RuntimeError("agent release has no primary model")
                    snapshots: list[ModelConfigSnapshot] = []
                    for model_ref in ordered_models:
                        candidate_snapshot = (
                            await self._config_resolver.capture_snapshot_by_id(
                                model_ref.model_config_id,
                                model_ref.config_version,
                            )
                        )
                        if candidate_snapshot is not None:
                            snapshots.append(candidate_snapshot)
                    if not snapshots:
                        raise RuntimeError(
                            "all Agent release model versions are unavailable"
                        )
                    for candidate_snapshot in snapshots:
                        await self._config_repository.store_snapshot(
                            candidate_snapshot
                        )
                    snapshot = snapshots[0]
                else:
                    snapshot = await self._config_resolver.capture_snapshot_for_runtime()
            except Exception as exc:
                raise RuntimeError(
                    f"cannot bind run {run_id} to a model config:"
                    f" capture_snapshot_for_runtime raised {exc!r};"
                    " refusing to fall back to the base provider"
                ) from exc
            if snapshot is not None:
                binding = await self._config_repository.store_binding(
                    run_id, snapshot
                )
                # Use the winner's snapshot — under concurrent inserts
                # the winner may have bound the Run to a different
                # (config_id, version) than ours.
                winner_snapshot = await self._config_repository.load_snapshot(
                    config_id=binding.config_id,
                    config_version=binding.config_version,
                )
                if winner_snapshot is None:  # pragma: no cover - race with delete
                    raise RuntimeError(
                        f"run {run_id} binding points to"
                        f" {binding.config_id} v{binding.config_version}"
                        " but the snapshot row is missing; failing closed"
                    )
                config = self._config_resolver.materialise_snapshot(winner_snapshot)
                candidate_configs.append((config, winner_snapshot))
                if release_snapshot is not None:
                    winner_seen = False
                    for model_ref in sorted(
                        release_snapshot.model_refs, key=lambda item: item.order
                    ):
                        if not winner_seen:
                            winner_seen = (
                                model_ref.model_config_id == binding.config_id
                                and model_ref.config_version == binding.config_version
                            )
                            continue
                        fallback_snapshot = (
                            await self._config_repository.load_snapshot(
                                config_id=model_ref.model_config_id,
                                config_version=model_ref.config_version,
                            )
                        )
                        if fallback_snapshot is not None:
                            candidate_configs.append(
                                (
                                    self._config_resolver.materialise_snapshot(
                                        fallback_snapshot
                                    ),
                                    fallback_snapshot,
                                )
                            )

        factory = self._base_factory
        if config is not None:
            provider: ModelProvider = self._provider_builder(config)
            if len(candidate_configs) > 1:
                provider = _RunFailoverModelProvider(
                    tuple(
                        (
                            self._provider_builder(candidate),
                            candidate_snapshot,
                        )
                        for candidate, candidate_snapshot in candidate_configs
                    ),
                    run_id=run_id,
                    repository=self._config_repository,
                )
            factory = factory.with_provider(
                provider,
                max_output_tokens=config.max_output_tokens,
            )
        bound = factory.for_registry(registry)
        self._bindings[run_id] = bound
        return bound
