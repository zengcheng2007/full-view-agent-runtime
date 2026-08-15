"""Published control-plane seeds used only by the in-memory composition."""

from full_view_agent.domain.capability import ToolCapability, ToolSemanticContract


def population_semantic_contract_v1_1() -> ToolSemanticContract:
    dimensions = {
        "district": ("区县", 4, True),
        "descendant_street": ("下辖街道", 4, True),
        "descendant_community": ("下辖社区", 4, True),
        "street": ("街道", 6, False),
        "community": ("社区", 9, False),
        "grid": ("网格", 12, False),
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
    for dimension, (_label, scope_level, list_is_ranking) in dimensions.items():
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
                }
            )
    return ToolSemanticContract.model_validate(
        {
            "subject": "population",
            "metrics": [
                {
                    "metric_id": "person_count",
                    "label": "人口数",
                    "unit": "人",
                    "value_type": "integer",
                }
            ],
            "dimensions": [
                {
                    "dimension_id": dimension,
                    "label": label,
                    "kind": "administrative_area",
                }
                for dimension, (label, _scope, _ranking) in dimensions.items()
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


def population_tool_v1_1() -> ToolCapability:
    return ToolCapability(
        capability_id="governance.query_population_metrics",
        name="查询人口聚合指标",
        owner="full-information-domain-team",
        version="1.1.0",
        status="published",
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
