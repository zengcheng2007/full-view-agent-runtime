"""区域研判契约共享的最小词表与 omission 契约。

叶子层：只依赖 ``contract_model``。计划、执行与报告契约都从这里取
状态词表与 ``AnalysisOmission``，``domain.models`` 因此可以在不形成
循环导入的前提下把 ``AnalysisReportDataResult`` 纳入 DataResult union。
"""

from typing import Literal, get_args

from pydantic import ConfigDict, Field

from full_view_agent.domain.contract_model import ContractModel

# 研判目标词表；overview 由 SemanticCatalog 派生主题。
AnalysisGoal = Literal["overview", "population", "housing", "event"]

ANALYSIS_GOAL_ORDER: tuple[AnalysisGoal, ...] = get_args(AnalysisGoal)

# omission 稳定原因码；只允许服务端受控集合。
OmissionReasonCode = Literal[
    "SUBJECT_NOT_BINDABLE",
    "SCOPE_KIND_UNSUPPORTED",
    "NOT_ENTITLED",
    "DATASET_NOT_AUTHORIZED",
    "FIELD_POLICY_MISMATCH",
    "AREA_NOT_AUTHORIZED",
    "SCOPE_LEVEL_UNSUPPORTED",
    "BUDGET_TOOL_CALLS_EXCEEDED",
]

# 执行终态词表；计划执行与报告共用。
AnalysisExecutionStatus = Literal["completed", "partial", "failed"]
AnalysisStepExecutionStatus = Literal[
    "success", "partial", "denied", "failed", "timeout", "skipped"
]


class AnalysisOmission(ContractModel):
    """计划 omission：不可执行主题的受控说明。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    goals: tuple[AnalysisGoal, ...] = Field(min_length=1, max_length=4)
    subject: str = Field(min_length=1, max_length=64)
    reason_code: OmissionReasonCode
    detail: str = Field(min_length=1, max_length=500)
