"""Framework-neutral ownership binding for resumable analysis runs."""

from typing import Literal, Protocol

from pydantic import ConfigDict, Field, model_validator

from full_view_agent.application.errors import RunStateConflict
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

    @model_validator(mode="after")
    def _validate_outcome_reference(self) -> "AnalysisRunBinding":
        if self.status in {"completed", "partial"} and self.report_result_id is None:
            raise ValueError("completed or partial analysis bindings require a report")
        if self.status in {"pending", "running", "waiting_input", "failed", "cancelled"} and (
            self.report_result_id is not None
        ):
            raise ValueError("non-reporting analysis binding status cannot carry a report")
        return self


class AnalysisRunBindingConflict(Exception):
    """A binding identity or stored authority record is inconsistent."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"analysis run binding rejected [{code}]: {message}")


_ALLOWED_TRANSITIONS: dict[AnalysisRunBindingStatus, frozenset[AnalysisRunBindingStatus]] = {
    "pending": frozenset({"running", "failed", "cancelled"}),
    "running": frozenset(
        {"waiting_input", "completed", "partial", "failed", "cancelled"}
    ),
    "waiting_input": frozenset({"running", "failed", "cancelled"}),
    "completed": frozenset(),
    "partial": frozenset(),
    "failed": frozenset(),
    "cancelled": frozenset(),
}


def validate_analysis_binding_transition(
    current: AnalysisRunBinding,
    *,
    status: AnalysisRunBindingStatus,
    report_result_id: str | None,
) -> None:
    """Validate a one-way lifecycle transition before either store mutates."""

    if current.status == status and current.report_result_id == report_result_id:
        return
    if status not in _ALLOWED_TRANSITIONS[current.status]:
        raise RunStateConflict(
            f"analysis run binding cannot transition from {current.status} to {status}"
        )
    try:
        AnalysisRunBinding.model_validate(
            {
                **current.model_dump(mode="python"),
                "status": status,
                "report_result_id": report_result_id,
            }
        )
    except ValueError as exc:
        raise RunStateConflict("analysis run binding outcome is inconsistent") from exc


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
