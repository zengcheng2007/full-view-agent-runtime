from __future__ import annotations

from collections.abc import Sequence
from typing import cast

from pydantic import BaseModel

from full_view_agent.application.answer_claims import (
    AnswerClaim,
    ClaimScalar,
    StructuredFinish,
)
from full_view_agent.domain.models import ResultDisplayField, TableDataResult, ToolResult

type ClaimValue = str | int | float | bool | None


class ContractResultFinisher:
    """Build a grounded conclusion from an exact semantic result contract.

    Business subjects and metric names intentionally come from persisted result
    lineage and presentation metadata.  This component does not own a catalog of
    population, housing, event, or other domain-specific phrases.
    """

    def finish(self, results: Sequence[ToolResult]) -> StructuredFinish | None:
        candidates = [result for result in results if self._eligible(result)]
        if len(candidates) != 1:
            return None
        result = candidates[0]
        lineage = result.semantic_lineage
        assert lineage is not None
        operator = lineage.semantic_operator
        if operator in {"top", "bottom"}:
            return self._finish_ranked(result, operator=operator)
        if operator in {"sum", "avg", "min", "max"}:
            return self._finish_aggregate(result)
        return None

    @staticmethod
    def _eligible(result: ToolResult) -> bool:
        lineage = result.semantic_lineage
        data_result = result.data_result
        return (
            result.status == "success"
            and isinstance(data_result, TableDataResult)
            and data_result.presentation is not None
            and lineage is not None
            and lineage.semantic_contract_fingerprint is not None
            and lineage.semantic_shape_id is not None
            and lineage.semantic_operator is not None
            and lineage.semantic_completeness == "complete"
        )

    def _finish_ranked(
        self, result: ToolResult, *, operator: str
    ) -> StructuredFinish | None:
        data_result = result.data_result
        assert isinstance(data_result, TableDataResult)
        presentation = data_result.presentation
        assert presentation is not None
        rows = _rows(data_result.data)
        winners = [row for row in rows if row.get("rank") == 1]
        metric = _first_metric_field(presentation.fields, rows)
        locator = _first_identifier_field(presentation.fields, rows)
        label = _first_label_field(presentation.fields, rows)
        if not winners or metric is None or locator is None or label is None:
            return None
        metric_values = [_claim_value(row.get(metric.field)) for row in winners]
        if any(value is _INVALID for value in metric_values):
            return None
        if len(set(metric_values)) != 1:
            return None
        names = [_claim_value(row.get(label.field)) for row in winners]
        if any(not isinstance(name, str) or not name for name in names):
            return None
        claims: list[AnswerClaim] = []
        for index, row in enumerate(winners, start=1):
            locator_value = _claim_value(row.get(locator.field))
            metric_value = _claim_value(row.get(metric.field))
            if locator_value is _INVALID or metric_value is _INVALID:
                return None
            typed_locator_value = cast(ClaimScalar, locator_value)
            typed_metric_value = cast(ClaimScalar, metric_value)
            row_locator: dict[str, ClaimScalar] = {
                locator.field: typed_locator_value
            }
            claims.extend(
                [
                    AnswerClaim(
                        claim_id=f"contract-result-{index}-rank",
                        result_id=data_result.result_id,
                        result_fingerprint=data_result.result_fingerprint,
                        collection="rows",
                        row_locator=row_locator,
                        field="rank",
                        operation="value",
                        value=1,
                    ),
                    AnswerClaim(
                        claim_id=f"contract-result-{index}-metric",
                        result_id=data_result.result_id,
                        result_fingerprint=data_result.result_fingerprint,
                        collection="rows",
                        row_locator=row_locator,
                        field=metric.field,
                        operation="value",
                        value=typed_metric_value,
                    ),
                ]
            )
        direction = "最高" if operator == "top" else "最低"
        value = metric_values[0]
        unit = metric.unit or ""
        joined_names = "、".join(name for name in names if isinstance(name, str))
        if len(winners) == 1:
            summary = (
                f"{joined_names}的{metric.label}为{value}{unit}，"
                f"为本次查询{direction}。"
            )
        else:
            summary = (
                f"{joined_names}的{metric.label}均为{value}{unit}，"
                f"并列本次查询{direction}。"
            )
        return StructuredFinish(kind="claims", summary=summary, claims=claims)

    @staticmethod
    def _finish_aggregate(result: ToolResult) -> StructuredFinish | None:
        data_result = result.data_result
        assert isinstance(data_result, TableDataResult)
        presentation = data_result.presentation
        assert presentation is not None
        rows = _rows(data_result.data)
        metric = _first_metric_field(presentation.fields, rows)
        if len(rows) != 1 or metric is None:
            return None
        row = rows[0]
        value = _claim_value(row.get(metric.field))
        if value is _INVALID:
            return None
        locator: dict[str, ClaimScalar] = {}
        for field in presentation.fields:
            locator_value = _claim_value(row.get(field.field))
            if field.role == "dimension" and locator_value is not _INVALID:
                locator[field.field] = cast(ClaimScalar, locator_value)
        return StructuredFinish(
            kind="claims",
            summary=presentation.summary,
            claims=[
                AnswerClaim(
                    claim_id="contract-result-aggregate-metric",
                    result_id=data_result.result_id,
                    result_fingerprint=data_result.result_fingerprint,
                    collection="rows",
                    row_locator=locator,
                    field=metric.field,
                    operation="value",
                    value=cast(ClaimScalar, value),
                )
            ],
        )


_INVALID = object()


def _rows(data: BaseModel) -> list[dict[str, object]]:
    raw_rows = getattr(data, "rows", None)
    if not isinstance(raw_rows, list):
        return []
    rows: list[dict[str, object]] = []
    for row in raw_rows:
        if isinstance(row, BaseModel):
            rows.append(row.model_dump(mode="python"))
        elif isinstance(row, dict) and all(isinstance(key, str) for key in row):
            rows.append(row)
        else:
            return []
    return rows


def _claim_value(value: object) -> ClaimValue | object:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return _INVALID


def _first_metric_field(
    fields: list[ResultDisplayField], rows: list[dict[str, object]]
) -> ResultDisplayField | None:
    return next(
        (
            field
            for field in fields
            if field.role == "metric" and all(field.field in row for row in rows)
        ),
        None,
    )


def _first_identifier_field(
    fields: list[ResultDisplayField], rows: list[dict[str, object]]
) -> ResultDisplayField | None:
    return next(
        (
            field
            for field in fields
            if field.role == "identifier"
            and all(_claim_value(row.get(field.field)) is not _INVALID for row in rows)
        ),
        None,
    )


def _first_label_field(
    fields: list[ResultDisplayField], rows: list[dict[str, object]]
) -> ResultDisplayField | None:
    return next(
        (
            field
            for field in fields
            if field.role == "dimension"
            and field.field != "rank"
            and all(isinstance(row.get(field.field), str) for row in rows)
        ),
        None,
    )
