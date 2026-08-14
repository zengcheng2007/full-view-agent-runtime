"""S0 语义内核候选：Validator。

对 SemanticQuerySpec 做两类校验，全部返回结构化违规码，不静默改写：
1. 语义校验：未知主题/指标、非法维度/筛选/排序/输出、scope 层级与
   group_by 的直接下级规则、时间范围不支持、版本不兼容；
2. 授权校验（提供 SubjectAuthorization 时）：越权主题、越权数据集、
   越权字段策略集、越权区域。

语义规则只此一份；HTTP 与 InMemory 实现、未来的统一入口都必须经由此
Validator 与生产 Policy 复核，不得各自复制一套。
"""

from enum import StrEnum

from pydantic import ValidationError

from full_view_agent.domain.models import ContractModel, EventMetricTimeRange
from full_view_agent.semantic.authorization import SubjectAuthorization
from full_view_agent.semantic.catalog import (
    SemanticCatalog,
    SubjectDefinition,
    area_is_authorized,
)
from full_view_agent.semantic.query_spec import SemanticQuerySpec


class ViolationCode(StrEnum):
    UNKNOWN_SUBJECT = "UNKNOWN_SUBJECT"
    UNKNOWN_METRIC = "UNKNOWN_METRIC"
    INVALID_RESULT_SHAPE = "INVALID_RESULT_SHAPE"
    INVALID_GROUP_BY = "INVALID_GROUP_BY"
    GROUP_BY_SCOPE_MISMATCH = "GROUP_BY_SCOPE_MISMATCH"
    SCOPE_LEVEL_UNSUPPORTED = "SCOPE_LEVEL_UNSUPPORTED"
    INVALID_FILTER_FIELD = "INVALID_FILTER_FIELD"
    INVALID_FILTER_OPERATOR = "INVALID_FILTER_OPERATOR"
    INVALID_FILTER_VALUE = "INVALID_FILTER_VALUE"
    INVALID_ORDER_BY = "INVALID_ORDER_BY"
    TIME_RANGE_UNSUPPORTED = "TIME_RANGE_UNSUPPORTED"
    TIME_RANGE_REQUIRED = "TIME_RANGE_REQUIRED"
    INVALID_TIME_RANGE = "INVALID_TIME_RANGE"
    INVALID_OUTPUT = "INVALID_OUTPUT"
    CATALOG_VERSION_INCOMPATIBLE = "CATALOG_VERSION_INCOMPATIBLE"
    SUBJECT_NOT_ENTITLED = "SUBJECT_NOT_ENTITLED"
    DATASET_NOT_AUTHORIZED = "DATASET_NOT_AUTHORIZED"
    FIELD_POLICY_NOT_AUTHORIZED = "FIELD_POLICY_NOT_AUTHORIZED"
    AREA_OUT_OF_SCOPE = "AREA_OUT_OF_SCOPE"


class Violation(ContractModel):
    code: ViolationCode
    message: str
    path: str | None = None


class ValidationReport(ContractModel):
    violations: tuple[Violation, ...] = ()

    @property
    def is_valid(self) -> bool:
        return not self.violations


class SemanticValidator:
    def __init__(self, catalog: SemanticCatalog) -> None:
        self._catalog = catalog

    def validate(
        self,
        spec: SemanticQuerySpec,
        *,
        authorization: SubjectAuthorization | None = None,
    ) -> ValidationReport:
        violations: list[Violation] = []
        if spec.schema_version not in self._catalog.supported_spec_versions:
            return ValidationReport(
                violations=(
                    Violation(
                        code=ViolationCode.CATALOG_VERSION_INCOMPATIBLE,
                        message=(
                            f"spec 版本 {spec.schema_version} 与目录兼容版本"
                            f" {list(self._catalog.supported_spec_versions)} 不兼容。"
                        ),
                        path="schema_version",
                    ),
                )
            )
        subject = self._catalog.subject(spec.subject)
        if subject is None:
            return ValidationReport(
                violations=(
                    Violation(
                        code=ViolationCode.UNKNOWN_SUBJECT,
                        message=(
                            f"未知业务主题 {spec.subject}；可用主题："
                            f"{self._catalog.subject_ids()}。"
                        ),
                        path="subject",
                    ),
                )
            )
        if authorization is not None:
            violations.extend(
                self._authorization_violations(spec, subject, authorization)
            )
        violations.extend(self._metric_violations(spec, subject))
        violations.extend(self._scope_violations(spec, subject))
        violations.extend(self._group_by_violations(spec, subject))
        violations.extend(self._result_shape_violations(spec, subject))
        violations.extend(self._filter_violations(spec, subject))
        if spec.order_by:
            metric_ids = {metric.metric_id for metric in subject.metrics}
            intrinsic_order_matches = any(
                len(spec.order_by) == 1
                and spec.order_by[0].field == rule.field
                and spec.order_by[0].direction == rule.direction
                and tuple(spec.group_by) == rule.group_by_selection
                and spec.output == rule.output
                and not spec.filters
                and spec.metrics == [rule.field]
                for rule in subject.intrinsic_order_rules
            )
            if not subject.supports_order_by and not intrinsic_order_matches:
                violations.append(
                    Violation(
                        code=ViolationCode.INVALID_ORDER_BY,
                        message=(
                            f"主题 {subject.subject_id} 当前真实数据源未验证任何排序能力。"
                        ),
                        path="order_by",
                    )
                )
            elif any(order.field not in metric_ids for order in spec.order_by):
                violations.append(
                    Violation(
                        code=ViolationCode.INVALID_ORDER_BY,
                        message=(
                            f"主题 {subject.subject_id} 仅支持按已声明指标排序。"
                        ),
                        path="order_by",
                    )
                )
        is_event_trend = (
            subject.subject_id == "event"
            and spec.metrics == ["event_count"]
            and spec.group_by == ["month"]
        )
        if is_event_trend and spec.time_range is None:
            violations.append(
                Violation(
                    code=ViolationCode.TIME_RANGE_REQUIRED,
                    message="事件总数月度趋势必须指定开始和结束日期。",
                    path="time_range",
                )
            )
        elif is_event_trend and spec.time_range is not None:
            try:
                EventMetricTimeRange.model_validate(
                    spec.time_range.model_dump(mode="json")
                )
            except ValidationError:
                violations.append(
                    Violation(
                        code=ViolationCode.INVALID_TIME_RANGE,
                        message=(
                            "事件趋势日期必须为 yyyy-MM-dd，起始不早于"
                            " 2021-01-01，且最多覆盖 24 个自然月。"
                        ),
                        path="time_range",
                    )
                )
        elif spec.time_range is not None and (
            not subject.supports_time_range or subject.subject_id == "event"
        ):
            violations.append(
                Violation(
                    code=ViolationCode.TIME_RANGE_UNSUPPORTED,
                    message=(
                        f"主题 {subject.subject_id} 当前真实数据源不支持时间范围。"
                    ),
                    path="time_range",
                )
            )
        if spec.output not in subject.output_forms:
            violations.append(
                Violation(
                    code=ViolationCode.INVALID_OUTPUT,
                    message=(
                        f"主题 {subject.subject_id} 不支持输出形态 {spec.output}；"
                        f"可用：{list(subject.output_forms)}。"
                    ),
                    path="output",
                )
            )
        else:
            requested_group_by = tuple(spec.group_by)
            requested_metrics = tuple(spec.metrics)
            matching_shape = next(
                (
                    shape
                    for shape in subject.result_shapes
                    if (
                        shape.group_by_selection is None
                        or shape.group_by_selection == requested_group_by
                    )
                    and (
                        shape.metric_selection is None
                        or shape.metric_selection == requested_metrics
                    )
                ),
                None,
            )
            if (
                matching_shape is not None
                and matching_shape.output_forms is not None
                and spec.output not in matching_shape.output_forms
            ):
                violations.append(
                    Violation(
                        code=ViolationCode.INVALID_OUTPUT,
                        message=(
                            f"主题 {subject.subject_id} 的当前结果形状不支持输出"
                            f" {spec.output}；可用："
                            f"{list(matching_shape.output_forms)}。"
                        ),
                        path="output",
                    )
                )
        return ValidationReport(violations=tuple(violations))

    @staticmethod
    def _authorization_violations(
        spec: SemanticQuerySpec,
        subject: SubjectDefinition,
        authorization: SubjectAuthorization,
    ) -> list[Violation]:
        violations: list[Violation] = []
        if not area_is_authorized(spec.scope, authorization):
            violations.append(
                Violation(
                    code=ViolationCode.AREA_OUT_OF_SCOPE,
                    message=(
                        f"区域 {spec.scope.area_code} 不在当前授权的区划范围内。"
                    ),
                    path="scope.area_code",
                )
            )
        if subject.required_entitlement not in authorization.entitlements:
            violations.append(
                Violation(
                    code=ViolationCode.SUBJECT_NOT_ENTITLED,
                    message=(
                        f"当前用户没有主题 {subject.subject_id} 所需的"
                        f" {subject.required_entitlement} 授权。"
                    ),
                    path="subject",
                )
            )
        if subject.logical_dataset_id not in authorization.datasets:
            violations.append(
                Violation(
                    code=ViolationCode.DATASET_NOT_AUTHORIZED,
                    message=(
                        f"数据集 {subject.logical_dataset_id} 不在当前授权范围内。"
                    ),
                    path="subject",
                )
            )
        if authorization.field_policy_set not in subject.field_policy_sets:
            violations.append(
                Violation(
                    code=ViolationCode.FIELD_POLICY_NOT_AUTHORIZED,
                    message=(
                        f"字段策略集 {authorization.field_policy_set} 无权访问"
                        f"主题 {subject.subject_id} 的字段。"
                    ),
                    path="filters",
                )
            )
        return violations

    @staticmethod
    def _metric_violations(
        spec: SemanticQuerySpec,
        subject: SubjectDefinition,
    ) -> list[Violation]:
        registered = {metric.metric_id for metric in subject.metrics}
        return [
            Violation(
                code=ViolationCode.UNKNOWN_METRIC,
                message=(
                    f"主题 {subject.subject_id} 未注册指标 {metric}；"
                    f"可用：{sorted(registered)}。"
                ),
                path="metrics",
            )
            for metric in spec.metrics
            if metric not in registered
        ]

    @staticmethod
    def _result_shape_violations(
        spec: SemanticQuerySpec,
        subject: SubjectDefinition,
    ) -> list[Violation]:
        requested_group_by = tuple(spec.group_by)
        requested_metrics = tuple(spec.metrics)
        if any(
            (shape.group_by_selection is None or shape.group_by_selection == requested_group_by)
            and (shape.metric_selection is None or shape.metric_selection == requested_metrics)
            for shape in subject.result_shapes
        ):
            return []
        return [
            Violation(
                code=ViolationCode.INVALID_RESULT_SHAPE,
                message=(
                    f"主题 {subject.subject_id} 不支持指标 {list(requested_metrics)} "
                    f"与分组 {list(requested_group_by)} 的组合。"
                ),
                path="metrics",
            )
        ]

    @staticmethod
    def _scope_violations(
        spec: SemanticQuerySpec,
        subject: SubjectDefinition,
    ) -> list[Violation]:
        violations: list[Violation] = []
        scope_level = len(spec.scope.area_code)
        if scope_level not in subject.scope_levels:
            violations.append(
                Violation(
                    code=ViolationCode.SCOPE_LEVEL_UNSUPPORTED,
                    message=(
                        f"主题 {subject.subject_id} 不支持层级 {scope_level}"
                        f"（区划编码 {spec.scope.area_code}）；"
                        f"可用层级：{list(subject.scope_levels)}。"
                    ),
                    path="scope.area_code",
                )
            )
        return violations

    @staticmethod
    def _group_by_violations(
        spec: SemanticQuerySpec,
        subject: SubjectDefinition,
    ) -> list[Violation]:
        violations: list[Violation] = []
        count = len(spec.group_by)
        if count < subject.min_group_by or count > subject.max_group_by:
            violations.append(
                Violation(
                    code=ViolationCode.INVALID_GROUP_BY,
                    message=(
                        f"主题 {subject.subject_id} 的 group_by 数量必须在"
                        f" {subject.min_group_by}..{subject.max_group_by} 之间，"
                        f"实际 {count}。"
                    ),
                    path="group_by",
                )
            )
            return violations
        rules = {rule.value: rule for rule in subject.group_by_rules}
        scope_level = len(spec.scope.area_code)
        for value in spec.group_by:
            rule = rules.get(value)
            if rule is None:
                violations.append(
                    Violation(
                        code=ViolationCode.INVALID_GROUP_BY,
                        message=(
                            f"主题 {subject.subject_id} 未注册分组维度 {value}；"
                            f"可用：{sorted(rules)}。"
                        ),
                        path="group_by",
                    )
                )
            elif scope_level not in rule.allowed_scope_levels:
                violations.append(
                    Violation(
                        code=ViolationCode.GROUP_BY_SCOPE_MISMATCH,
                        message=(
                            f"分组 {value} 不支持层级 {scope_level} 的 scope；"
                            f"该分组仅支持：{list(rule.allowed_scope_levels)}。"
                        ),
                        path="group_by",
                    )
                )
        return violations

    @staticmethod
    def _filter_violations(
        spec: SemanticQuerySpec,
        subject: SubjectDefinition,
    ) -> list[Violation]:
        violations: list[Violation] = []
        registered = {definition.field: definition for definition in subject.filters}
        for index, query_filter in enumerate(spec.filters):
            path = f"filters[{index}]"
            definition = registered.get(query_filter.field)
            if definition is None:
                violations.append(
                    Violation(
                        code=ViolationCode.INVALID_FILTER_FIELD,
                        message=(
                            f"主题 {subject.subject_id} 未注册筛选字段"
                            f" {query_filter.field}；可用：{sorted(registered)}。"
                        ),
                        path=path,
                    )
                )
                continue
            if query_filter.operator not in definition.operators:
                violations.append(
                    Violation(
                        code=ViolationCode.INVALID_FILTER_OPERATOR,
                        message=(
                            f"筛选字段 {query_filter.field} 不支持操作符"
                            f" {query_filter.operator}；"
                            f"可用：{list(definition.operators)}。"
                        ),
                        path=path,
                    )
                )
                continue
            if definition.allowed_values and (
                not isinstance(query_filter.value, str)
                or query_filter.value not in definition.allowed_values
            ):
                violations.append(
                    Violation(
                        code=ViolationCode.INVALID_FILTER_VALUE,
                        message=(
                            f"筛选字段 {query_filter.field} 的值不在白名单"
                            f" {list(definition.allowed_values)} 内。"
                        ),
                        path=path,
                    )
                )
        return violations
