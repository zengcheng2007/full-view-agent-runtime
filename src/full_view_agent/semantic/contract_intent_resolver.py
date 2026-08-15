"""Resolve bounded natural-language intent only from published Tool contracts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from full_view_agent.domain.capability import (
    ToolSemanticContract,
    ToolSemanticQueryShape,
)
from full_view_agent.semantic.query_spec import SemanticQuerySpec


@dataclass(frozen=True)
class ResolvedContractSemanticIntent:
    shape_id: str
    spec: SemanticQuerySpec


@dataclass(frozen=True)
class ContractSemanticIntentPreview:
    status: Literal["matched", "unsupported", "ambiguous"]
    reason_code: str
    shape_id: str | None = None
    spec: SemanticQuerySpec | None = None


@dataclass(frozen=True)
class ContractIntentCoverage:
    valid: bool
    issues: tuple[str, ...]


class ContractSemanticIntentResolver:
    """Select one exact executable shape without subject-specific source rules."""

    def resolve(
        self,
        *,
        message: str,
        scope_area_code: str,
        contracts: tuple[ToolSemanticContract, ...],
    ) -> ResolvedContractSemanticIntent | None:
        preview = self.preview(
            message=message,
            scope_area_code=scope_area_code,
            contracts=contracts,
        )
        if preview.status != "matched" or preview.shape_id is None or preview.spec is None:
            return None
        return ResolvedContractSemanticIntent(
            shape_id=preview.shape_id,
            spec=preview.spec,
        )

    def preview(
        self,
        *,
        message: str,
        scope_area_code: str,
        contracts: tuple[ToolSemanticContract, ...],
    ) -> ContractSemanticIntentPreview:
        matches: list[tuple[ToolSemanticContract, ToolSemanticQueryShape]] = []
        subject_matches = [
            contract
            for contract in contracts
            if contract.intent_terms and _contains_any(message, contract.intent_terms)
        ]
        if not subject_matches:
            return ContractSemanticIntentPreview(
                status="unsupported", reason_code="SUBJECT_NOT_MATCHED"
            )
        if any(
            _contains_any(message, contract.excluded_intent_terms)
            for contract in subject_matches
        ):
            return ContractSemanticIntentPreview(
                status="unsupported", reason_code="EXCLUDED_INTENT"
            )
        failure_codes: list[str] = []
        for contract in subject_matches:
            metric = _select_named_item(message, contract.metrics)
            if metric is None:
                failure_codes.append("METRIC_NOT_MATCHED")
                continue
            operator = _select_operator(message, contract)
            if operator is None:
                failure_codes.append("OPERATOR_NOT_MATCHED")
                continue
            dimensions = _matched_named_items(message, contract.dimensions)
            if not dimensions and len(contract.dimensions) == 1:
                dimensions = [_item_id(contract.dimensions[0])]
            if not dimensions:
                failure_codes.append("DIMENSION_NOT_MATCHED")
                continue
            declared_shapes = [
                shape
                for shape in contract.query_shapes
                if shape.metric_selection == (metric,)
                and shape.dimension_selection in {(item,) for item in dimensions}
                and shape.operator_selection == (operator,)
                and shape.output_forms[0] in {"table", "choropleth", "metric_card"}
            ]
            if not declared_shapes:
                failure_codes.append("SHAPE_NOT_DECLARED")
                continue
            shapes = [
                shape
                for shape in declared_shapes
                if len(scope_area_code) in shape.scope_levels
            ]
            if not shapes:
                failure_codes.append("SCOPE_NOT_SUPPORTED")
                continue
            if len(shapes) == 1:
                matches.append((contract, shapes[0]))
            else:
                failure_codes.append("SHAPE_AMBIGUOUS")
        if len(matches) > 1:
            return ContractSemanticIntentPreview(
                status="ambiguous", reason_code="MULTIPLE_EXACT_SHAPES"
            )
        if not matches:
            distinct_codes = set(failure_codes)
            reason_code = (
                failure_codes[0]
                if len(distinct_codes) == 1 and failure_codes
                else "NO_UNIQUE_EXECUTABLE_SHAPE"
            )
            return ContractSemanticIntentPreview(
                status="unsupported", reason_code=reason_code
            )
        contract, shape = matches[0]
        operator = shape.operator_selection[0]
        return ContractSemanticIntentPreview(
            status="matched",
            reason_code="MATCHED_EXACT_SHAPE",
            shape_id=shape.shape_id,
            spec=SemanticQuerySpec.model_validate(
                {
                    "subject": contract.subject,
                    "operator": operator,
                    "metrics": list(shape.metric_selection),
                    "scope": {"area_code": scope_area_code},
                    "group_by": list(shape.dimension_selection),
                    "filters": [],
                    "limit": 1 if operator in {"top", "bottom"} else 200,
                    "output": shape.output_forms[0],
                }
            ),
        )

    def validate_coverage(
        self, contract: ToolSemanticContract
    ) -> ContractIntentCoverage:
        if not contract.intent_terms:
            return ContractIntentCoverage(valid=True, issues=())
        issues: list[str] = []
        for index, example in enumerate(contract.examples):
            candidate_shapes = [
                shape
                for shape in contract.query_shapes
                if shape.metric_selection == (example.metric,)
                and shape.dimension_selection
                == ((example.dimension,) if example.dimension is not None else ())
                and shape.operator_selection == (example.operator,)
            ]
            matched_shape_ids: set[str] = set()
            reasons: set[str] = set()
            for scope_level in sorted(
                {level for shape in candidate_shapes for level in shape.scope_levels}
            ):
                preview = self.preview(
                    message=example.question,
                    scope_area_code="1" * scope_level,
                    contracts=(contract,),
                )
                reasons.add(preview.reason_code)
                if preview.status == "matched" and preview.shape_id is not None:
                    matched_shape_ids.add(preview.shape_id)
            expected_shape_ids = {shape.shape_id for shape in candidate_shapes}
            if len(matched_shape_ids) != 1 or not matched_shape_ids.issubset(
                expected_shape_ids
            ):
                issues.append(
                    f"example[{index}] must resolve to one exact declared shape; "
                    f"matched={sorted(matched_shape_ids)} reasons={sorted(reasons)}"
                )
        return ContractIntentCoverage(valid=not issues, issues=tuple(issues))


def _contains_any(message: str, terms: tuple[str, ...]) -> bool:
    return any(term in message for term in terms)


def _select_named_item(message: str, items: tuple[object, ...]) -> str | None:
    matched = _matched_named_items(message, items)
    if len(matched) == 1:
        return matched[0]
    if not matched and len(items) == 1:
        return _item_id(items[0])
    return None


def _matched_named_items(message: str, items: tuple[object, ...]) -> list[str]:
    return [
        _item_id(item)
        for item in items
        if _contains_any(message, getattr(item, "intent_terms", ()))
    ]


def _item_id(item: object) -> str:
    return str(getattr(item, "metric_id", getattr(item, "dimension_id", "")))


def _select_operator(message: str, contract: ToolSemanticContract) -> str | None:
    matched = [
        item.operator
        for item in contract.operator_intents
        if _contains_any(message, item.terms)
    ]
    return matched[0] if len(matched) == 1 else None
