import pytest

from full_view_agent.application.answer_claims import (
    FINISH_TOOL_ID,
    AnswerClaim,
)
from full_view_agent.application.builtin_capability_seeds import (
    population_semantic_contract_v1_2,
)
from full_view_agent.application.errors import BudgetExceeded, ModelContractError
from full_view_agent.application.harness import (
    DeterministicCompletionValidator,
    FinishAction,
    HarnessState,
    ToolAction,
)
from full_view_agent.application.model_planner import ModelPlanner
from full_view_agent.application.model_provider import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ModelToolCall,
    ModelToolDefinition,
    ModelUsage,
)
from full_view_agent.application.runtime_skill_registry import SKILL_INVOKE_TOOL_ID
from full_view_agent.domain.models import (
    AreaCandidate,
    AreaCandidatesData,
    HousingAreaGroupRow,
    HousingAreaGroupTable,
    PopulationRankingRow,
    PopulationRankingTable,
    ResultDisplayField,
    ResultPresentation,
    SemanticResultLineage,
    TableDataResult,
    ToolResult,
)
from full_view_agent.semantic.catalog import SemanticCatalog
from full_view_agent.semantic.presenter import SemanticToolPresenter

from .test_harness import successful_area_result, successful_housing_result
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


@pytest.mark.asyncio
async def test_model_planner_cannot_bypass_a_failed_area_resolution() -> None:
    class ContextBuilder:
        async def build(self, **_kwargs) -> ModelRequest:
            return ModelRequest(
                messages=(ModelMessage(role="user", content="杭州市哪个街道人口最少"),),
                tools=(
                    ModelToolDefinition(
                        tool_id="governance.semantic_query",
                        description="受控人口查询",
                        input_schema={"type": "object"},
                    ),
                ),
            )

    failed_area = ToolResult(
        tool_call_id="tc-area-failed",
        tool_id="governance.resolve_area",
        tool_version="1.0.0",
        status="failed",
        summary="未找到可查询的授权区划。",
        warnings=["AREA_NOT_FOUND"],
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
                            "operator": "bottom",
                            "metrics": ["person_count"],
                            "scope": {"area_code": "3301"},
                            "group_by": ["descendant_street"],
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
    ).decide(HarnessState(tool_results=(failed_area,)))

    assert isinstance(action, FinishAction)
    assert action.server_authored is True
    assert "区划" in action.summary
    assert provider.requests == []


@pytest.mark.asyncio
async def test_model_planner_routes_average_from_published_contract_without_model() -> None:
    semantic_tool = ModelToolDefinition(
        tool_id="governance.semantic_query",
        description="语义查询",
        input_schema={"type": "object"},
        server_arguments={"catalog_version": "catalog-v1"},
        semantic_contracts=(population_semantic_contract_v1_2(),),
    )

    class ContractContextBuilder:
        async def build(self, **_kwargs) -> ModelRequest:
            return ModelRequest(
                messages=(
                    ModelMessage(
                        role="user", content="杭州市下辖街道的平均人口是多少"
                    ),
                ),
                tools=(semantic_tool,),
            )

    area = successful_area_result().model_copy(deep=True)
    assert area.data_result is not None
    area.data_result = area.data_result.model_copy(
        update={
            "data": AreaCandidatesData(
                candidates=[
                    AreaCandidate(area_code="3301", area_name="杭州市", level="city")
                ],
                ambiguous=False,
                resolved_area_code="3301",
            )
        }
    )
    provider = QueueModelProvider()

    action = await ModelPlanner(
        provider=provider,
        context_builder=ContractContextBuilder(),
        user_id="user-01",
        auth_context=population_auth_context(),
    ).decide(HarnessState(tool_results=(area,)))

    assert action == ToolAction(
        tool_id="governance.semantic_query",
        arguments={
            "catalog_version": "catalog-v1",
            "spec": {
                "schema_version": "s0.1",
                "subject": "population",
                "operator": "avg",
                "metrics": ["person_count"],
                "scope": {"area_code": "3301", "include_descendants": True},
                "group_by": ["descendant_street"],
                "filters": [],
                "order_by": [],
                "limit": 200,
                "time_range": None,
                "output": "table",
            },
        },
    )
    assert provider.requests == []


@pytest.mark.asyncio
async def test_model_planner_finishes_housing_total_with_sum_not_one_lease_type() -> None:
    class ContextBuilder:
        async def build(self, **_kwargs) -> ModelRequest:
            return ModelRequest(
                messages=(ModelMessage(role="user", content="上城区总共有多少出租房"),),
                tools=(
                    ModelToolDefinition(
                        tool_id="governance.semantic_query",
                        description="housing 按租赁类型汇总出租房数量",
                        input_schema={"type": "object"},
                    ),
                ),
            )

    provider = QueueModelProvider()
    result = successful_housing_result()
    action = await ModelPlanner(
        provider=provider,
        context_builder=ContextBuilder(),
        user_id="user-01",
        auth_context=population_auth_context(),
    ).decide(HarnessState(tool_results=(successful_area_result(), result)))

    assert isinstance(action, FinishAction)
    assert action.server_authored is True
    assert action.structured_finish is not None
    assert action.structured_finish.claims == [
        AnswerClaim(
            claim_id="housing-total",
            result_id="res-housing",
            result_fingerprint="sha256:housing",
            collection="rows",
            row_locator={},
            field="dwelling_count",
            operation="sum",
            value=4420,
        )
    ]
    assert provider.requests == []


@pytest.mark.asyncio
async def test_model_planner_routes_city_wide_street_ranking_as_one_bounded_query() -> None:
    semantic_tool = ModelToolDefinition(
        tool_id="governance.semantic_query",
        description=(
            "housing group_by=['descendant_street'] -> 全市街道汇总，"
            "结果按出租房数量从高到低返回"
        ),
        input_schema={"type": "object"},
        server_arguments={"catalog_version": "catalog-v1"},
    )

    class ContextBuilder:
        async def build(self, **_kwargs) -> ModelRequest:
            return ModelRequest(
                messages=(
                    ModelMessage(role="user", content="找出全杭州出租房最多的街道"),
                ),
                tools=(semantic_tool,),
            )

    area = successful_area_result().model_copy(deep=True)
    assert area.data_result is not None
    area.data_result = area.data_result.model_copy(
        update={
            "data": AreaCandidatesData(
                candidates=[
                    AreaCandidate(area_code="3301", area_name="杭州市", level="city")
                ],
                ambiguous=False,
                resolved_area_code="3301",
            )
        }
    )
    provider = QueueModelProvider()
    action = await ModelPlanner(
        provider=provider,
        context_builder=ContextBuilder(),
        user_id="user-01",
        auth_context=population_auth_context(),
    ).decide(HarnessState(tool_results=(area,)))

    assert action == ToolAction(
        tool_id="governance.semantic_query",
        arguments={
            "catalog_version": "catalog-v1",
            "spec": {
                "subject": "housing",
                "metrics": ["dwelling_count"],
                "scope": {"area_code": "3301"},
                "group_by": ["descendant_street"],
                "filters": [],
                "output": "table",
            },
        },
    )
    assert provider.requests == []


@pytest.mark.asyncio
async def test_model_planner_routes_city_population_community_max_deterministically() -> None:
    semantic_tool = ModelToolDefinition(
        tool_id="governance.semantic_query",
        description="语义查询",
        input_schema={"type": "object"},
        server_arguments={"catalog_version": "catalog-v1"},
        semantic_contracts=(population_semantic_contract_v1_2(),),
    )

    class ContextBuilder:
        async def build(self, **_kwargs) -> ModelRequest:
            return ModelRequest(
                messages=(
                    ModelMessage(role="user", content="杭州市哪个社区人最多"),
                ),
                tools=(semantic_tool,),
            )

    area = successful_area_result().model_copy(deep=True)
    assert area.data_result is not None
    area.data_result = area.data_result.model_copy(
        update={
            "data": AreaCandidatesData(
                candidates=[
                    AreaCandidate(area_code="3301", area_name="杭州市", level="city")
                ],
                ambiguous=False,
                resolved_area_code="3301",
            )
        }
    )
    provider = QueueModelProvider()
    action = await ModelPlanner(
        provider=provider,
        context_builder=ContextBuilder(),
        user_id="user-01",
        auth_context=population_auth_context(),
    ).decide(HarnessState(tool_results=(area,)))

    assert action == ToolAction(
        tool_id="governance.semantic_query",
        arguments={
            "catalog_version": "catalog-v1",
            "spec": {
                "schema_version": "s0.1",
                "subject": "population",
                "operator": "top",
                "metrics": ["person_count"],
                "scope": {"area_code": "3301", "include_descendants": True},
                "group_by": ["descendant_community"],
                "filters": [],
                "order_by": [],
                "limit": 1,
                "time_range": None,
                "output": "table",
            },
        },
    )
    assert provider.requests == []


@pytest.mark.asyncio
async def test_model_planner_finishes_city_wide_street_max_from_complete_result() -> None:
    class ContextBuilder:
        async def build(self, **_kwargs) -> ModelRequest:
            return ModelRequest(
                messages=(
                    ModelMessage(role="user", content="找出全杭州出租房最多的街道"),
                ),
                tools=(
                    ModelToolDefinition(
                        tool_id="governance.semantic_query",
                        description="housing group_by=['descendant_street']",
                        input_schema={"type": "object"},
                    ),
                ),
            )

    data = HousingAreaGroupTable(
        rows=[
            HousingAreaGroupRow(
                area_code="330102002", area_name="丁兰街道", dwelling_count=520
            ),
            HousingAreaGroupRow(
                area_code="330106001", area_name="翠苑街道", dwelling_count=486
            ),
        ]
    )
    result = ToolResult(
        tool_call_id="tc-city-streets",
        tool_id="governance.query_housing_metrics",
        tool_version="1.0.0",
        status="success",
        summary="查询成功",
        data_result=TableDataResult(
            result_id="res-city-streets",
            data_schema_ref="schema://data/housing-area-group-table/1.0.0",
            result_fingerprint="sha256:city-streets",
            data=data,
            row_count=2,
            truncated=False,
        ),
    )
    provider = QueueModelProvider()
    action = await ModelPlanner(
        provider=provider,
        context_builder=ContextBuilder(),
        user_id="user-01",
        auth_context=population_auth_context(),
    ).decide(HarnessState(tool_results=(successful_area_result(), result)))

    assert isinstance(action, FinishAction)
    assert action.server_authored is True
    assert action.structured_finish is not None
    assert action.structured_finish.claims == [
        AnswerClaim(
            claim_id="housing-city-street-max",
            result_id="res-city-streets",
            result_fingerprint="sha256:city-streets",
            collection="rows",
            row_locator={"area_name": "丁兰街道"},
            field="dwelling_count",
            operation="is_max",
            value=520,
        )
    ]
    assert provider.requests == []


@pytest.mark.asyncio
async def test_model_planner_finishes_city_population_street_max_from_ranked_result() -> None:
    class ContextBuilder:
        async def build(self, **_kwargs) -> ModelRequest:
            return ModelRequest(
                messages=(ModelMessage(role="user", content="杭州市哪个街道人最多"),),
                tools=(
                    ModelToolDefinition(
                        tool_id="governance.semantic_query",
                        description="population group_by=['descendant_street']",
                        input_schema={"type": "object"},
                    ),
                ),
            )

    data = PopulationRankingTable(
        rows=[
            PopulationRankingRow(
                rank=1,
                area_code="330109113",
                area_name="瓜沥镇",
                person_count=2920,
            ),
            PopulationRankingRow(
                rank=2,
                area_code="330111001",
                area_name="富春街道",
                person_count=1920,
            ),
        ]
    )
    result = ToolResult(
        tool_call_id="tc-population-city-streets",
        tool_id="governance.query_population_metrics",
        tool_version="1.0.0",
        status="success",
        summary="查询成功",
        semantic_lineage=SemanticResultLineage(
            virtual_tool_id="governance.semantic_query",
            virtual_tool_version="1.0.0",
            spec_version="1.0",
            catalog_version="run-contracts",
            subject="population",
            logical_dataset_id="population",
            canonical_tool_id="governance.query_population_metrics",
            canonical_tool_version="1.2.0",
            spec_fingerprint="sha256:spec",
            plan_fingerprint="sha256:plan",
            semantic_contract_fingerprint="sha256:contract",
            semantic_shape_id="population_descendant_street_top",
            semantic_operator="top",
            semantic_completeness="complete",
            semantic_tie_policy="include_all",
            area_code="3301",
            output="table",
        ),
        data_result=TableDataResult(
            result_id="res-population-city-streets",
            data_schema_ref="schema://data/population-ranking-table/1.0.0",
            result_fingerprint="sha256:population-city-streets",
            data=data,
            row_count=2,
            truncated=True,
            presentation=ResultPresentation(
                title="人口分布",
                summary="共2个区划。",
                status_label="查询完成",
                fields=[
                    ResultDisplayField(field="rank", label="排名", role="dimension"),
                    ResultDisplayField(
                        field="area_code", label="区划编码", role="identifier"
                    ),
                    ResultDisplayField(
                        field="area_name", label="区划名称", role="dimension"
                    ),
                    ResultDisplayField(
                        field="person_count", label="人口数量", role="metric", unit="人"
                    ),
                ],
            ),
        ),
    )
    provider = QueueModelProvider()
    action = await ModelPlanner(
        provider=provider,
        context_builder=ContextBuilder(),
        user_id="user-01",
        auth_context=population_auth_context(),
    ).decide(HarnessState(tool_results=(successful_area_result(), result)))

    assert isinstance(action, FinishAction)
    assert action.server_authored is True
    assert action.structured_finish is not None
    assert action.structured_finish.claims == [
        AnswerClaim(
            claim_id="contract-result-1-rank",
            result_id="res-population-city-streets",
            result_fingerprint="sha256:population-city-streets",
            collection="rows",
            row_locator={"area_code": "330109113"},
            field="rank",
            operation="value",
            value=1,
        ),
        AnswerClaim(
            claim_id="contract-result-1-metric",
            result_id="res-population-city-streets",
            result_fingerprint="sha256:population-city-streets",
            collection="rows",
            row_locator={"area_code": "330109113"},
            field="person_count",
            operation="value",
            value=2920,
        ),
    ]
    assessment = await DeterministicCompletionValidator().assess(
        HarnessState(tool_results=(successful_area_result(), result)), action
    )
    assert assessment.status == "accept"
    assert assessment.safe_summary == "瓜沥镇的排名为1。\n瓜沥镇的人口数为2920人。"
    assert provider.requests == []


@pytest.mark.asyncio
async def test_simple_population_ranking_uses_one_model_planning_call_end_to_end() -> None:
    semantic_tool = ModelToolDefinition(
        tool_id="governance.semantic_query",
        description="受控人口语义查询",
        input_schema={"type": "object"},
        server_arguments={"catalog_version": "catalog-v1"},
        semantic_contracts=(population_semantic_contract_v1_2(),),
    )
    resolve_tool = ModelToolDefinition(
        tool_id="governance.resolve_area",
        description="解析授权区划",
        input_schema={"type": "object"},
    )

    class ContextBuilder:
        async def build(self, **_kwargs) -> ModelRequest:
            return ModelRequest(
                messages=(
                    ModelMessage(role="user", content="杭州市哪个街道人口最少"),
                ),
                tools=(resolve_tool, semantic_tool),
            )

    provider = QueueModelProvider(
        ModelResponse(
            content=None,
            tool_calls=(
                ModelToolCall(
                    tool_id="governance.resolve_area",
                    arguments={"area_name": "杭州市"},
                ),
            ),
            finish_reason="tool_calls",
        )
    )
    planner = ModelPlanner(
        provider=provider,
        context_builder=ContextBuilder(),
        user_id="user-01",
        auth_context=population_auth_context(),
    )

    resolve_action = await planner.decide(HarnessState())
    assert resolve_action == ToolAction(
        tool_id="governance.resolve_area",
        arguments={"area_name": "杭州市"},
    )

    area = successful_area_result().model_copy(deep=True)
    assert area.data_result is not None
    area.data_result = area.data_result.model_copy(
        update={
            "data": AreaCandidatesData(
                candidates=[
                    AreaCandidate(area_code="3301", area_name="杭州市", level="city")
                ],
                ambiguous=False,
                resolved_area_code="3301",
            )
        }
    )
    query_action = await planner.decide(HarnessState(tool_results=(area,)))
    assert isinstance(query_action, ToolAction)
    assert query_action.tool_id == "governance.semantic_query"
    assert query_action.arguments["spec"]["operator"] == "bottom"
    assert query_action.arguments["spec"]["group_by"] == ["descendant_street"]

    result = ToolResult(
        tool_call_id="tc-population-bottom",
        tool_id="governance.query_population_metrics",
        tool_version="1.1.0",
        status="success",
        summary="查询成功",
        semantic_lineage=SemanticResultLineage(
            virtual_tool_id="governance.semantic_query",
            virtual_tool_version="1.0.0",
            spec_version="1.0",
            catalog_version="catalog-v1",
            subject="population",
            logical_dataset_id="population",
            canonical_tool_id="governance.query_population_metrics",
            canonical_tool_version="1.1.0",
            spec_fingerprint="sha256:bottom-spec",
            plan_fingerprint="sha256:bottom-plan",
            semantic_contract_fingerprint="sha256:bottom-contract",
            semantic_shape_id="population_descendant_street_bottom",
            semantic_operator="bottom",
            semantic_completeness="complete",
            semantic_tie_policy="include_all",
            area_code="3301",
            output="table",
        ),
        data_result=TableDataResult(
            result_id="res-population-bottom",
            data_schema_ref="schema://data/population-ranking-table/1.0.0",
            result_fingerprint="sha256:population-bottom",
            data=PopulationRankingTable(
                rows=[
                    PopulationRankingRow(
                        rank=1,
                        area_code="330105004",
                        area_name="和睦街道",
                        person_count=2026,
                    )
                ],
                candidate_count=191,
                tie_policy="include_all",
            ),
            row_count=1,
            truncated=True,
            presentation=ResultPresentation(
                title="人口分布",
                summary="已比较191个街道，返回人口最少的1个结果。",
                status_label="查询完成",
                fields=[
                    ResultDisplayField(field="rank", label="排名", role="dimension"),
                    ResultDisplayField(
                        field="area_code", label="区划编码", role="identifier"
                    ),
                    ResultDisplayField(
                        field="area_name", label="区划名称", role="dimension"
                    ),
                    ResultDisplayField(
                        field="person_count", label="人口数量", role="metric", unit="人"
                    ),
                ],
            ),
        ),
    )
    finish = await planner.decide(HarnessState(tool_results=(area, result)))

    assert isinstance(finish, FinishAction)
    assert finish.server_authored is True
    assert "和睦街道" in finish.summary
    assert len(provider.requests) == 1


@pytest.mark.parametrize("wrapped_by_skill", [False, True])
@pytest.mark.parametrize(
    "prompt",
    [
        "查询西湖区楼幢总数和户室总数。",
        "查询西湖区房屋存量总览。",
    ],
)
@pytest.mark.asyncio
async def test_model_planner_routes_verified_housing_stock_after_area_without_model_retry(
    wrapped_by_skill: bool,
    prompt: str,
) -> None:
    semantic_tool = ModelToolDefinition(
        tool_id="governance.semantic_query",
        description=(
            "housing（房屋聚合指标）：指标 "
            "['dwelling_count', 'building_count', 'room_count']；"
            "结果粒度 group_by=[]（不传 group_by） -> "
            "区域房屋存量总览（楼幢总数与户室总数）"
        ),
        input_schema={"type": "object"},
        server_arguments={
            "catalog_version": "catalog-v1",
            "catalog_fingerprint": "sha256:catalog",
        },
    )
    advertised = semantic_tool
    if wrapped_by_skill:
        advertised = ModelToolDefinition(
            tool_id=SKILL_INVOKE_TOOL_ID,
            description="技能入口",
            input_schema={"type": "object"},
            skill_tool_allowlists={"housing-analysis": (semantic_tool.tool_id,)},
            wrapped_tool_definitions={semantic_tool.tool_id: semantic_tool},
        )

    class ContextBuilder:
        async def build(self, **_kwargs) -> ModelRequest:
            return ModelRequest(
                messages=(ModelMessage(role="user", content=prompt),),
                tools=(advertised,),
            )

    provider = QueueModelProvider()
    action = await ModelPlanner(
        provider=provider,
        context_builder=ContextBuilder(),
        user_id="user-01",
        auth_context=population_auth_context(),
    ).decide(HarnessState(tool_results=(successful_area_result(),)))

    assert action == ToolAction(
        tool_id="governance.semantic_query",
        arguments={
            "catalog_version": "catalog-v1",
            "catalog_fingerprint": "sha256:catalog",
            "spec": {
                "subject": "housing",
                "metrics": ["building_count", "room_count"],
                "scope": {"area_code": "330106"},
                "group_by": [],
                "filters": [],
                "output": "table",
            },
        },
    )
    assert provider.requests == []


@pytest.mark.asyncio
async def test_model_planner_routes_stock_from_current_authorized_catalog_description(
) -> None:
    base_auth = population_auth_context()
    housing_auth = base_auth.model_copy(
        update={
            "entitlements": ["governance.housing.aggregate.read"],
            "data_scopes": base_auth.data_scopes.model_copy(
                update={"datasets": ["housing"]}
            ),
        }
    )
    presentation = SemanticToolPresenter(
        catalog=SemanticCatalog.default()
    ).present(auth_context=housing_auth)
    assert presentation is not None
    semantic_tool = ModelToolDefinition(
        tool_id=presentation.tool_id,
        description=presentation.description,
        input_schema=presentation.input_schema,
        server_arguments=presentation.server_arguments,
    )

    class ContextBuilder:
        async def build(self, **_kwargs) -> ModelRequest:
            return ModelRequest(
                messages=(
                    ModelMessage(
                        role="user",
                        content="查询西湖区楼幢总数和户室总数。",
                    ),
                ),
                tools=(semantic_tool,),
            )

    provider = QueueModelProvider()
    action = await ModelPlanner(
        provider=provider,
        context_builder=ContextBuilder(),
        user_id="user-01",
        auth_context=housing_auth,
    ).decide(HarnessState(tool_results=(successful_area_result(),)))

    assert isinstance(action, ToolAction)
    assert action.arguments["spec"] == {
        "subject": "housing",
        "metrics": ["building_count", "room_count"],
        "scope": {"area_code": "330106"},
        "group_by": [],
        "filters": [],
        "output": "table",
    }
    assert provider.requests == []


@pytest.mark.parametrize(
    "prompt",
    [
        "查询西湖区楼幢总数。",
        "查询西湖区户室总数。",
        "查询西湖区楼幢总数和户室总数，按街道汇总。",
        "查询西湖区楼幢总数和户室总数，按用途分类。",
        "查询西湖区今年的楼幢总数和户室总数。",
        "查询西湖区近一年的楼幢总数和户室总数。",
        "查询西湖区仅住宅的楼幢总数和户室总数。",
        "查询西湖区楼幢总数和户室总数，从高到低排序。",
        "查询西湖区出租房分布。",
        "查询西湖区户室用途分类。",
        "综合分析西湖区人口、房屋和事件治理情况。",
    ],
)
@pytest.mark.asyncio
async def test_model_planner_does_not_force_housing_stock_for_broader_intents(
    prompt: str,
) -> None:
    semantic_tool = ModelToolDefinition(
        tool_id="governance.semantic_query",
        description=(
            "housing（房屋聚合指标）：指标 "
            "['dwelling_count', 'building_count', 'room_count']；"
            "结果粒度 group_by=[]（不传 group_by） -> "
            "区域房屋存量总览（楼幢总数与户室总数）"
        ),
        input_schema={"type": "object"},
    )

    class ContextBuilder:
        async def build(self, **_kwargs) -> ModelRequest:
            return ModelRequest(
                messages=(ModelMessage(role="user", content=prompt),),
                tools=(semantic_tool,),
            )

    provider = QueueModelProvider(
        ModelResponse(
            content="需要由模型继续判断。",
            tool_calls=(),
            finish_reason="stop",
        )
    )
    action = await ModelPlanner(
        provider=provider,
        context_builder=ContextBuilder(),
        user_id="user-01",
        auth_context=population_auth_context(),
        allow_legacy_finish=True,
    ).decide(HarnessState(tool_results=(successful_area_result(),)))

    assert isinstance(action, FinishAction)
    assert len(provider.requests) == 1


@pytest.mark.parametrize(
    "tool_results",
    [
        (successful_area_result().model_copy(update={"status": "partial"}),),
        (successful_area_result(), successful_area_result(tool_call_id="tc-2")),
    ],
)
@pytest.mark.asyncio
async def test_model_planner_requires_exactly_one_successful_area_before_stock_route(
    tool_results: tuple[ToolResult, ...],
) -> None:
    semantic_tool = ModelToolDefinition(
        tool_id="governance.semantic_query",
        description=(
            "housing（房屋聚合指标）：指标 "
            "['dwelling_count', 'building_count', 'room_count']；"
            "结果粒度 group_by=[]（不传 group_by） -> "
            "区域房屋存量总览（楼幢总数与户室总数）"
        ),
        input_schema={"type": "object"},
    )

    class ContextBuilder:
        async def build(self, **_kwargs) -> ModelRequest:
            return ModelRequest(
                messages=(
                    ModelMessage(
                        role="user",
                        content="查询西湖区楼幢总数和户室总数。",
                    ),
                ),
                tools=(semantic_tool,),
            )

    provider = QueueModelProvider(
        ModelResponse(
            content="需要由模型继续判断。",
            tool_calls=(),
            finish_reason="stop",
        )
    )
    action = await ModelPlanner(
        provider=provider,
        context_builder=ContextBuilder(),
        user_id="user-01",
        auth_context=population_auth_context(),
        allow_legacy_finish=True,
    ).decide(HarnessState(tool_results=tool_results))

    assert isinstance(action, FinishAction)
    assert len(provider.requests) == 1


@pytest.mark.asyncio
async def test_model_planner_does_not_force_housing_stock_when_catalog_shape_is_hidden(
) -> None:
    class ContextBuilder:
        async def build(self, **_kwargs) -> ModelRequest:
            return ModelRequest(
                messages=(
                    ModelMessage(
                        role="user",
                        content="查询西湖区楼幢总数和户室总数。",
                    ),
                ),
                tools=(
                    ModelToolDefinition(
                        tool_id="governance.semantic_query",
                        description="housing：当前仅声明出租房和户室用途能力",
                        input_schema={"type": "object"},
                    ),
                ),
            )

    provider = QueueModelProvider(
        ModelResponse(
            content="当前能力合同未声明房屋存量形状。",
            tool_calls=(),
            finish_reason="stop",
        )
    )
    action = await ModelPlanner(
        provider=provider,
        context_builder=ContextBuilder(),
        user_id="user-01",
        auth_context=population_auth_context(),
        allow_legacy_finish=True,
    ).decide(HarnessState(tool_results=(successful_area_result(),)))

    assert isinstance(action, FinishAction)
    assert len(provider.requests) == 1


@pytest.mark.parametrize("wrapped_by_skill", [False, True])
@pytest.mark.asyncio
async def test_model_planner_routes_verified_housing_next_area_descending_request_without_model(
    wrapped_by_skill: bool,
) -> None:
    semantic_tool = ModelToolDefinition(
        tool_id="governance.semantic_query",
        description=(
            "housing group_by=['next_area'] 返回直接下级区划，"
            "结果按出租房数量从高到低返回"
        ),
        input_schema={"type": "object"},
        server_arguments={
            "catalog_version": "catalog-v1",
            "catalog_fingerprint": "sha256:catalog",
        },
    )
    advertised = semantic_tool
    if wrapped_by_skill:
        advertised = ModelToolDefinition(
            tool_id=SKILL_INVOKE_TOOL_ID,
            description="技能入口",
            input_schema={"type": "object"},
            skill_tool_allowlists={"housing-analysis": (semantic_tool.tool_id,)},
            wrapped_tool_definitions={semantic_tool.tool_id: semantic_tool},
        )

    class ContextBuilder:
        async def build(self, **_kwargs) -> ModelRequest:
            return ModelRequest(
                messages=(
                    ModelMessage(
                        role="user",
                        content="西湖区哪些街道出租房比较多？按街道汇总并从高到低排序。",
                    ),
                ),
                tools=(advertised,),
            )

    provider = QueueModelProvider()
    action = await ModelPlanner(
        provider=provider,
        context_builder=ContextBuilder(),
        user_id="user-01",
        auth_context=population_auth_context(),
    ).decide(HarnessState(tool_results=(successful_area_result(),)))

    assert action == ToolAction(
        tool_id="governance.semantic_query",
        arguments={
            "catalog_version": "catalog-v1",
            "catalog_fingerprint": "sha256:catalog",
            "spec": {
                "subject": "housing",
                "metrics": ["dwelling_count"],
                "scope": {"area_code": "330106"},
                "group_by": ["next_area"],
                "filters": [],
                "output": "table",
            },
        },
    )
    assert provider.requests == []


@pytest.mark.parametrize(
    ("prompt", "description"),
    [
        (
            "西湖区出租房按街道从低到高排序。",
            "housing group_by=['next_area'] 结果按出租房数量从高到低返回",
        ),
        (
            "西湖区出租房按租赁类型汇总并从高到低排序。",
            "housing group_by=['next_area'] 结果按出租房数量从高到低返回",
        ),
        (
            "西湖区各街道住宅出租房从高到低排序。",
            "housing group_by=['next_area'] 结果按出租房数量从高到低返回",
        ),
        (
            "西湖区哪些街道出租房比较多？按街道汇总并从高到低排序。",
            "housing 仅支持按租赁类型汇总，group_by=['next_area'] 未开启",
        ),
    ],
)
@pytest.mark.asyncio
async def test_model_planner_does_not_force_housing_next_area_outside_proven_contract(
    prompt: str,
    description: str,
) -> None:
    semantic_tool = ModelToolDefinition(
        tool_id="governance.semantic_query",
        description=description,
        input_schema={"type": "object"},
    )

    class ContextBuilder:
        async def build(self, **_kwargs) -> ModelRequest:
            return ModelRequest(
                messages=(ModelMessage(role="user", content=prompt),),
                tools=(semantic_tool,),
            )

    provider = QueueModelProvider(
        ModelResponse(
            content="需要按当前能力边界进一步判断。",
            tool_calls=(),
            finish_reason="stop",
        )
    )
    action = await ModelPlanner(
        provider=provider,
        context_builder=ContextBuilder(),
        user_id="user-01",
        auth_context=population_auth_context(),
        allow_legacy_finish=True,
    ).decide(HarnessState(tool_results=(successful_area_result(),)))

    assert isinstance(action, FinishAction)
    assert len(provider.requests) == 1


@pytest.mark.parametrize("wrapped_by_skill", [False, True])
@pytest.mark.asyncio
async def test_model_planner_routes_verified_event_finish_rate_without_second_model(
    wrapped_by_skill: bool,
) -> None:
    semantic_tool = ModelToolDefinition(
        tool_id="governance.semantic_query",
        description=(
            "event（网格事件指标）：指标 ['finish_rate']；group_by 无；"
            "结果粒度 group_by=[]（不传 group_by） -> "
            "按网格、村社、镇街层级返回办结率快照"
        ),
        input_schema={"type": "object"},
        server_arguments={
            "catalog_version": "catalog-v1",
            "catalog_fingerprint": "sha256:catalog",
        },
    )
    advertised = semantic_tool
    if wrapped_by_skill:
        advertised = ModelToolDefinition(
            tool_id=SKILL_INVOKE_TOOL_ID,
            description="技能入口",
            input_schema={"type": "object"},
            skill_tool_allowlists={"event-analysis": (semantic_tool.tool_id,)},
            wrapped_tool_definitions={semantic_tool.tool_id: semantic_tool},
        )

    class ContextBuilder:
        async def build(self, **_kwargs) -> ModelRequest:
            return ModelRequest(
                messages=(
                    ModelMessage(
                        role="user",
                        content="看一下西湖区事件治理办结率，按网格、社区、街道三个层级说明。",
                    ),
                ),
                tools=(advertised,),
            )

    provider = QueueModelProvider()
    action = await ModelPlanner(
        provider=provider,
        context_builder=ContextBuilder(),
        user_id="user-01",
        auth_context=population_auth_context(),
    ).decide(HarnessState(tool_results=(successful_area_result(),)))

    assert action == ToolAction(
        tool_id="governance.semantic_query",
        arguments={
            "catalog_version": "catalog-v1",
            "catalog_fingerprint": "sha256:catalog",
            "spec": {
                "subject": "event",
                "metrics": ["finish_rate"],
                "scope": {"area_code": "330106"},
                "group_by": [],
                "filters": [],
                "output": "table",
            },
        },
    )
    assert provider.requests == []


@pytest.mark.parametrize(
    "prompt",
    [
        "看一下西湖区今年事件办结率，按三个层级说明。",
        "看一下西湖区民生类型事件办结率，按三个层级说明。",
        "看一下西湖区事件数量和办结率，按三个层级说明。",
        "看一下西湖区事件办结率。",
    ],
)
@pytest.mark.asyncio
async def test_model_planner_does_not_force_event_route_with_unsupported_constraints(
    prompt: str,
) -> None:
    semantic_tool = ModelToolDefinition(
        tool_id="governance.semantic_query",
        description=(
            "event（网格事件指标）：指标 ['finish_rate']；"
            "按网格、村社、镇街层级返回办结率快照"
        ),
        input_schema={"type": "object"},
    )

    class ContextBuilder:
        async def build(self, **_kwargs) -> ModelRequest:
            return ModelRequest(
                messages=(ModelMessage(role="user", content=prompt),),
                tools=(semantic_tool,),
            )

    provider = QueueModelProvider(
        ModelResponse(
            content="需要按当前能力边界进一步判断。",
            tool_calls=(),
            finish_reason="stop",
        )
    )
    action = await ModelPlanner(
        provider=provider,
        context_builder=ContextBuilder(),
        user_id="user-01",
        auth_context=population_auth_context(),
        allow_legacy_finish=True,
    ).decide(HarnessState(tool_results=(successful_area_result(),)))

    assert isinstance(action, FinishAction)
    assert len(provider.requests) == 1


@pytest.mark.asyncio
async def test_model_planner_does_not_force_event_route_when_contract_is_not_visible() -> None:
    class ContextBuilder:
        async def build(self, **_kwargs) -> ModelRequest:
            return ModelRequest(
                messages=(
                    ModelMessage(
                        role="user",
                        content="看一下西湖区事件治理办结率，按三个层级说明。",
                    ),
                ),
                tools=(
                    ModelToolDefinition(
                        tool_id="governance.semantic_query",
                        description="当前未声明 event finish_rate 三层快照能力",
                        input_schema={"type": "object"},
                    ),
                ),
            )

    provider = QueueModelProvider(
        ModelResponse(
            content="当前能力合同不可见。",
            tool_calls=(),
            finish_reason="stop",
        )
    )
    action = await ModelPlanner(
        provider=provider,
        context_builder=ContextBuilder(),
        user_id="user-01",
        auth_context=population_auth_context(),
        allow_legacy_finish=True,
    ).decide(HarnessState(tool_results=(successful_area_result(),)))

    assert isinstance(action, FinishAction)
    assert len(provider.requests) == 1


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
@pytest.mark.parametrize(
    "message", ["查询西湖区治理力量汇总", "西湖区网格力量构成"]
)
async def test_model_planner_routes_governance_power_aggregate_expressions(
    message: str,
) -> None:
    presentation = SemanticToolPresenter(
        catalog=SemanticCatalog.default()
    ).present(
        auth_context=population_auth_context().model_copy(
            update={
                "entitlements": ["governance.power.aggregate.read"],
                "data_scopes": population_auth_context().data_scopes.model_copy(
                    update={"datasets": ["governance_power"]}
                ),
            }
        )
    )
    assert presentation is not None

    class ContextBuilder:
        async def build(self, **_kwargs) -> ModelRequest:
            return ModelRequest(
                messages=(ModelMessage(role="user", content=message),),
                tools=(
                    ModelToolDefinition(
                        tool_id=presentation.tool_id,
                        description=presentation.description,
                        input_schema=presentation.input_schema,
                        server_arguments=presentation.server_arguments,
                        subject_intent_terms=presentation.subject_intent_terms,
                        subject_trigger_terms=presentation.subject_trigger_terms,
                    ),
                ),
            )

    provider = QueueModelProvider(
        ModelResponse(
            content=None,
            tool_calls=(
                ModelToolCall(
                    tool_id="governance.semantic_query",
                    arguments={
                        "spec": {
                            "subject": "governance_power",
                            "metrics": ["governance_power_count"],
                            "scope": {"area_code": "330106"},
                            "group_by": [],
                            "filters": [],
                            "output": "table",
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
    assert action.arguments["spec"]["subject"] == "governance_power"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message", [
        "查询西湖区治理力量人员明细",
        "给我网格力量人员姓名和电话",
    ],
)
async def test_model_planner_rejects_governance_power_personal_details(
    message: str,
) -> None:
    class ContextBuilder:
        async def build(self, **_kwargs) -> ModelRequest:
            return ModelRequest(
                messages=(ModelMessage(role="user", content=message),),
                tools=(
                    ModelToolDefinition(
                        tool_id="governance.semantic_query",
                        description="受控语义查询",
                        input_schema={"type": "object"},
                    ),
                ),
            )

    provider = QueueModelProvider()
    action = await ModelPlanner(
        provider=provider,
        context_builder=ContextBuilder(),
        user_id="user-01",
        auth_context=population_auth_context(),
    ).decide(HarnessState())

    assert isinstance(action, FinishAction)
    assert action.server_authored is True
    assert action.structured_finish is not None
    assert action.structured_finish.limitations == [
        "unsupported_requested_constraint"
    ]
    assert "仅支持治理力量分类汇总" in action.summary
    assert provider.requests == []


@pytest.mark.asyncio
async def test_model_planner_removes_specialized_filter_not_requested_by_user() -> None:
    semantic_tool = ModelToolDefinition(
        tool_id="governance.semantic_query",
        description="人口聚合查询",
        input_schema={"type": "object"},
        specialized_filter_intent_terms={
            "population": {
                "person_category:eq:solitary_elderly": ("独居老人",),
            }
        },
    )

    class ContextBuilder:
        async def build(self, **_kwargs) -> ModelRequest:
            return ModelRequest(
                messages=(ModelMessage(role="user", content="西湖区人口按街道汇总"),),
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
    assert action.arguments["spec"]["filters"] == []


@pytest.mark.asyncio
async def test_model_planner_keeps_specialized_filter_when_explicitly_requested() -> None:
    semantic_tool = ModelToolDefinition(
        tool_id="governance.semantic_query",
        description="人口聚合查询",
        input_schema={"type": "object"},
        specialized_filter_intent_terms={
            "population": {
                "person_category:eq:solitary_elderly": ("独居老人",),
            }
        },
    )

    class ContextBuilder:
        async def build(self, **_kwargs) -> ModelRequest:
            return ModelRequest(
                messages=(ModelMessage(role="user", content="西湖区独居老人按街道汇总"),),
                tools=(semantic_tool,),
            )

    specialized_filter = {
        "field": "person_category",
        "operator": "eq",
        "value": "solitary_elderly",
    }
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
                            "filters": [specialized_filter],
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
    assert action.arguments["spec"]["filters"] == [specialized_filter]


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


@pytest.mark.asyncio
async def test_model_planner_caps_single_request_output_independently_of_run_budget(
) -> None:
    provider = QueueModelProvider(
        tool_response(tool_id="governance.query_population_metrics")
    )
    planner = ModelPlanner(
        provider=provider,
        context_builder=StaticContextBuilder(),
        user_id="user-01",
        auth_context=population_auth_context(),
        max_total_tokens=2_147_483_647,
        max_output_tokens=131_072,
    )

    await planner.decide(HarnessState())

    assert provider.requests[0].max_output_tokens == 131_072
