from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Literal

from pydantic import Field, model_validator

from full_view_agent.domain.models import ContractModel, ToolResult

FINISH_TOOL_ID = "full_view.finish_answer"
FINISH_TOOL_NAME = "full_view__finish_answer"
REFERENCE_ONLY_SUMMARY = "查询已完成，详细结果请查看数据面板。"
CAPABILITY_SUMMARY = "我可以协助使用当前已授权的治理查询能力。"
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
    claims: list[AnswerClaim] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def validate_claim_shape(self) -> StructuredFinish:
        if self.kind == "claims" and not self.claims:
            raise ValueError("claims finish requires at least one claim")
        if self.kind != "claims" and self.claims:
            raise ValueError("only claims finish may contain claims")
        ids = [claim.claim_id for claim in self.claims]
        if len(ids) != len(set(ids)):
            raise ValueError("claim_id values must be unique")
        return self


FINISH_TOOL_DESCRIPTION = (
    "完成本轮回答。引用查询事实时必须使用 claims，并明确绑定 Result、行、字段、"
    "运算和值；只展示数据面板时使用 reference_only；能力说明、参数澄清、权限拒绝"
    "和执行失败必须分别使用 capability、clarification、denial、failure。"
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
        result.data_result.result_id: result.data_result
        for result in results
        if result.status in {"success", "partial"} and result.data_result is not None
    }
    if finish.kind == "reference_only":
        if not available and not has_reference:
            return ClaimAssessment(False, "reference_result_not_found")
        return ClaimAssessment(True, "reference_only", REFERENCE_ONLY_SUMMARY)
    if finish.kind != "claims":
        return ClaimAssessment(False, "structured_finish_kind_not_allowed")

    rendered: list[str] = []
    for claim in finish.claims:
        result = available.get(claim.result_id)
        if result is None:
            return ClaimAssessment(False, "claim_result_not_found")
        if result.result_fingerprint != claim.result_fingerprint:
            return ClaimAssessment(False, "claim_fingerprint_mismatch")
        if (
            getattr(result, "truncated", False)
            and claim.operation
            in {"sum", "count", "min", "max", "is_min", "is_max", "all_equal"}
        ):
            return ClaimAssessment(False, "claim_truncated_aggregate")

        rows = _claim_collection(result, claim.collection)
        selected = [row for row in rows if _matches(row, claim.row_locator)]
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
        rendered.append(_render_claim(claim, computed))
    return ClaimAssessment(True, "grounded", "\n".join(rendered))


_OPERATION_MISMATCH = object()


def _claim_collection(result: object, collection: str) -> list[dict[str, ClaimScalar]]:
    data = getattr(result, "data", None)
    if data is None:
        return []
    dumped = data.model_dump(mode="python")
    if collection == "root":
        return [dumped]
    rows = dumped.get("rows")
    return rows if isinstance(rows, list) else []


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


def _render_claim(claim: AnswerClaim, value: object) -> str:
    subject = "、".join(str(item) for item in claim.row_locator.values())
    prefix = f"{subject}的" if subject else ""
    rendered_value = _render_value(value)
    if claim.operation == "sum":
        return f"{prefix}{claim.field}合计为{rendered_value}。"
    if claim.operation == "count":
        return f"{prefix}记录数为{rendered_value}。"
    if claim.operation == "min":
        return f"{prefix}{claim.field}最小值为{rendered_value}。"
    if claim.operation == "max":
        return f"{prefix}{claim.field}最大值为{rendered_value}。"
    if claim.operation == "is_min":
        return f"{prefix}{claim.field}为{rendered_value}，且为最小值。"
    if claim.operation == "is_max":
        return f"{prefix}{claim.field}为{rendered_value}，且为最大值。"
    if claim.operation == "all_equal":
        return f"{prefix}{claim.field}全部相同。"
    return f"{prefix}{claim.field}为{rendered_value}。"


def _render_value(value: object) -> str:
    if isinstance(value, Decimal):
        normalized = value.normalize()
        if normalized == normalized.to_integral():
            return str(normalized.quantize(Decimal(1)))
        return format(normalized, "f").rstrip("0").rstrip(".")
    if isinstance(value, bool):
        return "是" if value else "否"
    return str(value)
