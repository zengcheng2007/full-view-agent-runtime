"""Framework-neutral ownership binding for resumable analysis runs."""

from typing import Literal, Protocol

from pydantic import ConfigDict, Field

from full_view_agent.domain.models import ContractModel

AnalysisRunBindingStatus = Literal[
    "pending",
    "running",
    "waiting_input",
    "completed",
    "partial",
    "failed",
    "cancelled",
]


class AnalysisRunBinding(ContractModel):
    """Durable identity and outcome reference for one analysis run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tenant_id: str = Field(min_length=1, max_length=128)
    user_id: str = Field(min_length=1, max_length=128)
    session_id: str = Field(min_length=1, max_length=128)
    run_id: str = Field(min_length=1, max_length=128)
    plan_id: str = Field(min_length=1, max_length=128)
    request_id: str = Field(min_length=1, max_length=128)
    invocation_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    status: AnalysisRunBindingStatus = "pending"
    report_result_id: str | None = Field(default=None, min_length=1, max_length=128)
    version: int = Field(default=1, ge=1)


class AnalysisRunBindingConflict(Exception):
    """A binding identity or stored authority record is inconsistent."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"analysis run binding rejected [{code}]: {message}")


class AnalysisRunBindingStore(Protocol):
    async def ensure_binding(
        self,
        *,
        tenant_id: str,
        user_id: str,
        session_id: str,
        run_id: str,
        plan_id: str,
        request_id: str,
        invocation_fingerprint: str,
    ) -> AnalysisRunBinding: ...

    async def get_binding(
        self,
        *,
        tenant_id: str,
        user_id: str,
        run_id: str,
        invocation_fingerprint: str,
    ) -> AnalysisRunBinding: ...

    async def update_binding(
        self,
        *,
        tenant_id: str,
        user_id: str,
        run_id: str,
        invocation_fingerprint: str,
        expected_version: int,
        status: AnalysisRunBindingStatus,
        report_result_id: str | None,
    ) -> AnalysisRunBinding: ...
