"""Framework-neutral, checkpoint-safe contracts for analysis execution."""

from contextlib import AbstractAsyncContextManager
from datetime import datetime
from typing import Literal, Protocol

from pydantic import ConfigDict, Field

from full_view_agent.domain.models import AuthContext, ContractModel

ANALYSIS_GRAPH_STATE_VERSION = "1.0"


class AnalysisGraphPreparation(ContractModel):
    """Scheduling budgets derived from a trusted server-side plan."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    expected_step_count: int = Field(ge=0, le=64)
    max_parallel: int = Field(ge=1, le=8)
    max_tool_calls: int = Field(ge=1, le=64)
    total_timeout_ms: int = Field(ge=100, le=600_000)


class AnalysisStepCheckpoint(ContractModel):
    """Minimal durable step state; payloads stay in the Result Store."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    step_id: str = Field(min_length=1, max_length=128)
    status: Literal["success", "partial", "denied", "failed", "timeout", "skipped"]
    reason_code: str = Field(min_length=1, max_length=128)
    result_id: str | None = Field(default=None, min_length=1, max_length=128)
    evidence_ids: tuple[str, ...] = Field(default=(), max_length=64)
    tool_call_consumed: bool = True


class AnalysisReduction(ContractModel):
    """Deterministic reduce decision after one execution wave."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    additions: tuple[AnalysisStepCheckpoint, ...] = ()
    ready_step_ids: tuple[str, ...] = Field(default=(), max_length=64)
    terminal: bool


class AnalysisRunOutcome(ContractModel):
    """Terminal reference to a report already saved in the Result Store."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    analysis_run_id: str = Field(min_length=1, max_length=128)
    plan_id: str = Field(min_length=1, max_length=128)
    request_id: str = Field(min_length=1, max_length=128)
    status: Literal["completed", "partial", "failed"]
    reason_code: str = Field(min_length=1, max_length=128)
    report_result_id: str | None = Field(default=None, min_length=1, max_length=128)


class AnalysisExecutionFrame(ContractModel):
    """Durable graph frame. It intentionally excludes auth and plan bodies."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0"] = ANALYSIS_GRAPH_STATE_VERSION
    invocation_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    analysis_run_id: str = Field(min_length=1, max_length=128)
    plan_id: str = Field(min_length=1, max_length=128)
    request_id: str = Field(min_length=1, max_length=128)
    deadline_at: datetime
    expected_step_count: int = Field(ge=0, le=64)
    max_parallel: int = Field(ge=1, le=8)
    max_tool_calls: int = Field(ge=1, le=64)


class AnalysisGraphExecutionPort(Protocol):
    """Idempotent service used by graph adapters.

    Every transition receives ``analysis_run_id``. ``execute_step`` must use a
    deterministic call identity and persist Result/Evidence before returning;
    crash replay therefore returns the same references without a second
    upstream call.
    """

    async def prepare(
        self,
        *,
        analysis_run_id: str,
        plan_id: str,
        request_id: str,
        auth_context: AuthContext,
    ) -> AnalysisGraphPreparation: ...

    async def reduce(
        self,
        *,
        analysis_run_id: str,
        plan_id: str,
        request_id: str,
        completed: tuple[AnalysisStepCheckpoint, ...],
        tool_call_count: int,
        deadline_exceeded: bool,
        auth_context: AuthContext,
    ) -> AnalysisReduction: ...

    async def execute_step(
        self,
        *,
        analysis_run_id: str,
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


class AnalysisRunLifecycle(Protocol):
    """Controlled product-ledger transitions for reauthentication."""

    async def wait_for_reauthentication(
        self, *, user_id: str, run_id: str
    ) -> object: ...

    async def resume_from_input(
        self,
        *,
        user_id: str,
        run_id: str,
        input_request_id: str,
        run_state_version: int,
    ) -> object: ...


class AnalysisRunLeaseManager(Protocol):
    """Serializes one analysis run before any graph transition executes."""

    def lease(
        self, *, analysis_run_id: str
    ) -> AbstractAsyncContextManager[None]: ...
