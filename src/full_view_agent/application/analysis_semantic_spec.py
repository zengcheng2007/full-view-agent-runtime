"""从可信 AnalysisPlan 确定性重建每个主题的 SemanticQuerySpec。"""

from full_view_agent.domain.analysis_plan import AnalysisPlan, AnalysisStep, AreaScopeRef
from full_view_agent.semantic.catalog import SemanticCatalog, SubjectDefinition
from full_view_agent.semantic.query_spec import SemanticFilter, SemanticQuerySpec


class AnalysisSemanticSpecError(ValueError):
    """计划步骤无法转换为受控语义查询。"""


class AnalysisSemanticSpecFactory:
    """执行器与报告校验器共享的服务端语义请求真源。"""

    def __init__(self, catalog: SemanticCatalog) -> None:
        self._catalog = catalog

    def build(self, plan: AnalysisPlan, step: AnalysisStep) -> SemanticQuerySpec:
        subject = self._catalog.subject(step.subject)
        binding = self._catalog.binding(step.subject)
        if subject is None or binding is None or not isinstance(step.scope_ref, AreaScopeRef):
            raise AnalysisSemanticSpecError(
                f"analysis step {step.step_id} is not executable by the semantic catalog"
            )
        if (
            binding.capability_id != step.capability_id
            or binding.capability_version != step.capability_version
        ):
            raise AnalysisSemanticSpecError(
                f"analysis step {step.step_id} does not match its catalog binding"
            )
        return SemanticQuerySpec(
            subject=subject.subject_id,
            metrics=[metric.metric_id for metric in subject.metrics],
            scope=step.scope_ref.scope,
            group_by=self._default_group_by(subject, step.scope_ref),
            filters=[
                SemanticFilter(
                    field=required.field,
                    operator=required.operator,
                    value=required.value,
                )
                for required in subject.required_filters
            ],
            output="table",
        )

    @staticmethod
    def _default_group_by(
        subject: SubjectDefinition,
        scope_ref: AreaScopeRef,
    ) -> list[str]:
        if subject.min_group_by == 0:
            return []
        scope_level = len(scope_ref.scope.area_code)
        candidates = sorted(
            rule.value
            for rule in subject.group_by_rules
            if scope_level in rule.allowed_scope_levels
        )
        if len(candidates) < subject.min_group_by:
            raise AnalysisSemanticSpecError(
                f"subject {subject.subject_id} has no safe grouping for this scope"
            )
        return candidates[: subject.min_group_by]
