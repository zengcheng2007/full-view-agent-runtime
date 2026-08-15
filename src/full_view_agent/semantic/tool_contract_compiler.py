"""Deterministic compiler for one exact versioned Tool semantic contract."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from full_view_agent.domain.capability import (
    SemanticFilterOperator,
    SemanticOperator,
    SemanticOutputForm,
    ToolCapability,
    ToolSemanticCompleteness,
)
from full_view_agent.domain.contract_model import ContractModel


class ToolSemanticQueryRejected(ValueError):
    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


class ToolSemanticQueryFilter(ContractModel):
    field: str
    operator: SemanticFilterOperator
    value: str | int | float | bool | list[str | int | float]


class ToolSemanticQuery(ContractModel):
    operator: SemanticOperator
    metric: str
    dimension: str | None = None
    scope_level: Literal[4, 6, 9, 12, 15]
    filters: tuple[ToolSemanticQueryFilter, ...] = ()
    sort_direction: Literal["asc", "desc"] | None = None
    limit: int = Field(default=200, ge=1, le=1000)
    output_form: SemanticOutputForm = "table"


class CompiledToolSemanticPlan(ContractModel):
    tool_id: str
    tool_version: str
    subject: str
    operator: SemanticOperator
    metric: str
    dimension: str | None
    filters: tuple[ToolSemanticQueryFilter, ...]
    sort_direction: Literal["asc", "desc"] | None
    tie_policy: Literal["include_all", "secondary_sort"]
    tie_breakers: tuple[str, ...]
    completeness: ToolSemanticCompleteness
    limit: int
    output_form: SemanticOutputForm
    shape_id: str
    result_schema_ref: str
    result_row_fields: tuple[str, ...]
    result_fingerprint_domain: str


class ToolSemanticContractCompiler:
    def compile(
        self, tool: ToolCapability, query: ToolSemanticQuery
    ) -> CompiledToolSemanticPlan:
        contract = tool.semantic_contract
        if contract is None:
            raise ToolSemanticQueryRejected(
                "SEMANTIC_CONTRACT_UNAVAILABLE", "Tool version has no semantic contract"
            )
        if query.operator not in contract.operators:
            raise ToolSemanticQueryRejected(
                "SEMANTIC_OPERATOR_UNSUPPORTED",
                f"operator {query.operator} is not supported by Tool@{tool.version}",
            )
        if query.metric not in {metric.metric_id for metric in contract.metrics}:
            raise ToolSemanticQueryRejected(
                "SEMANTIC_METRIC_UNSUPPORTED", "metric is not declared by Tool version"
            )
        dimension_ids = {item.dimension_id for item in contract.dimensions}
        if query.dimension is not None and query.dimension not in dimension_ids:
            raise ToolSemanticQueryRejected(
                "SEMANTIC_DIMENSION_UNSUPPORTED",
                "dimension is not declared by Tool version",
            )
        if query.operator in ("top", "bottom", "rank") and query.dimension is None:
            raise ToolSemanticQueryRejected(
                "SEMANTIC_DIMENSION_REQUIRED",
                f"operator {query.operator} requires a dimension",
            )
        filter_contracts = {item.field: item for item in contract.filters}
        for item in query.filters:
            definition = filter_contracts.get(item.field)
            if definition is None or item.operator not in definition.operators:
                raise ToolSemanticQueryRejected(
                    "SEMANTIC_FILTER_UNSUPPORTED", "filter is not declared by Tool version"
                )
            if definition.allowed_values:
                values = item.value if isinstance(item.value, list) else [item.value]
                if any(value not in definition.allowed_values for value in values):
                    raise ToolSemanticQueryRejected(
                        "SEMANTIC_FILTER_VALUE_UNSUPPORTED",
                        "filter value is not declared by Tool version",
                    )
        if query.output_form not in contract.output_forms:
            raise ToolSemanticQueryRejected(
                "SEMANTIC_OUTPUT_UNSUPPORTED",
                "output form is not declared by Tool version",
            )
        dimensions = (query.dimension,) if query.dimension is not None else ()
        matching_shapes = [
            shape
            for shape in contract.query_shapes
            if shape.metric_selection == (query.metric,)
            and shape.dimension_selection == dimensions
            and query.operator in shape.operator_selection
            and query.scope_level in shape.scope_levels
            and set(item.field for item in query.filters).issubset(
                shape.allowed_filters
            )
            and query.output_form in shape.output_forms
        ]
        if len(matching_shapes) != 1:
            raise ToolSemanticQueryRejected(
                "SEMANTIC_SHAPE_UNSUPPORTED",
                "metric/dimension/operator/scope/filter/output combination is not declared",
            )
        shape = matching_shapes[0]
        direction = query.sort_direction
        if query.operator == "top":
            direction = "desc"
        elif query.operator == "bottom":
            direction = "asc"
        elif query.operator not in ("rank",):
            direction = None
        return CompiledToolSemanticPlan(
            tool_id=tool.capability_id,
            tool_version=tool.version,
            subject=contract.subject,
            operator=query.operator,
            metric=query.metric,
            dimension=query.dimension,
            filters=query.filters,
            sort_direction=direction,
            tie_policy=contract.sort.tie_policy,
            tie_breakers=contract.sort.tie_breakers,
            completeness=shape.completeness,
            limit=query.limit,
            output_form=query.output_form,
            shape_id=shape.shape_id,
            result_schema_ref=shape.result_schema_ref,
            result_row_fields=shape.result_row_fields,
            result_fingerprint_domain=shape.result_fingerprint_domain,
        )
