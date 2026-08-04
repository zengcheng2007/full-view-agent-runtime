"""Durable execution ownership for an Agent Run taken over by Analysis Graph."""

from datetime import UTC, datetime
from typing import Literal, Protocol

from pydantic import ConfigDict, Field, model_validator

from full_view_agent.application.errors import RunStateConflict
from full_view_agent.domain.analysis_intent import AnalysisIntentV1
from full_view_agent.domain.contract_model import ContractModel

AnalysisIntentHandoffStatus = Literal[
    "captured",
    "waiting_clarification",
    "waiting_reauth",
    "compiling",
    "compiled",
    "executing",
    "completed",
    "partial",
    "failed",
    "denied",
    "cancelled",
]


class HandoffClarificationOption(ContractModel):
    """Server-only mapping; clients receive only option_id and area_name."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    option_id: str = Field(min_length=1, max_length=128)
    area_code: str = Field(pattern=r"^[0-9]+$", max_length=32)
    area_name: str = Field(min_length=1, max_length=100)


class AnalysisIntentHandoff(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    handoff_id: str = Field(min_length=1, max_length=128)
    tenant_id: str = Field(min_length=1, max_length=128)
    user_id: str = Field(min_length=1, max_length=128)
    session_id: str = Field(min_length=1, max_length=128)
    run_id: str = Field(min_length=1, max_length=128)
    intent: AnalysisIntentV1
    intent_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    status: AnalysisIntentHandoffStatus = "captured"
    clarification_options: tuple[HandoffClarificationOption, ...] = Field(
        default=(), max_length=32
    )
    selected_area_code: str | None = Field(
        default=None, pattern=r"^[0-9]+$", max_length=32
    )
    plan_id: str | None = Field(default=None, min_length=1, max_length=128)
    request_id: str | None = Field(default=None, min_length=1, max_length=128)
    failure_code: str | None = Field(default=None, min_length=1, max_length=128)
    report_result_id: str | None = Field(default=None, min_length=1, max_length=128)
    version: int = Field(default=1, ge=1)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def _validate_state_payload(self) -> "AnalysisIntentHandoff":
        if (self.plan_id is None) != (self.request_id is None):
            raise ValueError("handoff plan_id and request_id must be supplied together")
        if (
            self.status in {"compiled", "executing", "completed", "partial"}
            and self.plan_id is None
        ):
            raise ValueError("compiled analysis handoff requires plan authority")
        if self.status == "waiting_clarification" and (
            not self.clarification_options or self.selected_area_code is not None
        ):
            raise ValueError("clarification wait requires unresolved server options")
        if self.selected_area_code is not None and self.selected_area_code not in {
            option.area_code for option in self.clarification_options
        }:
            raise ValueError("selected area must come from durable clarification options")
        if self.status in {"failed", "denied"}:
            if self.failure_code is None:
                raise ValueError("failed or denied handoff requires failure_code")
        elif self.failure_code is not None:
            raise ValueError("non-failed handoff cannot carry failure_code")
        if self.status in {"completed", "partial"}:
            if self.report_result_id is None:
                raise ValueError("completed or partial handoff requires report result")
        elif self.report_result_id is not None:
            raise ValueError("non-reporting handoff cannot carry report result")
        if self.updated_at < self.created_at:
            raise ValueError("handoff updated_at cannot precede created_at")
        return self


class AnalysisIntentHandoffConflict(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"analysis intent handoff rejected [{code}]: {message}")


_ALLOWED_TRANSITIONS: dict[
    AnalysisIntentHandoffStatus, frozenset[AnalysisIntentHandoffStatus]
] = {
    "captured": frozenset(
        {
            "waiting_clarification",
            "waiting_reauth",
            "compiling",
            "failed",
            "denied",
            "cancelled",
        }
    ),
    "waiting_clarification": frozenset(
        {"waiting_reauth", "compiling", "failed", "denied", "cancelled"}
    ),
    "waiting_reauth": frozenset(
        {"compiling", "executing", "failed", "denied", "cancelled"}
    ),
    "compiling": frozenset(
        {
            "waiting_clarification",
            "waiting_reauth",
            "compiled",
            "failed",
            "denied",
            "cancelled",
        }
    ),
    "compiled": frozenset(
        {"waiting_reauth", "executing", "failed", "denied", "cancelled"}
    ),
    "executing": frozenset(
        {"waiting_reauth", "completed", "partial", "failed", "cancelled"}
    ),
    "completed": frozenset(),
    "partial": frozenset(),
    "failed": frozenset(),
    "denied": frozenset(),
    "cancelled": frozenset(),
}


def validate_handoff_transition(
    current: AnalysisIntentHandoff,
    requested: AnalysisIntentHandoff,
) -> None:
    if requested.status not in _ALLOWED_TRANSITIONS[current.status]:
        raise RunStateConflict(
            f"analysis handoff cannot transition from {current.status} "
            f"to {requested.status}"
        )
    immutable_fields = (
        "handoff_id",
        "tenant_id",
        "user_id",
        "session_id",
        "run_id",
        "intent",
        "intent_fingerprint",
        "created_at",
    )
    if any(
        getattr(current, field) != getattr(requested, field)
        for field in immutable_fields
    ):
        raise RunStateConflict("analysis handoff immutable identity changed")
    if current.clarification_options and (
        requested.clarification_options != current.clarification_options
    ):
        raise RunStateConflict("analysis handoff clarification authority changed")
    if current.selected_area_code is not None and (
        requested.selected_area_code != current.selected_area_code
    ):
        raise RunStateConflict("analysis handoff selected area changed")
    if current.plan_id is not None and (
        requested.plan_id != current.plan_id
        or requested.request_id != current.request_id
    ):
        raise RunStateConflict("analysis handoff plan authority changed")
    if requested.version != current.version + 1:
        raise RunStateConflict("analysis handoff version is not monotonic")


class AnalysisIntentHandoffStore(Protocol):
    async def capture(
        self,
        *,
        tenant_id: str,
        user_id: str,
        session_id: str,
        run_id: str,
        intent: AnalysisIntentV1,
    ) -> AnalysisIntentHandoff: ...

    async def get_for_run(
        self, *, tenant_id: str, user_id: str, run_id: str
    ) -> AnalysisIntentHandoff: ...

    async def advance(
        self,
        *,
        tenant_id: str,
        user_id: str,
        run_id: str,
        expected_version: int,
        status: AnalysisIntentHandoffStatus,
        clarification_options: tuple[HandoffClarificationOption, ...] = (),
        selected_area_code: str | None = None,
        plan_id: str | None = None,
        request_id: str | None = None,
        failure_code: str | None = None,
        report_result_id: str | None = None,
    ) -> AnalysisIntentHandoff: ...
