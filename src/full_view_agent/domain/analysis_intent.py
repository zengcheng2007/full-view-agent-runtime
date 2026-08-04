"""P2 受控研判意图契约：模型可提交的唯一结构化输入。

自然语言 Agent 只能通过 ``AnalysisIntentV1`` 表达"分析哪些主题、范围如何
确定"，不得提交计划步骤、SQL、URL、adapter 绑定、预算或可信区划编码——
这些全部由服务端在编译时生成（见
``application.analysis_intent_service.AnalysisIntentCompilationService``）：

- ``kind`` 固定 ``regional_analysis``；``schema_version`` 固定 1.0；
- ``goals`` 只取 ``AnalysisGoal`` 受限词表，非空、有界、去重并规范为
  词表顺序（与 AnalysisPlanner 的规范顺序一致，保证意图指纹稳定）；
- ``scope`` 仅允许 ``named_area``（必须携带 ``area_query``，由服务端区划
  解析器解析）/ ``current_area``（意图中不允许出现任何区划编码；当前区划
  只能由服务端上下文以独立入参另传）；
- ``extra="forbid"``：steps/sql/url/adapter/budget/area_code 等敏感字段
  在边界即被拒绝。
"""

from typing import Annotated, Literal

from pydantic import ConfigDict, Field, field_validator

from full_view_agent.domain.analysis_shared import ANALYSIS_GOAL_ORDER, AnalysisGoal
from full_view_agent.domain.contract_model import ContractModel

ANALYSIS_INTENT_SCHEMA_VERSION = "1.0"


class NamedAreaScopeIntent(ContractModel):
    """按名称指定区划：只携带区划名称文本，解析在服务端完成。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["named_area"] = "named_area"
    area_query: str = Field(min_length=1, max_length=100)


class CurrentAreaScopeIntent(ContractModel):
    """使用当前区划：刻意不携带任何字段。

    当前区划的可信编码不允许由模型提交，只能来自 run/session 上下文、由
    服务端以独立入参传入编译服务。配合 ``extra="forbid"``，任何试图在
    意图中夹带 ``area_code`` 的载荷都会被拒绝。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["current_area"] = "current_area"


AnalysisIntentScope = Annotated[
    NamedAreaScopeIntent | CurrentAreaScopeIntent,
    Field(discriminator="kind"),
]


class AnalysisIntentV1(ContractModel):
    """受控研判意图（模型 → 服务端）。

    只表达"主题集合 + 范围如何确定"，不表达任何可执行细节；编译为可信
    ``AnalysisRequest`` 并复用现有规划服务形成可信计划的过程全部在
    服务端完成。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0"] = ANALYSIS_INTENT_SCHEMA_VERSION
    kind: Literal["regional_analysis"] = "regional_analysis"
    goals: tuple[AnalysisGoal, ...] = Field(min_length=1, max_length=4)
    scope: AnalysisIntentScope

    @field_validator("goals")
    @classmethod
    def _canonicalize_goals(
        cls, goals: tuple[AnalysisGoal, ...]
    ) -> tuple[AnalysisGoal, ...]:
        # 与 AnalysisPlanner 的 goals 规范一致：去重并规范为词表顺序，
        # 表达顺序不同但语义相同的意图具有同一幂等指纹。
        requested = set(goals)
        return tuple(goal for goal in ANALYSIS_GOAL_ORDER if goal in requested)
