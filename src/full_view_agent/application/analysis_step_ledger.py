"""Application contract for crash-safe, at-most-once analysis steps."""

from typing import Literal, Protocol

from pydantic import ConfigDict, Field, model_validator

from full_view_agent.application.fingerprints import canonical_fingerprint
from full_view_agent.domain.models import ContractModel

AnalysisCheckpointStatus = Literal[
    "success", "partial", "denied", "failed", "timeout", "skipped"
]
AnalysisStepLedgerStatus = Literal[
    "reserved",
    "executing",
    "waiting_reauth",
    "observed",
    "persisted",
    "synthetic",
    "indeterminate",
    "failed",
]


def analysis_step_tool_call_id(
    *, tenant_id: str, run_id: str, plan_id: str, step_id: str
) -> str:
    """Derive a tenant/run-scoped canonical call identity for one plan step."""
    return canonical_fingerprint(
        domain="analysis-step-tool-call:1.1",
        value={
            "tenant_id": tenant_id,
            "run_id": run_id,
            "plan_id": plan_id,
            "step_id": step_id,
        },
    )


class AnalysisStepObservationValidator(Protocol):
    """Verifies that terminal ledger references belong to this observation."""

    async def validate(
        self,
        *,
        tenant_id: str,
        user_id: str,
        run_id: str,
        tool_call_id: str,
        result_id: str,
        evidence_ids: tuple[str, ...],
    ) -> None: ...


class AnalysisStepLedgerEntry(ContractModel):
    """Durable authority record for one plan step."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tenant_id: str = Field(min_length=1, max_length=128)
    user_id: str = Field(min_length=1, max_length=128)
    run_id: str = Field(min_length=1, max_length=128)
    plan_id: str = Field(min_length=1, max_length=128)
    request_id: str = Field(min_length=1, max_length=128)
    step_id: str = Field(min_length=1, max_length=128)
    tool_call_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    invocation_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    status: AnalysisStepLedgerStatus = "reserved"
    result_status: AnalysisCheckpointStatus | None = None
    reason_code: str | None = Field(default=None, min_length=1, max_length=256)
    result_id: str | None = Field(default=None, min_length=1, max_length=128)
    evidence_ids: tuple[str, ...] = Field(default=(), max_length=64)
    version: int = Field(default=1, ge=1)

    @model_validator(mode="after")
    def validate_result_references(self) -> "AnalysisStepLedgerEntry":
        if len(self.evidence_ids) != len(set(self.evidence_ids)):
            raise ValueError("evidence ids must be unique")
        if self.status == "persisted":
            if (
                self.result_status not in {"success", "partial"}
                or self.reason_code is None
                or self.result_id is None
                or not self.evidence_ids
            ):
                raise ValueError(
                    "persisted steps require outcome, result and evidence references"
                )
        elif self.status == "observed":
            if (
                self.result_status not in {"success", "partial"}
                or self.reason_code is None
                or self.result_id is not None
                or self.evidence_ids
            ):
                raise ValueError(
                    "observed steps require an outcome without result references"
                )
        elif self.status == "failed":
            if (
                self.result_status not in {"denied", "failed"}
                or self.reason_code is None
                or self.result_id is not None
                or self.evidence_ids
            ):
                raise ValueError("failed steps require a denied or failed outcome")
        elif self.status == "synthetic":
            if (
                self.result_status not in {"timeout", "skipped"}
                or self.reason_code is None
                or self.result_id is not None
                or self.evidence_ids
            ):
                raise ValueError("synthetic steps require a timeout or skipped outcome")
        elif (
            self.result_status is not None
            or self.reason_code is not None
            or self.result_id is not None
            or self.evidence_ids
        ):
            raise ValueError(
                "only terminal or observed steps may carry an outcome"
            )
        return self


class AnalysisStepLedgerConflict(Exception):
    """The requested step identity or state is unsafe to use."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"analysis step ledger rejected [{code}]: {message}")


class AnalysisStepLedgerStore(Protocol):
    async def reserve_step(
        self,
        *,
        tenant_id: str,
        user_id: str,
        run_id: str,
        plan_id: str,
        request_id: str,
        step_id: str,
        tool_call_id: str,
        invocation_fingerprint: str,
    ) -> AnalysisStepLedgerEntry: ...

    async def get_step(
        self,
        *,
        tenant_id: str,
        user_id: str,
        run_id: str,
        step_id: str,
        invocation_fingerprint: str,
    ) -> AnalysisStepLedgerEntry: ...

    async def transition_step(
        self,
        *,
        tenant_id: str,
        user_id: str,
        run_id: str,
        step_id: str,
        invocation_fingerprint: str,
        expected_version: int,
        status: AnalysisStepLedgerStatus,
        result_id: str | None,
        evidence_ids: tuple[str, ...],
        result_status: AnalysisCheckpointStatus | None = None,
        reason_code: str | None = None,
    ) -> AnalysisStepLedgerEntry: ...

    async def mark_indeterminate_if_unfinished(
        self,
        *,
        tenant_id: str,
        user_id: str,
        run_id: str,
        step_id: str,
        invocation_fingerprint: str,
        expected_version: int,
    ) -> AnalysisStepLedgerEntry: ...
