from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from full_view_agent.domain.models import ContractModel, ToolResult

FINISH_TOOL_ID = "full_view.finish_answer"
FINISH_TOOL_NAME = "full_view__finish_answer"
REFERENCE_ONLY_SUMMARY = "查询已完成，详细结果请查看数据面板。"
CAPABILITY_SUMMARY = "我可以协助使用当前已授权的治理查询能力。"
UNSUPPORTED_CONSTRAINT_SUMMARY = (
    "当前能力不支持用户要求的全部筛选条件，无法按原条件精确查询。"
)
CLARIFICATION_SUMMARY = "请补充查询所需的区域、对象或统计口径。"
DENIAL_SUMMARY = "当前查询因权限限制无法完成。"
FAILURE_SUMMARY = "本次查询执行失败，未生成业务结论。"

ClaimScalar = str | int | float | bool | None
ClaimOperation = Literal[
    "value",
    "sum",
    "count",
    "min",
    "max",
    "is_min",
    "is_max",
    "all_equal",
]
ResultLimitation = Literal["unsupported_requested_constraint"]

_LIMITATION_TEXT: dict[ResultLimitation, str] = {
    "unsupported_requested_constraint": (
        "当前能力不支持用户要求的全部筛选条件；以下结果采用已支持的更宽口径，"
        "不等同于原问题的精确结果。"
    )
}


class AnswerClaim(ContractModel):
    claim_id: str = Field(min_length=1, max_length=128)
    result_id: str = Field(min_length=1, max_length=128)
    result_fingerprint: str = Field(min_length=1, max_length=200)
    collection: Literal["rows", "root"]
    row_locator: dict[str, ClaimScalar] = Field(default_factory=dict, max_length=10)
    field: str = Field(min_length=1, max_length=128)
    operation: ClaimOperation
    value: ClaimScalar


class StructuredFinish(ContractModel):
    kind: Literal[
        "claims",
        "reference_only",
        "capability",
        "clarification",
        "denial",
        "failure",
    ]
    summary: str = Field(min_length=1, max_length=10_000)
    limitations: list[ResultLimitation] = Field(default_factory=list, max_length=3)
    claims: list[AnswerClaim] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def validate_claim_shape(self) -> StructuredFinish:
        if self.kind == "claims" and not self.claims:
            raise ValueError("claims finish requires at least one claim")
        if self.kind != "claims" and self.claims:
            raise ValueError("only claims finish may contain claims")
        if self.kind not in {"claims", "capability"} and self.limitations:
            raise ValueError("only claims or capability finish may contain limitations")
        if len(self.limitations) != len(set(self.limitations)):
            raise ValueError("result limitations must be unique")
        ids = [claim.claim_id for claim in self.claims]
        if len(ids) != len(set(ids)):
            raise ValueError("claim_id values must be unique")
        return self


FINISH_TOOL_DESCRIPTION = (
    "完成本轮回答。引用查询事实时必须使用 claims，并明确绑定 Result、行、字段、"
    "运算和值；只展示数据面板时使用 reference_only；能力说明、参数澄清、权限拒绝"
    "和执行失败必须分别使用 capability、clarification、denial、failure。若在用户"
    "要求的筛选条件不受支持时：若不返回数据，应使用 capability 并携带 limitations "
    "unsupported_requested_constraint；若仍返回更宽口径结果，claims 也必须携带该"
    "limitations；不得把更宽口径结果表述成原问题的精确结果。"
)
FINISH_TOOL_INPUT_SCHEMA: dict[str, object] = StructuredFinish.model_json_schema()


@dataclass(frozen=True)
class ClaimAssessment:
    accepted: bool
    reason_code: str
    rendered_summary: str | None = None


def assess_structured_finish(
    finish: StructuredFinish,
    results: tuple[ToolResult, ...],
    *,
    has_reference: bool = False,
) -> ClaimAssessment:
    available = {
        result.data_result.result_id: (result.data_result, result.semantic_lineage)
        for result in results
        if result.status in {"success", "partial"} and result.data_result is not None
    }
    if finish.kind == "reference_only":
        if not available and not has_reference:
            return ClaimAssessment(False, "reference_result_not_found")
        return ClaimAssessment(True, "reference_only", REFERENCE_ONLY_SUMMARY)
    if finish.kind == "capability" and finish.limitations == [
        "unsupported_requested_constraint"
    ]:
        return ClaimAssessment(
            True,
            "unsupported_requested_constraint",
            UNSUPPORTED_CONSTRAINT_SUMMARY,
        )
    if finish.kind != "claims":
        return ClaimAssessment(False, "structured_finish_kind_not_allowed")

    rendered: list[str] = []
    for claim in finish.claims:
        available_result = available.get(claim.result_id)
        if available_result is None:
            return ClaimAssessment(False, "claim_result_not_found")
        result, semantic_lineage = available_result
        if result.result_fingerprint != claim.result_fingerprint:
            return ClaimAssessment(False, "claim_fingerprint_mismatch")
        if (
            getattr(result, "truncated", False)
            and claim.operation
            in {"sum", "count", "min", "max", "is_min", "is_max", "all_equal"}
        ):
            return ClaimAssessment(False, "claim_truncated_aggregate")

        collection = _claim_collection(result, claim.collection)
        rows = collection.rows
        selected = [row for row in rows if _matches(row, claim.row_locator)]
        selected_models = [
            collection.models[index]
            for index, row in enumerate(rows)
            if _matches(row, claim.row_locator)
        ]
        direct = claim.operation in {"value", "is_min", "is_max"}
        if not selected:
            return ClaimAssessment(False, "claim_row_not_found")
        if direct and len(selected) != 1:
            return ClaimAssessment(False, "claim_row_ambiguous")
        if any(claim.field not in row for row in selected):
            return ClaimAssessment(False, "claim_field_not_found")

        computed = _compute_claim(claim, rows, selected)
        if computed is _OPERATION_MISMATCH:
            return ClaimAssessment(False, "claim_operation_mismatch")
        if not _values_equal(computed, claim.value):
            return ClaimAssessment(False, "claim_value_mismatch")
        context_labels = (
            [
                item.display_label
                for item in semantic_lineage.filter_contexts
                if item.display_label is not None
            ]
            if semantic_lineage is not None
            else []
        )
        rendered.append(
            _render_claim(
                claim,
                computed,
                selected_models,
                context_labels=context_labels,
            )
        )
    qualification = [_LIMITATION_TEXT[item] for item in finish.limitations]
    return ClaimAssessment(True, "grounded", "\n".join([*qualification, *rendered]))


_OPERATION_MISMATCH = object()


@dataclass(frozen=True)
class _ClaimCollection:
    rows: list[dict[str, ClaimScalar]]
    models: list[BaseModel]


def _claim_collection(result: object, collection: str) -> _ClaimCollection:
    data = getattr(result, "data", None)
    if not isinstance(data, BaseModel):
        return _ClaimCollection([], [])
    dumped = data.model_dump(mode="python")
    if collection == "root":
        return _ClaimCollection([dumped], [data])
    row_models = getattr(data, "rows", None)
    if not isinstance(row_models, list) or not all(
        isinstance(row, BaseModel) for row in row_models
    ):
        return _ClaimCollection([], [])
    rows = [row.model_dump(mode="python") for row in row_models]
    return _ClaimCollection(rows, row_models)


def _matches(row: dict[str, ClaimScalar], locator: dict[str, ClaimScalar]) -> bool:
    return all(key in row and _values_equal(row[key], value) for key, value in locator.items())


def _compute_claim(
    claim: AnswerClaim,
    rows: list[dict[str, ClaimScalar]],
    selected: list[dict[str, ClaimScalar]],
) -> ClaimScalar | object:
    if claim.operation == "count":
        return len(selected)
    values = [row[claim.field] for row in selected]
    if claim.operation == "value":
        return values[0]
    if claim.operation == "all_equal":
        if len(values) < 2:
            return _OPERATION_MISMATCH
        numeric = _decimals(values)
        if numeric is None or len(set(numeric)) != 1:
            return _OPERATION_MISMATCH
        return True
    numeric = _decimals(values)
    if numeric is None:
        return _OPERATION_MISMATCH
    if claim.operation == "sum":
        return sum(numeric, Decimal(0))
    if claim.operation == "min":
        return min(numeric)
    if claim.operation == "max":
        return max(numeric)
    all_values = _decimals(
        [row[claim.field] for row in rows if claim.field in row]
    )
    if all_values is None or not all_values:
        return _OPERATION_MISMATCH
    selected_value = numeric[0]
    if claim.operation == "is_min" and selected_value != min(all_values):
        return _OPERATION_MISMATCH
    if claim.operation == "is_max" and selected_value != max(all_values):
        return _OPERATION_MISMATCH
    return selected_value


def _decimals(values: list[ClaimScalar]) -> list[Decimal] | None:
    converted: list[Decimal] = []
    for value in values:
        if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
            return None
        converted.append(Decimal(str(value)))
    return converted


def _values_equal(left: object, right: object) -> bool:
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    try:
        if isinstance(left, (int, float, Decimal)) and isinstance(
            right, (int, float, Decimal)
        ):
            return Decimal(str(left)) == Decimal(str(right))
    except InvalidOperation:
        return False
    return type(left) is type(right) and left == right


def _render_claim(
    claim: AnswerClaim,
    value: object,
    selected_models: list[BaseModel],
    *,
    context_labels: list[str] | None = None,
) -> str:
    model = selected_models[0] if selected_models else None
    subject_parts = [
        _render_locator_value(model, field, item)
        for field, item in claim.row_locator.items()
    ]
    subject = "、".join(dict.fromkeys(subject_parts))
    context = "、".join(dict.fromkeys(context_labels or []))
    context_prefix = f"{context}中，" if context else ""
    prefix = f"{context_prefix}{subject}的" if subject else context_prefix
    field_label, unit = _field_display(model, claim.field)
    rendered_value = _render_value(value, unit=unit)
    if claim.operation == "sum":
        return f"{prefix}{field_label}合计为{rendered_value}。"
    if claim.operation == "count":
        return f"{prefix}记录数为{_render_value(value)}。"
    if claim.operation == "min":
        return f"{prefix}{field_label}最小值为{rendered_value}。"
    if claim.operation == "max":
        return f"{prefix}{field_label}最大值为{rendered_value}。"
    if claim.operation == "is_min":
        return f"{prefix}{field_label}为{rendered_value}，且为最小值。"
    if claim.operation == "is_max":
        return f"{prefix}{field_label}为{rendered_value}，且为最大值。"
    if claim.operation == "all_equal":
        return f"{prefix}{field_label}全部相同。"
    return f"{prefix}{field_label}为{rendered_value}。"


def _field_extra(model: BaseModel | None, field: str) -> Mapping[str, object]:
    if model is None:
        return {}
    field_info = type(model).model_fields.get(field)
    if field_info is None or not isinstance(field_info.json_schema_extra, dict):
        return {}
    return field_info.json_schema_extra


def _field_display(model: BaseModel | None, field: str) -> tuple[str, str]:
    if model is None:
        return field, ""
    field_info = type(model).model_fields.get(field)
    if field_info is None:
        return field, ""
    extra = _field_extra(model, field)
    unit = extra.get("unit")
    return field_info.title or field, unit if isinstance(unit, str) else ""


def _render_locator_value(
    model: BaseModel | None,
    field: str,
    value: ClaimScalar,
) -> str:
    if field == "area_code" and model is not None:
        area_name = getattr(model, "area_name", None)
        if isinstance(area_name, str) and area_name:
            return area_name
    labels = _field_extra(model, field).get("value_labels")
    if isinstance(labels, dict):
        label = labels.get(value)
        if isinstance(label, str):
            return label
    return _render_value(value)


def _render_value(value: object, *, unit: str = "") -> str:
    if isinstance(value, bool):
        rendered = "是" if value else "否"
    elif isinstance(value, (int, float, Decimal)):
        normalized = Decimal(str(value)).normalize()
        if normalized == normalized.to_integral():
            rendered = str(normalized.quantize(Decimal(1)))
        else:
            rendered = format(normalized, "f").rstrip("0").rstrip(".")
    else:
        rendered = str(value)
    return f"{rendered}{unit}"
