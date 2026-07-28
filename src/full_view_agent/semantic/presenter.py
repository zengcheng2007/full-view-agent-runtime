"""S1-A：从权限过滤后的 Catalog 派生 semantic_query 模型可见 Tool。

ADR-05 要求 Tool 描述由经过权限过滤的 Catalog 能力生成，Prompt 不得
手写一份容易漂移的能力清单。本模块按当前 AuthContext 派生
``ModelCapabilityView``（fail closed：无显式授权时没有任何主题可见），
再与 S1-A 可绑定主题取交集；交集为空则虚拟 Tool 对模型不可见。

描述文本中固定部分只有"如何使用语义入口"与层级编码通识；主题、指标、
分组、筛选、输出形态全部逐字段从 Catalog 序列化，Catalog 能力变化时
模型可见面自动同步，不产生第二份真相。
"""

from dataclasses import dataclass

from full_view_agent.domain.models import AuthContext
from full_view_agent.semantic.action_resolver import (
    S1A_BINDABLE_SUBJECTS,
    SEMANTIC_QUERY_TOOL_ID,
    SEMANTIC_QUERY_TOOL_VERSION,
    SemanticQueryInput,
)
from full_view_agent.semantic.authorization import SubjectAuthorization
from full_view_agent.semantic.catalog import (
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


class SemanticToolPresenter:
    def __init__(
        self,
        *,
        catalog: SemanticCatalog,
        bindable_subjects: frozenset[str] = S1A_BINDABLE_SUBJECTS,
    ) -> None:
        self._catalog = catalog
        self._bindable_subjects = frozenset(bindable_subjects)

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
        return SemanticToolPresentation(
            tool_id=SEMANTIC_QUERY_TOOL_ID,
            tool_version=SEMANTIC_QUERY_TOOL_VERSION,
            description=self._build_description(view, bindable),
            input_schema=SemanticQueryInput.model_json_schema(mode="validation"),
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
            "当前所有主题均不支持 order_by 与 time_range。",
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
                f"{item.value}(scope层级{list(item.allowed_scope_levels)})"
                for item in subject.group_by
            )
            or "无"
        )
        filters = (
            "、".join(
                f"{item.field}{list(item.operators)}{list(item.allowed_values)}"
                for item in subject.filters
            )
            or "无"
        )
        return (
            f"- {subject.subject_id}（{subject.display_name}）："
            f"scope层级 {list(subject.scope_levels)}；"
            f"指标 {list(subject.metrics)}；"
            f"group_by {group_by}；"
            f"filters {filters}；"
            f"输出形态 {list(subject.output_forms)}。"
        )
