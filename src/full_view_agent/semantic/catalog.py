"""S0 语义内核候选：版本化语义目录（离线候选，不接入生产 Tool Registry）。

Catalog 只声明当前生产 HTTP Adapter 已逐项验证的能力，证据来源：
- ``infrastructure/governance_adapter.py`` 的真实端点映射与白名单校验
  （``_validate_solitary_elderly_query`` / ``_validate_housing_next_area_query``）；
- ``application/tool_registry.py`` 的生产 manifest（dataset、权限、adapter 绑定）；
- ``tests/test_http_governance_adapter.py``、``tests/test_production_wiring.py``
  与 ``evals/cases`` 中的契约/接线用例。

真实接口未验证的能力一律不声明：population 的 gender/age_band、housing 的
筛选/排序、event 的时间范围/事件总量/办结数/下级区划明细/阈值筛选。

模型可见能力视图（``model_capability_view``）由 Catalog 经权限过滤派生，
只含业务语义；物理 adapter 绑定只存在于内部 ``CapabilityBinding``，
从不出现在模型可见面的序列化中。视图必须 fail closed：必须由显式
``SubjectAuthorization`` 派生，未授权（None）时不展示任何主题；主题
可见性除授权面/数据集/字段策略外，还要求授权区域至少存在一个可支持
的 scope（授权区域本级在主题 scope_levels，或含下级且其下存在受支持
层级），避免模型看到无法真正查询的主题。
"""

from collections.abc import Mapping
from typing import Literal

from pydantic import Field

from full_view_agent.application.authorization_scope import area_is_within_scope
from full_view_agent.domain.models import ContractModel, MetricQueryScope
from full_view_agent.semantic.authorization import SubjectAuthorization
from full_view_agent.semantic.errors import UnknownSubjectError

SEMANTIC_CATALOG_VERSION = "0.1.0-s0-candidate"
SEMANTIC_SPEC_VERSIONS: tuple[str, ...] = ("s0.1",)

OutputForm = Literal["table", "choropleth"]


class MetricDefinition(ContractModel):
    metric_id: str = Field(min_length=1, max_length=64)
    label: str = Field(min_length=1, max_length=100)
    unit: str = Field(default="count", max_length=20)
    definition_version: str = "1.0"


class FilterDefinition(ContractModel):
    field: str = Field(min_length=1, max_length=64)
    label: str = Field(min_length=1, max_length=100)
    operators: tuple[str, ...] = Field(min_length=1)
    allowed_values: tuple[str, ...] = ()


class GroupByRule(ContractModel):
    value: str = Field(min_length=1, max_length=64)
    label: str = Field(min_length=1, max_length=100)
    # 允许该分组的 scope 区划编码长度（市4/区县6/街道9/社区12/网格15）。
    allowed_scope_levels: tuple[int, ...] = Field(min_length=1)


class ResultShape(ContractModel):
    shape_id: str = Field(min_length=1, max_length=64)
    kind: Literal["table"] = "table"
    data_schema_ref: str = Field(min_length=1, max_length=200)
    row_fields: tuple[str, ...] = Field(min_length=1)
    # None = 适用于该主题的所有合法 group_by；元组 = 精确匹配 group_by。
    group_by_selection: tuple[str, ...] | None = None


class SubjectDefinition(ContractModel):
    subject_id: Literal["population", "housing", "event"]
    display_name: str = Field(min_length=1, max_length=100)
    logical_dataset_id: str = Field(min_length=1, max_length=64)
    required_entitlement: str = Field(min_length=1, max_length=128)
    field_policy_sets: tuple[str, ...] = ("governance_analyst_v1",)
    scope_levels: tuple[int, ...] = Field(min_length=1)
    metrics: tuple[MetricDefinition, ...] = Field(min_length=1)
    group_by_rules: tuple[GroupByRule, ...] = ()
    min_group_by: int = Field(default=0, ge=0, le=2)
    max_group_by: int = Field(default=0, ge=0, le=2)
    filters: tuple[FilterDefinition, ...] = ()
    supports_order_by: bool = False
    supports_time_range: bool = False
    output_forms: tuple[OutputForm, ...] = ("table",)
    result_shapes: tuple[ResultShape, ...] = Field(min_length=1)


class CapabilityBinding(ContractModel):
    """内部边界：主题 → 已验证生产能力的映射，模型不可见。"""

    capability_id: str = Field(min_length=1, max_length=128)
    capability_version: str = Field(min_length=1, max_length=32)
    adapter_ref: str = Field(min_length=1, max_length=200)


class FilterSummary(ContractModel):
    field: str
    label: str
    operators: tuple[str, ...]
    allowed_values: tuple[str, ...]


class GroupBySummary(ContractModel):
    """模型可见的分组维度摘要：携带适用层级，避免只看到维度名。"""

    value: str
    allowed_scope_levels: tuple[int, ...]


class SubjectCapabilityView(ContractModel):
    """模型可见的主题能力摘要：只含业务语义。"""

    subject_id: str
    display_name: str
    # 主题真实数据源支持的 scope 区划编码长度（市4/区县6/街道9/社区12/网格15）。
    scope_levels: tuple[int, ...]
    metrics: tuple[str, ...]
    group_by: tuple[GroupBySummary, ...]
    filters: tuple[FilterSummary, ...]
    output_forms: tuple[str, ...]


class ModelCapabilityView(ContractModel):
    catalog_version: str
    spec_version: str
    subjects: tuple[SubjectCapabilityView, ...]


def _population() -> SubjectDefinition:
    return SubjectDefinition(
        subject_id="population",
        display_name="人口指标",
        logical_dataset_id="population",
        required_entitlement="governance.population.aggregate.read",
        # 真实 Adapter 仅支持区县(6)/街道(9)/社区(12)三级 scope 的独居老人
        # 直接下级聚合（governance_adapter._validate_solitary_elderly_query）。
        scope_levels=(6, 9, 12),
        metrics=(
            MetricDefinition(metric_id="person_count", label="人数", unit="人"),
        ),
        group_by_rules=(
            GroupByRule(value="street", label="街道", allowed_scope_levels=(6,)),
            GroupByRule(value="community", label="社区", allowed_scope_levels=(9,)),
            GroupByRule(value="grid", label="网格", allowed_scope_levels=(12,)),
        ),
        min_group_by=1,
        max_group_by=1,
        filters=(
            FilterDefinition(
                field="person_category",
                label="人口类别",
                operators=("eq",),
                allowed_values=("solitary_elderly",),
            ),
        ),
        output_forms=("table", "choropleth"),
        result_shapes=(
            ResultShape(
                shape_id="population_metric_table",
                data_schema_ref="schema://data/population-metric-table/1.0.0",
                row_fields=("area_code", "area_name", "person_count"),
            ),
        ),
    )


def _housing() -> SubjectDefinition:
    return SubjectDefinition(
        subject_id="housing",
        display_name="出租房指标",
        logical_dataset_id="housing",
        required_entitlement="governance.housing.aggregate.read",
        # 租赁类型汇总走 /house/getRoomLeaseType；next_area 与人口共用
        # /getNextSiteData（base_room_lease），仅支持市/区县/街道/社区 scope
        # （governance_adapter._validate_housing_next_area_query）。
        scope_levels=(4, 6, 9, 12, 15),
        metrics=(
            MetricDefinition(metric_id="dwelling_count", label="出租房数量", unit="套"),
        ),
        group_by_rules=(
            GroupByRule(
                value="next_area",
                label="直接下级区划",
                allowed_scope_levels=(4, 6, 9, 12),
            ),
        ),
        min_group_by=0,
        max_group_by=1,
        output_forms=("table",),
        result_shapes=(
            ResultShape(
                shape_id="housing_lease_type_table",
                data_schema_ref="schema://data/housing-lease-type-table/1.0.0",
                row_fields=("lease_type", "dwelling_count"),
                group_by_selection=(),
            ),
            ResultShape(
                shape_id="housing_area_group_table",
                data_schema_ref="schema://data/housing-area-group-table/1.0.0",
                row_fields=("area_code", "area_name", "dwelling_count"),
                group_by_selection=("next_area",),
            ),
        ),
    )


def _event() -> SubjectDefinition:
    return SubjectDefinition(
        subject_id="event",
        display_name="网格事件指标",
        logical_dataset_id="event",
        required_entitlement="governance.event.aggregate.read",
        # 真实接口只返回区域自身的网格/社区/街道三层办结率快照：
        # 无下级区划明细、无事件总量/办结数、无时间范围、无阈值筛选。
        scope_levels=(4, 6, 9, 12, 15),
        metrics=(
            MetricDefinition(metric_id="finish_rate", label="办结率", unit="%"),
        ),
        min_group_by=0,
        max_group_by=0,
        output_forms=("table",),
        result_shapes=(
            ResultShape(
                shape_id="event_finish_rate_table",
                data_schema_ref="schema://data/event-finish-rate-table/1.0.0",
                row_fields=("level", "finish_rate"),
            ),
        ),
    )


_DEFAULT_SUBJECTS: tuple[SubjectDefinition, ...] = (_population(), _housing(), _event())

_DEFAULT_BINDINGS: tuple[tuple[str, CapabilityBinding], ...] = (
    (
        "population",
        CapabilityBinding(
            capability_id="governance.query_population_metrics",
            capability_version="1.0.0",
            adapter_ref="adapter://geo-qxst/population-metrics/1.0",
        ),
    ),
    (
        "housing",
        CapabilityBinding(
            capability_id="governance.query_housing_metrics",
            capability_version="1.0.0",
            adapter_ref="adapter://geo-qxst/housing-metrics/1.0",
        ),
    ),
    (
        "event",
        CapabilityBinding(
            capability_id="governance.query_event_metrics",
            capability_version="1.0.0",
            adapter_ref="adapter://geo-qxst/event-metrics/1.0",
        ),
    ),
)


class SemanticCatalog:
    """只读语义目录；内部绑定不进入模型可见序列化。"""

    def __init__(
        self,
        *,
        catalog_version: str,
        supported_spec_versions: tuple[str, ...],
        subjects: Mapping[str, SubjectDefinition],
        bindings: Mapping[str, CapabilityBinding],
    ) -> None:
        unknown_bindings = set(bindings) - set(subjects)
        if unknown_bindings:
            raise ValueError(f"bindings reference unknown subjects: {unknown_bindings}")
        for subject in subjects.values():
            if not subject.result_shapes:
                raise ValueError(f"subject {subject.subject_id} has no result shape")
        self._catalog_version = catalog_version
        self._supported_spec_versions = tuple(supported_spec_versions)
        self._subjects = dict(subjects)
        self._bindings = dict(bindings)

    @classmethod
    def default(cls) -> "SemanticCatalog":
        return cls(
            catalog_version=SEMANTIC_CATALOG_VERSION,
            supported_spec_versions=SEMANTIC_SPEC_VERSIONS,
            subjects={subject.subject_id: subject for subject in _DEFAULT_SUBJECTS},
            bindings=dict(_DEFAULT_BINDINGS),
        )

    @property
    def catalog_version(self) -> str:
        return self._catalog_version

    @property
    def supported_spec_versions(self) -> tuple[str, ...]:
        return self._supported_spec_versions

    @property
    def subjects(self) -> dict[str, SubjectDefinition]:
        return dict(self._subjects)

    @property
    def bindings(self) -> dict[str, CapabilityBinding]:
        return dict(self._bindings)

    def subject_ids(self) -> list[str]:
        return sorted(self._subjects)

    def subject(self, subject_id: str) -> SubjectDefinition | None:
        return self._subjects.get(subject_id)

    def require_subject(self, subject_id: str) -> SubjectDefinition:
        subject = self._subjects.get(subject_id)
        if subject is None:
            raise UnknownSubjectError(f"subject not in catalog: {subject_id}")
        return subject

    def binding(self, subject_id: str) -> CapabilityBinding | None:
        return self._bindings.get(subject_id)

    def is_subject_visible(
        self,
        subject: SubjectDefinition,
        authorization: SubjectAuthorization | None,
    ) -> bool:
        # Fail closed：没有显式授权时任何主题都不可见。
        if authorization is None:
            return False
        return (
            subject.required_entitlement in authorization.entitlements
            and subject.logical_dataset_id in authorization.datasets
            and authorization.field_policy_set in subject.field_policy_sets
            and self._authorization_can_reach_a_supported_scope(subject, authorization)
        )

    @staticmethod
    def _authorization_can_reach_a_supported_scope(
        subject: SubjectDefinition,
        authorization: SubjectAuthorization,
    ) -> bool:
        """授权区域至少存在一个主题可支持的 scope。

        区域编码长度即层级（市4/区县6/街道9/社区12/网格15）。授权区域
        本级在主题 scope_levels 内，或 include_descendants 且其下（更深
        层级）存在受支持层级时，该主题对模型可见。
        """
        for area in authorization.area_scopes:
            level = len(area.area_code)
            if level in subject.scope_levels:
                return True
            if area.include_descendants and any(
                supported > level for supported in subject.scope_levels
            ):
                return True
        return False

    def model_capability_view(
        self,
        authorization: SubjectAuthorization | None,
    ) -> ModelCapabilityView:
        """模型可见能力视图：必须由显式授权派生；None 时 fail closed。"""
        views: list[SubjectCapabilityView] = []
        if authorization is not None:
            for subject_id in self.subject_ids():
                subject = self._subjects[subject_id]
                if not self.is_subject_visible(subject, authorization):
                    continue
                views.append(
                    SubjectCapabilityView(
                        subject_id=subject.subject_id,
                        display_name=subject.display_name,
                        scope_levels=subject.scope_levels,
                        metrics=tuple(
                            metric.metric_id for metric in subject.metrics
                        ),
                        group_by=tuple(
                            GroupBySummary(
                                value=rule.value,
                                allowed_scope_levels=rule.allowed_scope_levels,
                            )
                            for rule in subject.group_by_rules
                        ),
                        filters=tuple(
                            FilterSummary(
                                field=filter_definition.field,
                                label=filter_definition.label,
                                operators=filter_definition.operators,
                                allowed_values=filter_definition.allowed_values,
                            )
                            for filter_definition in subject.filters
                        ),
                        output_forms=tuple(subject.output_forms),
                    )
                )
        return ModelCapabilityView(
            catalog_version=self._catalog_version,
            spec_version=self._supported_spec_versions[-1],
            subjects=tuple(views),
        )


def area_is_authorized(
    scope: MetricQueryScope,
    authorization: SubjectAuthorization,
) -> bool:
    """与生产 Policy 相同的区域前缀规则（含 include_descendants）。"""
    return any(
        area_is_within_scope(
            scope.area_code,
            MetricQueryScope(
                area_code=authorized.area_code,
                include_descendants=authorized.include_descendants,
            ),
        )
        for authorized in authorization.area_scopes
    )
