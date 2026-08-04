"""区域研判的确定性、引用式结果契约。

本契约只描述服务端已经执行完成的计划和子结果引用，不承载原始数据行、
模型生成文案、因果判断或处置建议。后续派生指标与证据图必须在新版本中由
确定性服务实现；1.0 版本显式保留空扩展位，避免调用方误以为已经实现。

``AnalysisReportDataResult`` 是 ``domain.models.DataResult`` union 的
``analysis_report`` 成员：envelope 字段与既有结果同形，身份
（result_id/result_fingerprint）由服务端按确定性内容计算，因此报告
可以经既有 AgentStore 保存、读取与解码。本模块只依赖叶子层
（``contract_model`` / ``analysis_shared``），避免与 ``domain.models``
循环导入。
"""

from datetime import UTC, datetime, timedelta
from typing import Literal

from pydantic import ConfigDict, Field, model_validator

from full_view_agent.domain.analysis_shared import (
    AnalysisExecutionStatus,
    AnalysisOmission,
    AnalysisStepExecutionStatus,
)
from full_view_agent.domain.contract_model import ContractModel


class AnalysisChildResultRef(ContractModel):
    """已校验子 DataResult 的最小引用，不复制 payload。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    result_id: str = Field(min_length=1, max_length=128)
    result_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    kind: Literal["table"]
    data_schema_ref: str = Field(min_length=1, max_length=200)


class AnalysisReportSection(ContractModel):
    """与计划步骤一一对应的主题节。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    step_id: str = Field(min_length=1, max_length=128)
    subject: str = Field(min_length=1, max_length=64)
    status: AnalysisStepExecutionStatus
    reason_code: str = Field(min_length=1, max_length=128)
    result_ref: AnalysisChildResultRef | None = None

    @model_validator(mode="after")
    def _validate_result_reference(self) -> "AnalysisReportSection":
        usable = self.status in {"success", "partial"}
        if usable != (self.result_ref is not None):
            raise ValueError(
                "result reference is required exactly for success/partial sections"
            )
        return self


class AnalysisReportLimitation(ContractModel):
    """非 success 步骤的结构化限制，不把失败解释成零值。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    step_id: str = Field(min_length=1, max_length=128)
    subject: str = Field(min_length=1, max_length=64)
    status: Literal["partial", "denied", "failed", "timeout", "skipped"]
    reason_code: str = Field(min_length=1, max_length=128)


class AnalysisReportExtensions(ContractModel):
    """未来确定性派生指标/证据图的版本化扩展位；1.0 必须为空。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    derived_metric_refs: tuple[str, ...] = Field(default=(), max_length=0)
    evidence_graph_refs: tuple[str, ...] = Field(default=(), max_length=0)


class AnalysisReportDataResult(ContractModel):
    """框架无关的区域研判结构化结果（DataResult union 成员）。

    envelope 字段与既有 DataResult 同形：``result_id`` 与
    ``result_fingerprint`` 由组装器按报告确定性内容计算，创建/过期时间
    不参与身份计算。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    result_id: str = Field(min_length=1, max_length=128)
    kind: Literal["analysis_report"] = "analysis_report"
    data_schema_ref: str = Field(min_length=1, max_length=200)
    result_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    payload_status: Literal["available", "expired"] = "available"
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    payload_expires_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC) + timedelta(hours=24)
    )
    evidence_ids: list[str] = Field(default_factory=list)
    inline: bool = True

    schema_version: Literal["1.0"] = "1.0"
    plan_id: str = Field(min_length=1, max_length=128)
    request_id: str = Field(min_length=1, max_length=128)
    status: AnalysisExecutionStatus
    reason_code: str = Field(min_length=1, max_length=128)
    sections: tuple[AnalysisReportSection, ...]
    omissions: tuple[AnalysisOmission, ...] = ()
    limitations: tuple[AnalysisReportLimitation, ...] = ()
    extensions: AnalysisReportExtensions = Field(default_factory=AnalysisReportExtensions)

    @model_validator(mode="after")
    def _validate_report_consistency(self) -> "AnalysisReportDataResult":
        step_ids = [section.step_id for section in self.sections]
        subjects = [section.subject for section in self.sections]
        if len(step_ids) != len(set(step_ids)):
            raise ValueError("report contains duplicate section step ids")
        if len(subjects) != len(set(subjects)):
            raise ValueError("report contains duplicate section subjects")
        if set(subjects) & {omission.subject for omission in self.omissions}:
            raise ValueError("a report subject cannot be both executed and omitted")

        if (
            self.sections
            and all(section.status == "success" for section in self.sections)
            and not self.omissions
        ):
            expected_status: AnalysisExecutionStatus = "completed"
        elif any(
            section.status in {"success", "partial"} for section in self.sections
        ):
            expected_status = "partial"
        else:
            expected_status = "failed"
        expected_reason = f"ANALYSIS_{expected_status.upper()}"
        if self.status != expected_status or self.reason_code != expected_reason:
            raise ValueError("report overall status is inconsistent with its sections")

        expected_limitations = tuple(
            AnalysisReportLimitation(
                step_id=section.step_id,
                subject=section.subject,
                status=section.status,
                reason_code=section.reason_code,
            )
            for section in self.sections
            if section.status != "success"
        )
        if self.limitations != expected_limitations:
            raise ValueError("report limitations must exactly match non-success sections")
        return self
