"""S0 语义内核候选：Compiler 与结果 Schema 校验。

Compiler 把通过 Validator 的 SemanticQuerySpec 编译为框架中立的
SemanticPlan：计划步骤只绑定已验证的能力标识（生产 Tool 契约 ID）与
受控参数，并携带该主题准确的结果 Schema 期望与 Evidence 期望。
物理 adapter 映射保存在 Catalog 的内部绑定中，不进入计划的序列化。

verify_result 在执行后核对结果 Schema：主题之间不得串用 Schema，
schema_ref 正确但行字段漂移同样拒绝。
"""

from typing import Literal

from pydantic import Field

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
from full_view_agent.semantic.validator import SemanticValidator


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


class SemanticCompiler:
    def __init__(
        self,
        catalog: SemanticCatalog,
        validator: SemanticValidator | None = None,
    ) -> None:
        self._catalog = catalog
        self._validator = validator or SemanticValidator(catalog)

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
        shape = self._select_shape(subject, spec)
        return SemanticPlan(
            catalog_version=self._catalog.catalog_version,
            subject=subject.subject_id,
            logical_dataset_id=subject.logical_dataset_id,
            steps=(
                PlanStep(
                    capability_id=binding.capability_id,
                    capability_version=binding.capability_version,
                    arguments=self._build_arguments(binding.capability_id, spec),
                ),
            ),
            expected_result=ExpectedResultShape(
                data_schema_ref=shape.data_schema_ref,
                row_fields=shape.row_fields,
                fingerprint_domain=shape.fingerprint_domain,
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
                shape.group_by_selection is not None
                and shape.group_by_selection == requested
                and (
                    shape.metric_selection is None
                    or shape.metric_selection == requested_metrics
                )
            ):
                return shape
        for shape in subject.result_shapes:
            if shape.group_by_selection is None:
                return shape
        raise SemanticKernelError(
            f"subject {subject.subject_id} has no result shape for "
            f"metrics {list(requested_metrics)} and group_by {list(requested)}"
        )

    @staticmethod
    def _build_arguments(
        capability_id: str,
        spec: SemanticQuerySpec,
    ) -> dict[str, object]:
        scope = spec.scope.model_dump()
        if capability_id == "governance.query_population_metrics":
            return {
                "query": {
                    "schema_version": "1.1",
                    "metrics": list(spec.metrics),
                    "scope": scope,
                    "filters": [
                        query_filter.model_dump() for query_filter in spec.filters
                    ],
                    "group_by": list(spec.group_by),
                    "order_by": [order.model_dump() for order in spec.order_by],
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
