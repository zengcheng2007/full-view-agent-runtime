import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from full_view_agent.domain.models import ToolResult

GroundingReasonCode = Literal[
    "grounded",
    "unsupported_number",
    "unsupported_area",
    "unsupported_object",
    "unsupported_judgement",
]

_ANSWER_NUMBER_RE = re.compile(r"(?<![\w])\d[\d,]*(?:\.\d+)?")
_AREA_RE = re.compile(
    r"(?:^|[，。；：、\s的])"
    r"([\u4e00-\u9fff]{2,12}?(?:街道|社区|网格|区|县|市|镇|乡|村))"
    r"(?=$|[，。；：、\s的]|查询|结果|出租|人口|有|为|最多|最少|最高|最低)"
)
_OBJECT_RE = re.compile(
    r"(?:^|[，。；：、\s的])"
    r"([\u4e00-\u9fffA-Za-z·_-]{1,20}\d+(?:幢|号楼))"
    r"(?=$|[，。；：、\s的查询])"
)
_ALL_SAME_PATTERNS = ("全部相同", "均相同", "都相同", "完全相同")
_GENERIC_AREA_MARKERS = (
    "几个",
    "三个",
    "各个",
    "各区",
    "各街道",
    "各社区",
    "按区",
    "按街道",
    "按社区",
    "区县",
    "全市",
    "下级",
    "哪个",
    "某个",
    "街道级",
    "社区级",
    "不支持",
)


@dataclass(frozen=True)
class ComparisonFacts:
    source_result_id: str
    values_by_label: dict[str, Decimal]


@dataclass
class AnswerFactLedger:
    numbers: set[str] = field(default_factory=set)
    areas: set[str] = field(default_factory=set)
    objects: set[str] = field(default_factory=set)
    comparisons: list[ComparisonFacts] = field(default_factory=list)
    sources: dict[str, set[str]] = field(default_factory=dict)


@dataclass(frozen=True)
class GroundingCheck:
    reason_code: GroundingReasonCode
    unsupported_values: frozenset[str] = frozenset()


def assess_answer_grounding(
    summary: str, results: tuple[ToolResult, ...]
) -> GroundingCheck:
    ledger = build_fact_ledger(results)
    checks: tuple[tuple[GroundingReasonCode, set[str]], ...] = (
        ("unsupported_area", _unsupported_areas(summary, ledger)),
        ("unsupported_object", _unsupported_objects(summary, ledger)),
        ("unsupported_number", _unsupported_numbers(summary, ledger)),
        ("unsupported_judgement", _unsupported_judgements(summary, ledger)),
    )
    for reason_code, unsupported in checks:
        if unsupported:
            return GroundingCheck(reason_code, frozenset(unsupported))
    return GroundingCheck("grounded")


def unsupported_answer_numbers(
    summary: str, results: tuple[ToolResult, ...]
) -> set[str]:
    return _unsupported_numbers(summary, build_fact_ledger(results))


def remove_lines_with_numbers(summary: str, unsupported: set[str]) -> str:
    return "\n".join(
        line
        for line in summary.splitlines()
        if not any(
            _normalize_number(raw) in unsupported
            for raw in _ANSWER_NUMBER_RE.findall(line)
        )
    ).strip()


def remove_lines_with_values(summary: str, unsupported: set[str]) -> str:
    return "\n".join(
        line for line in summary.splitlines() if not any(v in line for v in unsupported)
    ).strip()


def build_fact_ledger(results: tuple[ToolResult, ...]) -> AnswerFactLedger:
    ledger = AnswerFactLedger()
    for result in results:
        if result.status not in {"success", "partial"} or result.data_result is None:
            continue
        result_ledger = AnswerFactLedger()
        payload = result.data_result.model_dump(mode="json")
        _collect_scalars(payload, result_ledger.numbers)
        data = payload.get("data")
        if not isinstance(data, dict):
            continue
        _collect_named_facts(data, result_ledger)
        rows = data.get("rows")
        if isinstance(rows, list):
            result_ledger.numbers.add(_normalize_number(str(len(rows))))
            _collect_row_derivations(rows, result_ledger.numbers)
            comparison = _comparison_facts(
                rows, source_result_id=result.data_result.result_id
            )
            if comparison is not None:
                result_ledger.comparisons.append(comparison)
        _merge_ledger(
            target=ledger,
            source=result_ledger,
            result_id=result.data_result.result_id,
        )
    return ledger


def _unsupported_numbers(summary: str, ledger: AnswerFactLedger) -> set[str]:
    return {
        token
        for raw in _ANSWER_NUMBER_RE.findall(summary)
        if (token := _normalize_number(raw)) not in ledger.numbers
    }


def _unsupported_areas(summary: str, ledger: AnswerFactLedger) -> set[str]:
    claims = {
        candidate
        for candidate in _AREA_RE.findall(summary)
        if len(candidate) <= 8
        and not any(marker in candidate for marker in _GENERIC_AREA_MARKERS)
    }
    return {
        claim
        for claim in claims
        if not any(
            claim == area_name or claim.endswith(area_name)
            for area_name in ledger.areas
        )
    }


def _unsupported_objects(summary: str, ledger: AnswerFactLedger) -> set[str]:
    claims = {candidate.rsplit("的", 1)[-1] for candidate in _OBJECT_RE.findall(summary)}
    return claims - ledger.objects


def _unsupported_judgements(
    summary: str, ledger: AnswerFactLedger
) -> set[str]:
    violations: set[str] = set()
    if (
        any(pattern in summary for pattern in _ALL_SAME_PATTERNS)
        and not any(
            facts.values_by_label
            and len(set(facts.values_by_label.values())) == 1
            for facts in ledger.comparisons
        )
    ):
        violations.add("全部相同")

    for facts in ledger.comparisons:
        values = facts.values_by_label
        if not values:
            continue
        violations.update(_label_value_violations(summary, values))
        maximum = max(values.values())
        minimum = min(values.values())
        for label, value in values.items():
            match = re.search(
                rf"{re.escape(label)}.{{0,12}}?(最多|最高|最少|最低)",
                summary,
            )
            if match is not None:
                direction = match.group(1)
                expected = maximum if direction in {"最多", "最高"} else minimum
                if value != expected:
                    violations.add(f"{label}{direction}")
            inverted_match = re.search(
                rf"(最多|最高|最少|最低)(?:的)?(?:是|为)?\s*"
                rf"{re.escape(label)}",
                summary,
            )
            if inverted_match is not None:
                inverted_direction = inverted_match.group(1)
                inverted_expected = (
                    maximum
                    if inverted_direction in {"最多", "最高"}
                    else minimum
                )
                if value != inverted_expected:
                    violations.add(f"{inverted_direction}{label}")
        if (
            "并列最多" in summary or "并列最高" in summary
        ) and sum(value == maximum for value in values.values()) < 2:
            violations.add("并列最多")
        if (
            "并列最少" in summary or "并列最低" in summary
        ) and sum(value == minimum for value in values.values()) < 2:
            violations.add("并列最少")
    return violations


def _label_value_violations(
    summary: str, values_by_label: dict[str, Decimal]
) -> set[str]:
    """Reject locally bound label/value pairs that contradict their source row."""
    violations: set[str] = set()
    value_pattern = re.compile(
        r"(?<![\w])(?P<value>\d[\d,]*(?:\.\d+)?)\s*"
        r"(?:套|人|户|件|个|栋|幢)"
    )
    for clause in re.split(r"[，。；\n]", summary):
        labels = sorted(
            (
                (match.start(), label)
                for label in values_by_label
                for match in re.finditer(re.escape(label), clause)
            ),
            key=lambda item: item[0],
        )
        value_matches = list(value_pattern.finditer(clause))
        if "分别" in clause and len(labels) == len(value_matches) and len(labels) > 1:
            for (_, label), match in zip(labels, value_matches, strict=True):
                actual = _normalize_number(match.group("value"))
                if actual != _normalize_decimal(values_by_label[label]):
                    violations.add(f"{label}:{actual}")
            continue
        for index, (position, label) in enumerate(labels):
            segment_end = labels[index + 1][0] if index + 1 < len(labels) else len(clause)
            segment = clause[position + len(label) : segment_end]
            match = value_pattern.search(segment)
            if match is None:
                continue
            actual = _normalize_number(match.group("value"))
            if actual != _normalize_decimal(values_by_label[label]):
                violations.add(f"{label}:{actual}")
    return violations


def _collect_named_facts(data: dict[str, Any], ledger: AnswerFactLedger) -> None:
    candidates = data.get("candidates")
    if isinstance(candidates, list):
        for candidate in candidates:
            if isinstance(candidate, dict):
                area_name = candidate.get("area_name")
                if isinstance(area_name, str):
                    ledger.areas.add(area_name)

    rows = data.get("rows")
    if isinstance(rows, list):
        for row in rows:
            if isinstance(row, dict):
                area_name = row.get("area_name")
                if isinstance(area_name, str):
                    ledger.areas.add(area_name)

    title = data.get("title")
    if isinstance(title, str):
        ledger.objects.add(title)
        _collect_embedded_numbers(title, ledger.numbers)
    fields = data.get("fields")
    if isinstance(fields, list):
        for item in fields:
            if not isinstance(item, dict) or item.get("masked") is True:
                continue
            value = item.get("value")
            if isinstance(value, str):
                ledger.objects.add(value)
                _collect_embedded_numbers(value, ledger.numbers)


def _collect_embedded_numbers(value: str, allowed: set[str]) -> None:
    for raw in _ANSWER_NUMBER_RE.findall(value):
        allowed.add(_normalize_number(raw))


def _comparison_facts(
    rows: list[Any], *, source_result_id: str
) -> ComparisonFacts | None:
    values_by_label: dict[str, Decimal] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        label = next(
            (
                value
                for key in ("lease_type", "area_name", "level")
                if isinstance((value := row.get(key)), str)
            ),
            None,
        )
        numeric = next(
            (
                Decimal(str(value))
                for key, value in row.items()
                if key != "area_code"
                and isinstance(value, (int, float, Decimal))
                and not isinstance(value, bool)
            ),
            None,
        )
        if label is not None and numeric is not None:
            values_by_label[label] = numeric
    return (
        ComparisonFacts(
            source_result_id=source_result_id,
            values_by_label=values_by_label,
        )
        if values_by_label
        else None
    )


def _merge_ledger(
    *,
    target: AnswerFactLedger,
    source: AnswerFactLedger,
    result_id: str,
) -> None:
    for kind, values in (
        ("number", source.numbers),
        ("area", source.areas),
        ("object", source.objects),
    ):
        for value in values:
            target.sources.setdefault(f"{kind}:{value}", set()).add(result_id)
    target.numbers.update(source.numbers)
    target.areas.update(source.areas)
    target.objects.update(source.objects)
    target.comparisons.extend(source.comparisons)


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
                percentage = (value * Decimal(100) / total).quantize(Decimal("0.01"))
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
