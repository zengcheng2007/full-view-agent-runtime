"""S1-A：从权限过滤后的 Catalog 派生 semantic_query 模型可见 Tool。

ADR-05 要求 Tool 描述由经过权限过滤的 Catalog 能力生成，Prompt 不得
手写一份容易漂移的能力清单。本模块按当前 AuthContext 派生
``ModelCapabilityView``（fail closed：无显式授权时没有任何主题可见），
再与 Catalog 绑定派生的可执行主题取交集；交集为空则虚拟 Tool 对模型
不可见。

描述文本中固定部分只有"如何使用语义入口"与层级编码通识；主题、指标、
分组、筛选、输出形态全部逐字段从 Catalog 序列化，Catalog 能力变化时
模型可见面自动同步，不产生第二份真相。
"""

from dataclasses import dataclass

from full_view_agent.domain.models import AuthContext
from full_view_agent.semantic.action_resolver import (
    SEMANTIC_QUERY_TOOL_ID,
    SEMANTIC_QUERY_TOOL_VERSION,
    SemanticQueryInput,
)
from full_view_agent.semantic.authorization import SubjectAuthorization
from full_view_agent.semantic.catalog import (
    ResultShapeSummary,
    SemanticCatalog,
    SubjectCapabilityView,
)

# 区划编码长度即层级 —— 系统通识，不属于某个主题的能力清单。
_SCOPE_LEVEL_LEGEND = "市4/区县6/街道9/社区12/网格15"


@dataclass(frozen=True)
class SemanticToolPresentation:
    tool_id: str
    tool_version: str
    description: str
    input_schema: dict[str, object]
    server_arguments: dict[str, object]
    # Canonical capabilities represented by this model-facing semantic Tool.
    # They remain executable internally but must not be advertised in parallel,
    # otherwise the model can bypass Catalog validation or execute twice.
    shadowed_tool_ids: tuple[str, ...]


class SemanticToolPresenter:
    def __init__(
        self,
        *,
        catalog: SemanticCatalog,
    ) -> None:
        self._catalog = catalog

    @property
    def _bindable_subjects(self) -> frozenset[str]:
        """可执行主题：由 Catalog 能力绑定派生，无硬编码白名单。"""
        return self._catalog.bindable_subject_ids()

    @property
    def shadowed_tool_ids(self) -> tuple[str, ...]:
        """Canonical Tools owned by this model-facing semantic entry.

        Every bound subject is exposed to the model only through the semantic
        entry. Canonical Tools remain registered and internally executable,
        but are never advertised in parallel. This set is independent of the
        current authorization so incomplete policy fails closed rather than
        falling back to a bypass path.
        """

        return tuple(
            sorted(
                binding.capability_id
                for subject_id in self._catalog.bindable_subject_ids()
                if (binding := self._catalog.binding(subject_id)) is not None
            )
        )

    def present(self, *, auth_context: AuthContext) -> SemanticToolPresentation | None:
        authorization = SubjectAuthorization.from_auth_context(auth_context)
        view = self._catalog.model_capability_view(authorization)
        bindable = [
            subject
            for subject in view.subjects
            if subject.subject_id in self._bindable_subjects
        ]
        if not bindable:
            return None
        input_schema = SemanticQueryInput.model_json_schema(mode="validation")
        properties = input_schema.get("properties")
        if isinstance(properties, dict):
            properties.pop("catalog_version", None)
            properties.pop("catalog_fingerprint", None)
        required = input_schema.get("required")
        if isinstance(required, list):
            input_schema["required"] = [
                item
                for item in required
                if item not in {"catalog_version", "catalog_fingerprint"}
            ]
        return SemanticToolPresentation(
            tool_id=SEMANTIC_QUERY_TOOL_ID,
            tool_version=SEMANTIC_QUERY_TOOL_VERSION,
            description=self._build_description(view, bindable),
            input_schema=input_schema,
            server_arguments={
                "catalog_version": self._catalog.catalog_version,
                "catalog_fingerprint": self._catalog.execution_fingerprint,
            },
            shadowed_tool_ids=self.shadowed_tool_ids,
        )

    def _build_description(
        self,
        view: object,
        bindable: list[SubjectCapabilityView],
    ) -> str:
        catalog_version = self._catalog.catalog_version
        spec_version = self._catalog.supported_spec_versions[-1]
        lines = [
            "semantic_query：语义查询统一入口。用受控 spec 表达查询意图"
            "（subject/metrics/scope/group_by/filters/output），运行时解析到"
            "已验证的生产能力并按实际主题复核数据集、字段与区域权限。",
            "区划编码长度即层级（" + _SCOPE_LEVEL_LEGEND + "）；scope.area_code"
            " 必须是授权范围内的标准区划编码。",
            "不得生成目录未声明的指标、维度、筛选字段、操作符或输出形态；"
            "当前所有主题均不支持 order_by 与 time_range。用户要求目录未声明的"
            "指标、维度或筛选时，不得改用更宽口径查询替代；应以 capability 完成"
            "并明确说明当前能力边界。",
            f"当前语义目录（catalog_version={catalog_version}，"
            f"spec_version={spec_version}），本次授权可用主题：",
        ]
        for subject in bindable:
            lines.append(self._describe_subject(subject))
        return "".join(lines)

    @staticmethod
    def _describe_subject(subject: SubjectCapabilityView) -> str:
        group_by = (
            "、".join(
                f"{item.value}（{item.label}）"
                f"(scope层级{list(item.allowed_scope_levels)})"
                for item in subject.group_by
            )
            or "无"
        )
        result_grains = "；".join(
            SemanticToolPresenter._describe_result_shape(shape)
            for shape in subject.result_shapes
        )
        filters = (
            "、".join(
                f"{item.field}{list(item.operators)}{list(item.allowed_values)}"
                for item in subject.filters
            )
            or "无"
        )
        required_filters = "、".join(
            f"{item.field} {item.operator} {item.value}"
            for item in subject.required_filters
        )
        required_filter_description = (
            f"必填筛选 {required_filters}（查询必须携带，否则拒绝执行）；"
            if required_filters
            else ""
        )
        return (
            f"- {subject.subject_id}（{subject.display_name}）："
            f"scope层级 {list(subject.scope_levels)}；"
            f"指标 {list(subject.metrics)}；"
            f"group_by {group_by}；"
            f"filters {filters}；"
            f"{required_filter_description}"
            f"输出形态 {list(subject.output_forms)}；"
            f"结果粒度 {result_grains}。"
        )

    @staticmethod
    def _describe_result_shape(shape: ResultShapeSummary) -> str:
        selection = shape.group_by_selection
        if selection == ():
            group_by = "group_by=[]（不传 group_by）"
        elif selection is None:
            group_by = "任一已声明 group_by"
        else:
            group_by = f"group_by={list(selection)}"
        return (
            f"{group_by} -> {shape.grain_label}"
        )
