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

可执行主题（``bindable_subject_ids``）同样由 Catalog 派生：只有在目录
声明且存在已验证能力绑定的主题才能进入语义入口解析链路。没有硬编码
主题白名单——绑定增减时可执行集合随之变化；声明存在但缺少绑定的主题
由解析层结构化拒绝（``SUBJECT_NOT_BINDABLE``），不被执行。

已绑定主题统一由 ``semantic_query`` 面向模型承载；规范 Tool 继续保留在
内部 Registry/CapabilityService，供语义编译结果兼容执行，但不并行暴露。
"""

import re
from collections.abc import Mapping
from typing import Literal

from pydantic import Field, field_validator

from full_view_agent.application.authorization_scope import area_is_within_scope
from full_view_agent.application.fingerprints import canonical_fingerprint
from full_view_agent.domain.models import ContractModel, MetricQueryScope
from full_view_agent.semantic.authorization import SubjectAuthorization
from full_view_agent.semantic.errors import UnknownSubjectError

SEMANTIC_CATALOG_VERSION = "0.1.0-s0-candidate"
SEMANTIC_SPEC_VERSIONS: tuple[str, ...] = ("s0.1",)

OutputForm = Literal["table", "choropleth"]

_DEFAULT_RESULT_GRAIN_LABEL = "按所选查询维度返回结果"
_UNSAFE_BUSINESS_LABEL = re.compile(
    r"(?i)(://|[\\/]|[a-z]:|"
    # Python 的 \b 会把下划线当作单词字符，无法阻断 adapter_ref / SQL_token。
    # 这里以 ASCII 字母数字为边界，_ 与 - 均被视作 token 分隔符。
    r"(?<![a-z0-9])(?:https?|adapter(?:[_-]?ref)?|schema(?:[_-]?ref)?|"
    r"select|insert|update|delete|drop|alter|create|from|join|where|table)"
    r"(?![a-z0-9]))"
)
_BUSINESS_LABEL_CHARACTERS = re.compile(
    r"^[\u4e00-\u9fffA-Za-z0-9 _、，。（）()%-]+$"
)


def _validated_business_label(value: str) -> str:
    normalized = value.strip()
    if (
        not normalized
        or _UNSAFE_BUSINESS_LABEL.search(normalized)
        or _BUSINESS_LABEL_CHARACTERS.fullmatch(normalized) is None
    ):
        raise ValueError("grain_label must be safe business display text")
    return normalized


def _safe_model_grain_label(value: str | None) -> str:
    if value is None:
        return _DEFAULT_RESULT_GRAIN_LABEL
    try:
        return _validated_business_label(value)
    except ValueError:
        # Defense in depth for custom Catalog objects created through
        # model_copy/model_construct without Pydantic validation.
        return _DEFAULT_RESULT_GRAIN_LABEL


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


class RequiredFilter(ContractModel):
    """真实数据源强制要求的筛选：缺少即不可执行。

    这是能力事实而非偏好 —— 例如人口 Adapter 白名单只接受
    ``person_category=solitary_elderly``（governance_adapter
    ``_validate_solitary_elderly_query``）。S1-A 语义入口在编译前
    强制校验，缺失时 fail closed，不让无筛选查询打到真实接口后才失败。
    """

    field: str = Field(min_length=1, max_length=64)
    operator: str = Field(min_length=1, max_length=64)
    value: str | int | float | bool


class GroupByRule(ContractModel):
    value: str = Field(min_length=1, max_length=64)
    label: str = Field(min_length=1, max_length=100)
    # 允许该分组的 scope 区划编码长度（市4/区县6/街道9/社区12/网格15）。
    allowed_scope_levels: tuple[int, ...] = Field(min_length=1)


class ResultShape(ContractModel):
    shape_id: str = Field(min_length=1, max_length=64)
    kind: Literal["table"] = "table"
    data_schema_ref: str = Field(min_length=1, max_length=200)
    grain_label: str | None = Field(default=None, max_length=100)
    row_fields: tuple[str, ...] = Field(min_length=1)
    # None = 适用于该主题的所有合法 group_by；元组 = 精确匹配 group_by。
    group_by_selection: tuple[str, ...] | None = None

    @field_validator("grain_label")
    @classmethod
    def validate_grain_label(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _validated_business_label(value)


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
    required_filters: tuple[RequiredFilter, ...] = ()
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


class RequiredFilterSummary(ContractModel):
    """模型可见的强制筛选能力事实。"""

    field: str
    operator: str
    value: str | int | float | bool


class GroupBySummary(ContractModel):
    """模型可见的分组维度摘要：携带适用层级，避免只看到维度名。"""

    value: str
    label: str
    allowed_scope_levels: tuple[int, ...]


class ResultShapeSummary(ContractModel):
    """模型可见的安全结果粒度；不含 schema/adapter 等物理绑定。"""

    group_by_selection: tuple[str, ...] | None
    grain_label: str


class SubjectCapabilityView(ContractModel):
    """模型可见的主题能力摘要：只含业务语义。"""

    subject_id: str
    display_name: str
    # 主题真实数据源支持的 scope 区划编码长度（市4/区县6/街道9/社区12/网格15）。
    scope_levels: tuple[int, ...]
    metrics: tuple[str, ...]
    group_by: tuple[GroupBySummary, ...]
    filters: tuple[FilterSummary, ...]
    required_filters: tuple[RequiredFilterSummary, ...] = ()
    output_forms: tuple[str, ...]
    result_shapes: tuple[ResultShapeSummary, ...]


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
        # 真实 Adapter 白名单只接受 solitary_elderly 独居老人查询；
        # 缺失该筛选时语义入口必须 fail closed（见 action_resolver）。
        required_filters=(
            RequiredFilter(
                field="person_category",
                operator="eq",
                value="solitary_elderly",
            ),
        ),
        output_forms=("table", "choropleth"),
        result_shapes=(
            ResultShape(
                shape_id="population_metric_table",
                data_schema_ref="schema://data/population-metric-table/1.0.0",
                grain_label="按直接下级区划汇总",
                row_fields=("area_code", "area_name", "person_count"),
            ),
        ),
    )


def _housing(*, next_area_enabled: bool = False) -> SubjectDefinition:
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
        )
        if next_area_enabled
        else (),
        min_group_by=0,
        max_group_by=1 if next_area_enabled else 0,
        output_forms=("table",),
        result_shapes=(
            ResultShape(
                shape_id="housing_lease_type_table",
                data_schema_ref="schema://data/housing-lease-type-table/1.0.0",
                grain_label="按租赁类型汇总",
                row_fields=("lease_type", "dwelling_count"),
                group_by_selection=(),
            ),
        )
        + (
            (
                ResultShape(
                    shape_id="housing_area_group_table",
                    data_schema_ref="schema://data/housing-area-group-table/1.0.0",
                    grain_label="按直接下级区划汇总",
                    row_fields=("area_code", "area_name", "dwelling_count"),
                    group_by_selection=("next_area",),
                ),
            )
            if next_area_enabled
            else ()
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
                grain_label="按网格、村社、镇街层级返回办结率快照",
                row_fields=("level", "finish_rate"),
            ),
        ),
    )


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
    def default(
        cls, *, housing_next_area_enabled: bool = False
    ) -> "SemanticCatalog":
        subjects = (_population(), _housing(
            next_area_enabled=housing_next_area_enabled
        ), _event())
        return cls(
            catalog_version=SEMANTIC_CATALOG_VERSION,
            supported_spec_versions=SEMANTIC_SPEC_VERSIONS,
            subjects={subject.subject_id: subject for subject in subjects},
            bindings=dict(_DEFAULT_BINDINGS),
        )

    @property
    def catalog_version(self) -> str:
        return self._catalog_version

    @property
    def execution_fingerprint(self) -> str:
        """Fingerprint every Catalog fact that can change compiled execution."""

        return canonical_fingerprint(
            domain="semantic-catalog-execution:1.0",
            value={
                "catalog_version": self._catalog_version,
                "supported_spec_versions": list(self._supported_spec_versions),
                "subjects": {
                    subject_id: self._execution_subject_payload(
                        self._subjects[subject_id]
                    )
                    for subject_id in sorted(self._subjects)
                },
                "bindings": {
                    subject_id: self._bindings[subject_id].model_dump(mode="json")
                    for subject_id in sorted(self._bindings)
                },
            },
        )

    @staticmethod
    def _execution_subject_payload(
        subject: SubjectDefinition,
    ) -> dict[str, object]:
        payload = subject.model_dump(mode="json")
        result_shapes = payload.get("result_shapes")
        if isinstance(result_shapes, list):
            for shape in result_shapes:
                if isinstance(shape, dict):
                    # Display-only metadata must not invalidate an otherwise
                    # identical compiled semantic action.
                    shape.pop("grain_label", None)
        return payload

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

    def bindable_subject_ids(self) -> frozenset[str]:
        """可执行主题集合：由声明主题与已验证能力绑定取交集派生。

        没有硬编码主题白名单：绑定增减时可执行集合随之变化。声明存在
        但缺少绑定的主题不在集合内，由解析层结构化拒绝
        （``SUBJECT_NOT_BINDABLE``），不被执行。
        """
        return frozenset(set(self._subjects) & set(self._bindings))

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
                                label=rule.label,
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
                        required_filters=tuple(
                            RequiredFilterSummary(
                                field=required_filter.field,
                                operator=required_filter.operator,
                                value=required_filter.value,
                            )
                            for required_filter in subject.required_filters
                        ),
                        output_forms=tuple(subject.output_forms),
                        result_shapes=tuple(
                            ResultShapeSummary(
                                group_by_selection=shape.group_by_selection,
                                grain_label=_safe_model_grain_label(
                                    shape.grain_label
                                ),
                            )
                            for shape in subject.result_shapes
                        ),
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
