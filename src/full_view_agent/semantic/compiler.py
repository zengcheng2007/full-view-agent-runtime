"""S0 语义内核候选：Compiler 与结果 Schema 校验。

Compiler 把通过 Validator 的 SemanticQuerySpec 编译为框架中立的
SemanticPlan：计划步骤只绑定已验证的能力标识（生产 Tool 契约 ID）与
受控参数，并携带该主题准确的结果 Schema 期望与 Evidence 期望。
物理 adapter 映射保存在 Catalog 的内部绑定中，不进入计划的序列化。

verify_result 在执行后核对结果 Schema：主题之间不得串用 Schema，
schema_ref 正确但行字段漂移同样拒绝。
"""

from collections.abc import Mapping
from typing import TYPE_CHECKING, Literal

from pydantic import Field

from full_view_agent.application.fingerprints import canonical_fingerprint
from full_view_agent.domain.models import ContractModel, TableDataResult
from full_view_agent.semantic.authorization import SubjectAuthorization
from full_view_agent.semantic.catalog import (
    ResultShape,
    SemanticCatalog,
    SubjectDefinition,
)
from full_view_agent.semantic.errors import (
    ResultSchemaMismatch,
    SemanticKernelError,
    SemanticQueryRejected,
)
from full_view_agent.semantic.query_spec import (
    SEMANTIC_SPEC_VERSION,
    SemanticQuerySpec,
)
from full_view_agent.semantic.tool_contract_compiler import (
    ToolSemanticContractCompiler,
    ToolSemanticQuery,
)
from full_view_agent.semantic.validator import SemanticValidator

if TYPE_CHECKING:
    from full_view_agent.application.tool_registry import ToolRegistry


class PlanStep(ContractModel):
    capability_id: str = Field(min_length=1, max_length=128)
    capability_version: str = Field(min_length=1, max_length=32)
    arguments: dict[str, object]


class ExpectedResultShape(ContractModel):
    kind: Literal["table"] = "table"
    data_schema_ref: str
    row_fields: tuple[str, ...]
    fingerprint_domain: str | None = None


class MetricDefinitionRef(ContractModel):
    metric_id: str
    definition_version: str


class EvidenceExpectation(ContractModel):
    dataset_id: str
    metric_definitions: tuple[MetricDefinitionRef, ...]


class SemanticPlan(ContractModel):
    schema_version: Literal["s0.1"] = SEMANTIC_SPEC_VERSION
    catalog_version: str
    subject: str
    logical_dataset_id: str
    steps: tuple[PlanStep, ...] = Field(min_length=1)
    expected_result: ExpectedResultShape
    evidence: EvidenceExpectation
    semantic_contract_fingerprint: str | None = None
    semantic_shape_id: str | None = None
    semantic_operator: str | None = None
    semantic_completeness: str | None = None
    semantic_tie_policy: str | None = None


class SemanticCompiler:
    def __init__(
        self,
        catalog: SemanticCatalog,
        validator: SemanticValidator | None = None,
        registry: "ToolRegistry | None" = None,
    ) -> None:
        self._catalog = catalog
        self._validator = validator or SemanticValidator(catalog)
        self._registry = registry

    def compile(
        self,
        spec: SemanticQuerySpec,
        *,
        authorization: SubjectAuthorization | None = None,
    ) -> SemanticPlan:
        report = self._validator.validate(spec, authorization=authorization)
        if not report.is_valid:
            raise SemanticQueryRejected(report.violations)
        subject = self._catalog.require_subject(spec.subject)
        binding = self._catalog.binding(spec.subject)
        if binding is None:
            raise SemanticKernelError(
                f"subject {spec.subject} has no verified capability binding"
            )
        compiled_contract = None
        manifest = None
        if self._registry is not None:
            manifest = self._registry.get_manifest(binding.capability_id)
            if manifest.semantic_contract is not None:
                compiled_contract = ToolSemanticContractCompiler().compile(
                    _contract_tool(manifest),
                    ToolSemanticQuery.model_validate(
                        {
                            "operator": spec.operator,
                            "metric": spec.metrics[0],
                            "dimension": spec.group_by[0] if spec.group_by else None,
                            "scope_level": len(spec.scope.area_code),
                            "filters": [
                                {
                                    "field": item.field,
                                    "operator": (
                                        "ne" if item.operator == "neq" else item.operator
                                    ),
                                    "value": item.value,
                                }
                                for item in spec.filters
                            ],
                            "limit": spec.limit,
                            "output_form": spec.output,
                        }
                    ),
                )
            elif spec.subject == "population" and spec.operator != "list":
                raise SemanticKernelError(
                    "population analytical operator requires a published semantic contract"
                )
        shape = None if compiled_contract is not None else self._select_shape(subject, spec)
        if compiled_contract is not None:
            result_schema_ref = compiled_contract.result_schema_ref
            result_row_fields = compiled_contract.result_row_fields
            result_fingerprint_domain = compiled_contract.result_fingerprint_domain
        else:
            assert shape is not None
            result_schema_ref = shape.data_schema_ref
            result_row_fields = shape.row_fields
            result_fingerprint_domain = shape.fingerprint_domain
        contract_fingerprint = None
        if manifest is not None and manifest.semantic_contract is not None:
            contract_fingerprint = canonical_fingerprint(
                domain=(
                    f"tool-semantic-contract:{manifest.tool_id}:"
                    f"{manifest.tool_version}"
                ),
                value=manifest.semantic_contract,
            )
        capability_version = (
            manifest.tool_version if manifest is not None else binding.capability_version
        )
        return SemanticPlan(
            catalog_version=self._catalog.catalog_version,
            subject=subject.subject_id,
            logical_dataset_id=subject.logical_dataset_id,
            steps=(
                PlanStep(
                    capability_id=binding.capability_id,
                    capability_version=capability_version,
                    arguments=self._build_arguments(
                        binding.capability_id,
                        spec,
                        argument_template=(
                            compiled_contract.argument_template
                            if compiled_contract is not None
                            else None
                        ),
                    ),
                ),
            ),
            expected_result=ExpectedResultShape(
                data_schema_ref=result_schema_ref,
                row_fields=result_row_fields,
                fingerprint_domain=result_fingerprint_domain,
            ),
            evidence=EvidenceExpectation(
                dataset_id=subject.logical_dataset_id,
                metric_definitions=tuple(
                    MetricDefinitionRef(
                        metric_id=metric.metric_id,
                        definition_version=metric.definition_version,
                    )
                    for metric in subject.metrics
                    if metric.metric_id in spec.metrics
                ),
            ),
            semantic_contract_fingerprint=contract_fingerprint,
            semantic_shape_id=(
                compiled_contract.shape_id if compiled_contract is not None else None
            ),
            semantic_operator=(
                compiled_contract.operator if compiled_contract is not None else None
            ),
            semantic_completeness=(
                compiled_contract.completeness.mode
                if compiled_contract is not None
                else None
            ),
            semantic_tie_policy=(
                compiled_contract.tie_policy if compiled_contract is not None else None
            ),
        )

    def verify_result(self, plan: SemanticPlan, result: TableDataResult) -> None:
        expected = plan.expected_result
        if result.kind != expected.kind:
            raise ResultSchemaMismatch(
                f"result kind {result.kind} does not match expected {expected.kind}"
            )
        if result.data_schema_ref != expected.data_schema_ref:
            raise ResultSchemaMismatch(
                f"result schema {result.data_schema_ref} does not match "
                f"expected {expected.data_schema_ref} for subject {plan.subject}"
            )
        rows = result.data.rows
        if rows:
            first_row = rows[0]
            actual_fields = set(
                first_row.keys()
                if isinstance(first_row, dict)
                else first_row.model_dump().keys()
            )
            if actual_fields != set(expected.row_fields):
                raise ResultSchemaMismatch(
                    f"result row fields {sorted(actual_fields)} do not match "
                    f"expected {sorted(expected.row_fields)}"
                )

    @staticmethod
    def _select_shape(
        subject: SubjectDefinition,
        spec: SemanticQuerySpec,
    ) -> ResultShape:
        requested = tuple(spec.group_by)
        requested_metrics = tuple(spec.metrics)
        for shape in subject.result_shapes:
            if (
                shape.operator_selection is not None
                and spec.operator in shape.operator_selection
                and (
                    shape.metric_selection is None
                    or shape.metric_selection == requested_metrics
                )
            ):
                return shape
        for shape in subject.result_shapes:
            if (
                shape.operator_selection is None
                and
                shape.group_by_selection is not None
                and shape.group_by_selection == requested
                and (
                    shape.metric_selection is None
                    or shape.metric_selection == requested_metrics
                )
            ):
                return shape
        for shape in subject.result_shapes:
            if (
                shape.operator_selection is None
                and shape.group_by_selection is None
            ):
                return shape
        raise SemanticKernelError(
            f"subject {subject.subject_id} has no result shape for "
            f"metrics {list(requested_metrics)} and group_by {list(requested)}"
        )

    @staticmethod
    def _build_arguments(
        capability_id: str,
        spec: SemanticQuerySpec,
        *,
        argument_template: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        if argument_template is not None:
            rendered = _render_argument_template(argument_template, spec)
            if not isinstance(rendered, dict):
                raise SemanticKernelError("semantic argument template must render an object")
            return rendered
        scope = spec.scope.model_dump()
        if capability_id == "governance.query_population_metrics":
            order_by = [order.model_dump() for order in spec.order_by]
            if not order_by and spec.operator in ("top", "bottom", "rank"):
                order_by = [
                    {
                        "field": "person_count",
                        "direction": "asc" if spec.operator == "bottom" else "desc",
                    }
                ]
            return {
                "query": {
                    "schema_version": "1.1",
                    "metrics": list(spec.metrics),
                    "operator": spec.operator,
                    "scope": scope,
                    "filters": [
                        query_filter.model_dump() for query_filter in spec.filters
                    ],
                    "group_by": list(spec.group_by),
                    "order_by": order_by,
                    "limit": spec.limit,
                    "presentation_hint": (
                        "choropleth" if spec.output == "choropleth" else "table"
                    ),
                }
            }
        if capability_id == "governance.query_housing_metrics":
            return {
                "query": {
                    "schema_version": "1.1",
                    "metrics": list(spec.metrics),
                    "scope": scope,
                    "group_by": list(spec.group_by),
                    "limit": spec.limit,
                }
            }
        if capability_id == "governance.query_event_metrics":
            if spec.metrics == ["finish_rate"]:
                return {
                    "query": {
                        "schema_version": "1.1",
                        "scope": scope,
                        "limit": spec.limit,
                    }
                }
            query: dict[str, object] = {
                "schema_version": "1.1",
                "metrics": list(spec.metrics),
                "scope": scope,
                "group_by": list(spec.group_by),
                "limit": spec.limit,
            }
            if spec.time_range is not None:
                query["time_range"] = spec.time_range.model_dump(mode="json")
            return {"query": query}
        if capability_id == "governance.get_governance_overview":
            return {
                "query": {
                    "schema_version": "1.1",
                    "scope": scope,
                }
            }
        if capability_id == "governance.query_governance_power_metrics":
            return {
                "query": {
                    "schema_version": "1.0",
                    "scope": scope,
                }
            }
        if capability_id == "governance.query_enterprise_metrics":
            return {
                "query": {
                    "schema_version": "1.1",
                    "scope": scope,
                    "group_by": list(spec.group_by),
                    "limit": spec.limit,
                }
            }
        raise SemanticKernelError(f"no compilation rule for {capability_id}")


def _render_argument_template(value: object, spec: SemanticQuerySpec) -> object:
    placeholders: dict[str, object] = {
        "$semantic.metrics": list(spec.metrics),
        "$semantic.metric": spec.metrics[0],
        "$semantic.operator": spec.operator,
        "$semantic.scope": spec.scope.model_dump(mode="json"),
        "$semantic.scope.area_code": spec.scope.area_code,
        "$semantic.group_by": list(spec.group_by),
        "$semantic.filters": [item.model_dump(mode="json") for item in spec.filters],
        "$semantic.order_by": [item.model_dump(mode="json") for item in spec.order_by],
        "$semantic.limit": spec.limit,
        "$semantic.output": spec.output,
        "$semantic.time_range": (
            spec.time_range.model_dump(mode="json")
            if spec.time_range is not None
            else None
        ),
    }
    if isinstance(value, str) and value.startswith("$semantic."):
        if value not in placeholders:
            raise SemanticKernelError(f"unsupported semantic placeholder {value}")
        return placeholders[value]
    if isinstance(value, dict):
        return {key: _render_argument_template(item, spec) for key, item in value.items()}
    if isinstance(value, list):
        return [_render_argument_template(item, spec) for item in value]
    return value


def _contract_tool(manifest):
    from full_view_agent.domain.capability import ToolCapability

    return ToolCapability(
        capability_id=manifest.tool_id,
        name=manifest.tool_id,
        owner=manifest.owner,
        version=manifest.tool_version,
        status="published",
        connector_ref="runtime.contract",
        resource_path="/runtime-contract",
        dataset_ids=[manifest.dataset_id],
        semantic_contract=manifest.semantic_contract,
    )
