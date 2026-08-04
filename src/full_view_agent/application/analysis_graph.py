"""Framework-neutral port and checkpoint-safe contracts for analysis runs."""

from typing import Literal, Protocol

from pydantic import ConfigDict, Field

from full_view_agent.domain.models import AuthContext, ContractModel


class AnalysisGraphPreparation(ContractModel):
    """Safe scheduling metadata; the trusted plan body stays in its repository."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    expected_step_count: int = Field(ge=0, le=64)
    max_parallel: int = Field(ge=1, le=8)


class AnalysisStepCheckpoint(ContractModel):
    """Minimal durable step state; result payloads stay in the Result Store."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    step_id: str = Field(min_length=1, max_length=128)
    status: Literal["success", "partial", "denied", "failed", "timeout", "skipped"]
    reason_code: str = Field(min_length=1, max_length=128)
    result_id: str | None = Field(default=None, min_length=1, max_length=128)
    evidence_ids: tuple[str, ...] = Field(default=(), max_length=64)


class AnalysisRunOutcome(ContractModel):
    """Terminal reference to a report already written to the Result Store."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    analysis_run_id: str = Field(min_length=1, max_length=128)
    plan_id: str = Field(min_length=1, max_length=128)
    request_id: str = Field(min_length=1, max_length=128)
    status: Literal["completed", "partial", "failed"]
    reason_code: str = Field(min_length=1, max_length=128)
    report_result_id: str | None = Field(default=None, min_length=1, max_length=128)


class AnalysisGraphExecutionPort(Protocol):
    """Idempotent application service used by Native/LangGraph adapters.

    ``execute_step`` must use a deterministic step call identity and persist its
    Result before returning. Repeating it after a crash therefore returns the
    same reference instead of invoking the upstream capability twice.
    """

    async def prepare(
        self,
        *,
        plan_id: str,
        request_id: str,
        auth_context: AuthContext,
    ) -> AnalysisGraphPreparation: ...

    async def ready(
        self,
        *,
        plan_id: str,
        request_id: str,
        completed: tuple[AnalysisStepCheckpoint, ...],
        auth_context: AuthContext,
    ) -> tuple[str, ...]: ...

    async def execute_step(
        self,
        *,
        plan_id: str,
        request_id: str,
        step_id: str,
        auth_context: AuthContext,
    ) -> AnalysisStepCheckpoint: ...

    async def finalize(
        self,
        *,
        analysis_run_id: str,
        plan_id: str,
        request_id: str,
        completed: tuple[AnalysisStepCheckpoint, ...],
        auth_context: AuthContext,
    ) -> AnalysisRunOutcome: ...
