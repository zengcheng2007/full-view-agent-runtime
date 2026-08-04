"""Authorization-derived model presentation for regional analysis intents."""

from copy import deepcopy
from dataclasses import dataclass
from typing import cast

from full_view_agent.application.harness import ANALYSIS_INTENT_TOOL_ID
from full_view_agent.domain.analysis_intent import AnalysisIntentV1
from full_view_agent.domain.analysis_shared import ANALYSIS_GOAL_ORDER, AnalysisGoal
from full_view_agent.domain.models import AuthContext
from full_view_agent.semantic.authorization import SubjectAuthorization
from full_view_agent.semantic.catalog import SemanticCatalog


@dataclass(frozen=True)
class AnalysisIntentToolPresentation:
    tool_id: str
    tool_version: str
    description: str
    input_schema: dict[str, object]
    goals: tuple[AnalysisGoal, ...]


class AnalysisIntentToolPresenter:
    """Expose only analysis goals supported by Catalog and current authority."""

    def __init__(self, *, catalog: SemanticCatalog) -> None:
        self._catalog = catalog

    def present(
        self, *, auth_context: AuthContext
    ) -> AnalysisIntentToolPresentation | None:
        authorization = SubjectAuthorization.from_auth_context(auth_context)
        view = self._catalog.model_capability_view(authorization)
        bindable = self._catalog.bindable_subject_ids()
        visible_subjects = {
            subject.subject_id
            for subject in view.subjects
            if subject.subject_id in bindable
        }
        if not visible_subjects:
            return None

        goals = cast(
            tuple[AnalysisGoal, ...],
            tuple(
                goal
                for goal in ANALYSIS_GOAL_ORDER
                if goal == "overview" or goal in visible_subjects
            ),
        )
        schema = deepcopy(AnalysisIntentV1.model_json_schema(mode="validation"))
        properties = schema.get("properties")
        if not isinstance(properties, dict):
            return None
        goals_property = properties.get("goals")
        if not isinstance(goals_property, dict):
            return None
        items = goals_property.get("items")
        if not isinstance(items, dict):
            return None
        items["enum"] = list(goals)

        return AnalysisIntentToolPresentation(
            tool_id=ANALYSIS_INTENT_TOOL_ID,
            tool_version="1.0",
            description=(
                "请求服务端编译并执行区域综合研判。仅可选择当前授权且已绑定的"
                f"研判目标：{', '.join(goals)}；范围只能使用区域名称或当前区域。"
            ),
            input_schema=schema,
            goals=goals,
        )
