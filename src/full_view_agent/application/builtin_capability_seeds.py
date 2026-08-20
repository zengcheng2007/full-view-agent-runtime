"""Published control-plane seeds used only by the in-memory composition."""

from full_view_agent.domain.capability import ToolCapability, ToolSemanticContract


def population_semantic_contract_v1_2() -> ToolSemanticContract:
    dimensions = {
        "district": ("区县", 4, True, ["区县", "城区", "哪个区"]),
        "descendant_street": ("下辖街道", 4, True, ["街道", "镇街"]),
        "descendant_community": ("下辖社区", 4, True, ["社区", "村社"]),
        "street": ("街道", 6, False, ["街道", "镇街"]),
        "community": ("社区", 9, False, ["社区", "村社"]),
        "grid": ("网格", 12, False, ["网格"]),
    }
    operator_outputs = (
        ("list", "table"),
        ("list", "choropleth"),
        ("sum", "table"),
        ("avg", "table"),
        ("min", "table"),
        ("max", "table"),
        ("top", "table"),
        ("bottom", "table"),
        ("rank", "table"),
    )
    completeness = {
        "mode": "complete",
        "statement": "聚合与排名仅基于完整上游区划行集。",
    }
    shapes: list[dict[str, object]] = []
    for dimension, (_label, scope_level, list_is_ranking, _terms) in dimensions.items():
        for operator, output in operator_outputs:
            aggregate = operator in {"sum", "avg", "min", "max"}
            ranking = operator in {"top", "bottom", "rank"} or (
                operator == "list" and list_is_ranking
            )
            result_kind = "aggregate" if aggregate else "ranking" if ranking else "metric"
            result_fields = (
                ["operator", "metric", "value", "area_count", "completeness"]
                if aggregate
                else ["rank", "area_code", "area_name", "person_count"]
                if ranking
                else ["area_code", "area_name", "person_count"]
            )
            shapes.append(
                {
                    "shape_id": f"population_{dimension}_{operator}_{output}",
                    "metric_selection": ["person_count"],
                    "dimension_selection": [dimension],
                    "operator_selection": [operator],
                    "scope_levels": [scope_level],
                    "allowed_filters": ["person_category"],
                    "output_forms": [output],
                    "completeness": completeness,
                    "result_schema_ref": (
                        f"schema://data/population-{result_kind}-table/1.0.0"
                    ),
                    "result_row_fields": result_fields,
                    "result_fingerprint_domain": (
                        f"data-result:population-{result_kind}-table:1.0.0"
                    ),
                    "argument_template": {
                        "query": {
                            "schema_version": "1.1",
                            "metrics": "$semantic.metrics",
                            "operator": "$semantic.operator",
                            "scope": "$semantic.scope",
                            "filters": "$semantic.filters",
                            "group_by": "$semantic.group_by",
                            "order_by": (
                                [
                                    {
                                        "field": "person_count",
                                        "direction": (
                                            "asc" if operator == "bottom" else "desc"
                                        ),
                                    }
                                ]
                                if operator in {"top", "bottom", "rank"}
                                else []
                            ),
                            "limit": "$semantic.limit",
                            "presentation_hint": output,
                        }
                    },
                }
            )
    return ToolSemanticContract.model_validate(
        {
            "subject": "population",
            "intent_terms": [
                "人口",
                "人数",
                "人最多",
                "人最少",
                "人最高",
                "人最低",
            ],
            "excluded_intent_terms": [
                "独居",
                "空巢",
                "年龄",
                "性别",
                "男性",
                "女性",
                "明细",
                "姓名",
                "电话",
            ],
            "metrics": [
                {
                    "metric_id": "person_count",
                    "label": "人口数",
                    "unit": "人",
                    "value_type": "integer",
                    "intent_terms": ["人口", "人数", "人口数", "多少"],
                }
            ],
            "dimensions": [
                {
                    "dimension_id": dimension,
                    "label": label,
                    "kind": "administrative_area",
                    "intent_terms": terms,
                }
                for dimension, (label, _scope, _ranking, terms) in dimensions.items()
            ],
            "filters": [
                {
                    "field": "person_category",
                    "label": "人口类别",
                    "operators": ["eq"],
                    "allowed_values": ["solitary_elderly"],
                }
            ],
            "operators": ["list", "sum", "avg", "min", "max", "top", "bottom", "rank"],
            "operator_intents": [
                {"operator": "list", "terms": ["分布", "列表"]},
                {"operator": "sum", "terms": ["总数", "合计"]},
                {"operator": "avg", "terms": ["平均", "均值"]},
                {"operator": "min", "terms": ["最小值"]},
                {"operator": "max", "terms": ["最大值"]},
                {"operator": "top", "terms": ["最多", "最高"]},
                {"operator": "bottom", "terms": ["最少", "最低"]},
                {"operator": "rank", "terms": ["排名", "排行"]},
            ],
            "sort": {
                "allowed_fields": ["person_count", *dimensions],
                "default_direction": "desc",
                "tie_policy": "include_all",
                "tie_breakers": [],
            },
            "completeness": completeness,
            "output_forms": ["table", "choropleth"],
            "examples": [
                {
                    "question": "哪个街道人口最多？",
                    "operator": "top",
                    "metric": "person_count",
                    "dimension": "descendant_street",
                }
            ],
            "limitations": ["不提供个人明细", "不支持合同 query_shapes 之外的组合"],
            "query_shapes": shapes,
        }
    )


def population_semantic_contract_v1_1() -> ToolSemanticContract:
    """Historical 1.1 contract, preserved without intent-routing metadata."""

    payload = population_semantic_contract_v1_2().model_dump(mode="json")
    payload.pop("intent_terms", None)
    payload.pop("excluded_intent_terms", None)
    payload.pop("operator_intents", None)
    for metric in payload["metrics"]:
        metric.pop("intent_terms", None)
    for dimension in payload["dimensions"]:
        dimension.pop("intent_terms", None)
    for shape in payload["query_shapes"]:
        shape.pop("argument_template", None)
    return ToolSemanticContract.model_validate(payload)


def population_tool_v1_1() -> ToolCapability:
    return ToolCapability(
        capability_id="governance.query_population_metrics",
        name="查询人口聚合指标",
        owner="full-information-domain-team",
        version="1.1.0",
        status="published",
        guidance="查询行政区划级人口聚合指标（总数、均值、极值、排名等），"
        "支持按区县、街道、社区、网格维度聚合，可叠加独居老人筛选。",
        risk_level="low",
        required_permissions=["governance.population.aggregate.read"],
        dataset_ids=["population"],
        connector_ref="governance-geo-qxst",
        http_method="GET",
        resource_path="/geo-qxst/population-metrics",
        result_kind="table",
        data_schema_ref="schema://data/population-metric-table/1.0.0",
        semantic_contract=population_semantic_contract_v1_1(),
        created_by="system-seed",
        updated_by="system-seed",
    )


def population_tool_v1_2() -> ToolCapability:
    return population_tool_v1_1().model_copy(
        update={
            "version": "1.2.0",
            "semantic_contract": population_semantic_contract_v1_2(),
        }
    )


def population_semantic_contract_v1_3() -> ToolSemanticContract:
    """Population contract with unsupported median requests made explicit.

    A request for a *median-ranked* area is not equivalent to a conventional
    rank/list query: the controller must first define the even-count and tie
    policy in a dedicated shape.  Keep that boundary in the published contract
    instead of letting a Runtime fallback reinterpret the question.
    """
    payload = population_semantic_contract_v1_2().model_dump(mode="python")
    payload["excluded_intent_terms"] = [
        *payload["excluded_intent_terms"],
        "中位数",
        "中位",
    ]
    payload["limitations"] = [
        *payload["limitations"],
        "未发布中位数排名能力形态，不将其降级为普通排名或列表",
    ]
    return ToolSemanticContract.model_validate(payload)


def population_tool_v1_3() -> ToolCapability:
    return population_tool_v1_2().model_copy(
        update={
            "version": "1.3.0",
            "semantic_contract": population_semantic_contract_v1_3(),
        }
    )
