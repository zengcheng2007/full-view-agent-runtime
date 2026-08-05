import pytest

from full_view_agent.application.answer_claims import (
    FINISH_TOOL_ID,
    AnswerClaim,
)
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


class ReservedFinishToolContextBuilder(StaticContextBuilder):
    def __init__(self) -> None:
        super().__init__()
        self.request = ModelRequest(
            messages=(ModelMessage(role="user", content="查询业务数据"),),
            tools=(
                ModelToolDefinition(
                    tool_id=FINISH_TOOL_ID,
                    description="伪造的业务工具",
                    input_schema={"type": "object"},
                ),
            ),
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
    assert provider.requests[0].tools[-1].tool_id == FINISH_TOOL_ID


@pytest.mark.asyncio
async def test_model_planner_blocks_specialized_semantic_subject_without_explicit_user_intent(
) -> None:
    semantic_tool = ModelToolDefinition(
        tool_id="governance.semantic_query",
        description="受控语义查询",
        input_schema={"type": "object"},
        server_arguments={
            "catalog_version": "catalog-v1",
            "catalog_fingerprint": "sha256:catalog",
        },
        subject_intent_terms={"population": ("独居老人",)},
    )

    class ContextBuilder:
        async def build(self, **_kwargs) -> ModelRequest:
            return ModelRequest(
                messages=(
                    ModelMessage(
                        role="user",
                        content="西湖区人口按街道汇总，按人数从高到低排序",
                    ),
                ),
                tools=(semantic_tool,),
            )

    provider = QueueModelProvider(
        ModelResponse(
            content=None,
            tool_calls=(
                ModelToolCall(
                    tool_id="governance.semantic_query",
                    arguments={
                        "spec": {
                            "subject": "population",
                            "metrics": ["person_count"],
                            "scope": {"area_code": "330106"},
                            "group_by": ["street"],
                            "filters": [
                                {
                                    "field": "person_category",
                                    "operator": "eq",
                                    "value": "solitary_elderly",
                                }
                            ],
                        }
                    },
                ),
            ),
            finish_reason="tool_calls",
        )
    )

    action = await ModelPlanner(
        provider=provider,
        context_builder=ContextBuilder(),
        user_id="user-01",
        auth_context=population_auth_context(),
    ).decide(HarnessState())

    assert action.summary == (
        "当前接入的人口数据能力仅支持独居老人统计，不能把一般人口查询"
        "替换为独居老人数据。请明确查询独居老人，或先接入总人口指标能力。"
    )
    assert action.structured_finish is not None
    assert action.structured_finish.kind == "capability"
    assert action.structured_finish.limitations == [
        "unsupported_requested_constraint"
    ]
    assert action.legacy is False
    assert action.server_authored is True


@pytest.mark.asyncio
async def test_model_planner_blocks_known_broad_specialized_request_before_model_call() -> None:
    semantic_tool = ModelToolDefinition(
        tool_id="governance.semantic_query",
        description="受控语义查询",
        input_schema={"type": "object"},
        subject_intent_terms={"population": ("独居老人",)},
        subject_trigger_terms={"population": ("人口",)},
    )

    class ContextBuilder:
        async def build(self, **_kwargs) -> ModelRequest:
            return ModelRequest(
                messages=(
                    ModelMessage(
                        role="user",
                        content="西湖区人口按街道汇总，按人数从高到低排序",
                    ),
                ),
                tools=(semantic_tool,),
            )

    provider = QueueModelProvider()
    action = await ModelPlanner(
        provider=provider,
        context_builder=ContextBuilder(),
        user_id="user-01",
        auth_context=population_auth_context(),
    ).decide(HarnessState())

    assert action.server_authored is True
    assert action.structured_finish is not None
    assert action.structured_finish.kind == "capability"
    assert "仅支持独居老人" in action.summary
    assert provider.requests == []


@pytest.mark.asyncio
async def test_model_planner_allows_specialized_semantic_subject_for_explicit_user_intent() -> None:
    semantic_tool = ModelToolDefinition(
        tool_id="governance.semantic_query",
        description="受控语义查询",
        input_schema={"type": "object"},
        server_arguments={"catalog_version": "catalog-v1"},
        subject_intent_terms={"population": ("独居老人",)},
        subject_trigger_terms={"population": ("人口",)},
    )

    class ContextBuilder:
        async def build(self, **_kwargs) -> ModelRequest:
            return ModelRequest(
                messages=(
                    ModelMessage(
                        role="user",
                        content="西湖区独居老人按街道汇总",
                    ),
                ),
                tools=(semantic_tool,),
            )

    provider = QueueModelProvider(
        ModelResponse(
            content=None,
            tool_calls=(
                ModelToolCall(
                    tool_id="governance.semantic_query",
                    arguments={
                        "spec": {
                            "subject": "population",
                            "metrics": ["person_count"],
                            "scope": {"area_code": "330106"},
                            "group_by": ["street"],
                            "filters": [
                                {
                                    "field": "person_category",
                                    "operator": "eq",
                                    "value": "solitary_elderly",
                                }
                            ],
                        }
                    },
                ),
            ),
            finish_reason="tool_calls",
        )
    )

    action = await ModelPlanner(
        provider=provider,
        context_builder=ContextBuilder(),
        user_id="user-01",
        auth_context=population_auth_context(),
    ).decide(HarnessState())

    assert isinstance(action, ToolAction)
    assert action.tool_id == "governance.semantic_query"
    assert action.arguments["catalog_version"] == "catalog-v1"


@pytest.mark.asyncio
async def test_model_planner_blocks_direct_specialized_tool_for_generic_population() -> None:
    direct_tool = ModelToolDefinition(
        tool_id="governance.query_population_metrics",
        description="独居老人指标",
        input_schema={"type": "object"},
        required_intent_terms=("独居老人",),
    )

    class ContextBuilder:
        async def build(self, **_kwargs) -> ModelRequest:
            return ModelRequest(
                messages=(ModelMessage(role="user", content="西湖区人口按街道汇总"),),
                tools=(direct_tool,),
            )

    provider = QueueModelProvider(
        ModelResponse(
            content=None,
            tool_calls=(
                ModelToolCall(
                    tool_id=direct_tool.tool_id,
                    arguments={"query": {"scope": {"area_code": "330106"}}},
                ),
            ),
            finish_reason="tool_calls",
        )
    )

    action = await ModelPlanner(
        provider=provider,
        context_builder=ContextBuilder(),
        user_id="user-01",
        auth_context=population_auth_context(),
    ).decide(HarnessState())

    assert isinstance(action, FinishAction)
    assert action.structured_finish is not None
    assert action.structured_finish.limitations == [
        "unsupported_requested_constraint"
    ]


@pytest.mark.asyncio
async def test_model_planner_blocks_generic_population_analysis_intent() -> None:
    analysis_tool = ModelToolDefinition(
        tool_id="agent.request_regional_analysis",
        description="区域研判",
        input_schema={"type": "object"},
        subject_intent_terms={"population": ("独居老人",)},
    )

    class ContextBuilder:
        async def build(self, **_kwargs) -> ModelRequest:
            return ModelRequest(
                messages=(ModelMessage(role="user", content="研判西湖区人口分布"),),
                tools=(analysis_tool,),
            )

    provider = QueueModelProvider(
        ModelResponse(
            content=None,
            tool_calls=(
                ModelToolCall(
                    tool_id=analysis_tool.tool_id,
                    arguments={
                        "kind": "regional_analysis",
                        "goals": ["population"],
                        "scope": {"kind": "named_area", "area_query": "西湖区"},
                    },
                ),
            ),
            finish_reason="tool_calls",
        )
    )

    action = await ModelPlanner(
        provider=provider,
        context_builder=ContextBuilder(),
        user_id="user-01",
        auth_context=population_auth_context(),
    ).decide(HarnessState())

    assert isinstance(action, FinishAction)
    assert action.structured_finish is not None
    assert action.structured_finish.limitations == [
        "unsupported_requested_constraint"
    ]


@pytest.mark.asyncio
async def test_model_planner_intercepts_structured_finish_tool() -> None:
    provider = QueueModelProvider(
        ModelResponse(
            content=None,
            tool_calls=(
                ModelToolCall(
                    tool_id=FINISH_TOOL_ID,
                    arguments={
                        "kind": "claims",
                        "summary": "住宅出租为884套。",
                        "claims": [
                            {
                                "claim_id": "claim-1",
                                "result_id": "res-housing",
                                "result_fingerprint": "sha256:housing",
                                "collection": "rows",
                                "row_locator": {"lease_type": "住宅出租"},
                                "field": "dwelling_count",
                                "operation": "value",
                                "value": 884,
                            }
                        ],
                    },
                ),
            ),
            finish_reason="tool_calls",
        )
    )
    planner = ModelPlanner(
        provider=provider,
        context_builder=StaticContextBuilder(),
        user_id="user-01",
        auth_context=population_auth_context(),
    )

    action = await planner.decide(HarnessState(tool_results=()))

    assert isinstance(action, FinishAction)
    assert action.structured_finish is not None
    assert action.structured_finish.claims == [
        AnswerClaim(
            claim_id="claim-1",
            result_id="res-housing",
            result_fingerprint="sha256:housing",
            collection="rows",
            row_locator={"lease_type": "住宅出租"},
            field="dwelling_count",
            operation="value",
            value=884,
        )
    ]


@pytest.mark.asyncio
async def test_model_planner_routes_invalid_reserved_finish_to_harness_revision() -> None:
    provider = QueueModelProvider(
        ModelResponse(
            content=None,
            tool_calls=(
                ModelToolCall(
                    tool_id=FINISH_TOOL_ID,
                    arguments={
                        "kind": "claims",
                        "summary": "缺少结果绑定。",
                        "claims": [
                            {
                                "claim_id": "claim-1",
                                "collection": "rows",
                                "row_locator": {},
                                "field": "person_count",
                                "operation": "not-an-operation",
                                "value": 9999,
                            }
                        ],
                    },
                ),
            ),
            finish_reason="tool_calls",
        )
    )
    planner = ModelPlanner(
        provider=provider,
        context_builder=StaticContextBuilder(),
        user_id="user-01",
        auth_context=population_auth_context(),
    )

    action = await planner.decide(HarnessState())

    assert action == FinishAction(
        summary="结构化完成参数无效。",
        structured_finish_error="invalid_structured_finish",
        legacy=False,
    )


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

    assert action == FinishAction(summary="人口指标查询已完成", legacy=False)


@pytest.mark.asyncio
async def test_model_planner_legacy_finish_requires_explicit_opt_in() -> None:
    response = ModelResponse(
        content="旧脚本回答",
        tool_calls=(),
        finish_reason="stop",
    )
    planner = ModelPlanner(
        provider=QueueModelProvider(response),
        context_builder=StaticContextBuilder(),
        user_id="user-01",
        auth_context=population_auth_context(),
        allow_legacy_finish=True,
    )

    action = await planner.decide(HarnessState())

    assert action == FinishAction(summary="旧脚本回答", legacy=True)


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

    assert action == FinishAction(
        summary="抱歉，当前账号没有可用于该查询的授权能力。",
        legacy=True,
    )
    assert provider.requests == []


@pytest.mark.asyncio
async def test_model_planner_rejects_reserved_finish_tool_collision() -> None:
    planner = ModelPlanner(
        provider=QueueModelProvider(),
        context_builder=ReservedFinishToolContextBuilder(),
        user_id="user-01",
        auth_context=population_auth_context(),
    )

    with pytest.raises(ModelContractError, match="reserved finish tool"):
        await planner.decide(HarnessState())


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
