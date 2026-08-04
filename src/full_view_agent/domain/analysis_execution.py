"""区域研判计划的框架无关执行结果契约。"""

from pydantic import ConfigDict, Field

from full_view_agent.domain.analysis_plan import AnalysisOmission
from full_view_agent.domain.analysis_shared import (
    AnalysisExecutionStatus as AnalysisExecutionStatus,
)
from full_view_agent.domain.analysis_shared import (
    AnalysisStepExecutionStatus as AnalysisStepExecutionStatus,
)
from full_view_agent.domain.models import ContractModel, ToolResult


class AnalysisStepExecution(ContractModel):
    """单个计划步骤的结构化结果；不用异常文本猜测状态。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    step_id: str = Field(min_length=1, max_length=128)
    subject: str = Field(min_length=1, max_length=64)
    status: AnalysisStepExecutionStatus
    reason_code: str = Field(min_length=1, max_length=128)
    detail: str = Field(min_length=1, max_length=500)
    tool_result: ToolResult | None = None


class AnalysisExecutionResult(ContractModel):
    """计划执行终态；步骤顺序永远与 AnalysisPlan 一致。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    plan_id: str = Field(min_length=1, max_length=128)
    request_id: str = Field(min_length=1, max_length=128)
    status: AnalysisExecutionStatus
    reason_code: str = Field(min_length=1, max_length=128)
    steps: tuple[AnalysisStepExecution, ...]
    omissions: tuple[AnalysisOmission, ...] = ()
    tool_call_count: int = Field(ge=0)
