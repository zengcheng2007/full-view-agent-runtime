import re
from decimal import Decimal, InvalidOperation
from typing import Any

from full_view_agent.domain.models import ToolResult

_ANSWER_NUMBER_RE = re.compile(r"(?<![\w])\d[\d,]*(?:\.\d+)?")


def unsupported_answer_numbers(
    summary: str, results: tuple[ToolResult, ...]
) -> set[str]:
    allowed = _build_allowed_number_ledger(results)
    return {
        token
        for raw in _ANSWER_NUMBER_RE.findall(summary)
        if (token := _normalize_number(raw)) not in allowed
    }


def remove_lines_with_numbers(summary: str, unsupported: set[str]) -> str:
    return "\n".join(
        line
        for line in summary.splitlines()
        if not any(
            _normalize_number(raw) in unsupported
            for raw in _ANSWER_NUMBER_RE.findall(line)
        )
    ).strip()


def _build_allowed_number_ledger(results: tuple[ToolResult, ...]) -> set[str]:
    allowed: set[str] = set()
    for result in results:
        if result.status not in {"success", "partial"} or result.data_result is None:
            continue
        payload = result.data_result.model_dump(mode="json")
        _collect_scalars(payload, allowed)
        data = payload.get("data")
        if isinstance(data, dict) and isinstance(data.get("rows"), list):
            rows = data["rows"]
            allowed.add(_normalize_number(str(len(rows))))
            _collect_row_derivations(rows, allowed)
    return allowed


def _collect_scalars(value: Any, allowed: set[str]) -> None:
    if isinstance(value, bool) or value is None:
        return
    if isinstance(value, (int, float, Decimal)):
        allowed.add(_normalize_number(str(value)))
        return
    if isinstance(value, str):
        if value.isdigit():
            allowed.add(_normalize_number(value))
        return
    if isinstance(value, dict):
        for nested in value.values():
            _collect_scalars(nested, allowed)
        return
    if isinstance(value, list):
        for nested in value:
            _collect_scalars(nested, allowed)


def _collect_row_derivations(rows: list[Any], allowed: set[str]) -> None:
    numeric_columns: dict[str, list[Decimal]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        for key, value in row.items():
            if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
                continue
            numeric_columns.setdefault(key, []).append(Decimal(str(value)))
    for values in numeric_columns.values():
        if not values:
            continue
        total = sum(values, Decimal(0))
        for value in (total, min(values), max(values)):
            allowed.add(_normalize_decimal(value))
        if total:
            for value in values:
                percentage = (value * Decimal(100) / total).quantize(
                    Decimal("0.01")
                )
                allowed.add(_normalize_decimal(percentage))


def _normalize_number(raw: str) -> str:
    try:
        return _normalize_decimal(Decimal(raw.replace(",", "")))
    except InvalidOperation:
        return raw


def _normalize_decimal(value: Decimal) -> str:
    normalized = value.normalize()
    if normalized == normalized.to_integral():
        return str(normalized.quantize(Decimal(1)))
    return format(normalized, "f").rstrip("0").rstrip(".")
