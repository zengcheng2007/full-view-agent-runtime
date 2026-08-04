from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field

from full_view_agent.application.answer_claims import StructuredFinish
from full_view_agent.domain.models import ContractModel, RunOutcome, RunStatus


class EvalAuthSpec(ContractModel):
    area_codes: list[str] = Field(default_factory=lambda: ["330106"], min_length=1)
    datasets: list[str] = Field(default_factory=lambda: ["population"], min_length=1)
    entitlements: list[str] = Field(
        default_factory=lambda: ["governance.population.aggregate.read"],
        min_length=1,
    )
    # S1-A：语义入口按 Catalog 声明的字段策略集校验；直接 Tool 用例
    # 不受该字段影响，保持默认评测策略集。
    field_policy_set: str = Field(default="eval_policy_v1", min_length=1, max_length=64)


class EvalToolCallStep(ContractModel):
    type: Literal["tool_call"]
    tool_id: str = Field(min_length=1, max_length=200)
    arguments: dict[str, object]
    total_tokens: int = Field(default=0, ge=0)


class EvalFinishStep(ContractModel):
    type: Literal["finish"]
    content: str = Field(min_length=1, max_length=10_000)
    structured_finish: StructuredFinish | None = None
    total_tokens: int = Field(default=0, ge=0)


class EvalErrorStep(ContractModel):
    type: Literal["error"]
    error_code: Literal[
        "model_timeout",
        "model_provider_unavailable",
        "model_contract_error",
    ]
    message: str = Field(min_length=1, max_length=1000)


class EvalFaultSpec(ContractModel):
    type: Literal[
        "upstream_timeout",
        "upstream_unavailable",
        "upstream_contract_error",
    ]
    tool_id: str = Field(min_length=1, max_length=200)


EvalModelStep = Annotated[
    EvalToolCallStep | EvalFinishStep | EvalErrorStep,
    Field(discriminator="type"),
]


class EvalTerminalVariant(ContractModel):
    outcome: RunOutcome
    completion_reason_code: str = Field(min_length=1, max_length=200)
    tool_ids: list[str] = Field(default_factory=list)


class EvalExpected(ContractModel):
    terminal_status: RunStatus
    outcome: RunOutcome
    completion_reason_code: str = Field(min_length=1, max_length=200)
    tool_ids: list[str] = Field(default_factory=list)
    required_tool_ids: list[str] = Field(default_factory=list)
    forbidden_tool_ids: list[str] = Field(default_factory=list)
    max_tool_calls: int | None = Field(default=None, ge=0)
    min_evidence_count: int = Field(default=0, ge=0)
    max_evidence_count: int | None = Field(default=None, ge=0)
    required_event_types: list[str] = Field(default_factory=list)
    required_answer_substrings: list[str] = Field(default_factory=list)
    required_answer_any_substrings: list[str] = Field(default_factory=list)
    forbidden_answer_substrings: list[str] = Field(default_factory=list)
    grounding_reason_code: Literal[
        "grounded",
        "unsupported_number",
        "unsupported_area",
        "unsupported_object",
        "unsupported_judgement",
    ] | None = None
    acceptable_terminal_variants: list[EvalTerminalVariant] = Field(
        default_factory=list,
        max_length=10,
    )


class EvalFollowUpTurn(ContractModel):
    user_message: str = Field(min_length=1, max_length=10_000)
    model_steps: list[EvalModelStep] = Field(
        default_factory=list,
        max_length=20,
        description="Scripted steps for this turn. Empty = use live provider.",
    )
    expected: EvalExpected


class EvalCase(ContractModel):
    schema_version: Literal["1.0"] = "1.0"
    case_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{2,127}$")
    description: str = Field(min_length=1, max_length=500)
    user_message: str = Field(min_length=1, max_length=10_000)
    auth: EvalAuthSpec = Field(default_factory=EvalAuthSpec)
    model_steps: list[EvalModelStep] = Field(
        default_factory=list, max_length=20,
        description="Scripted model steps. Empty = use live model provider.",
    )
    fault: EvalFaultSpec | None = None
    expected: EvalExpected
    follow_up_turns: list[EvalFollowUpTurn] = Field(
        default_factory=list,
        max_length=5,
    )


class EvalMessageRecord(ContractModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str | None = None
    tool_call_id: str | None = None


class EvalModelRequestRecord(ContractModel):
    messages: list[EvalMessageRecord]
    tool_ids: list[str]
    prompt_version: str | None = None


class EvalOutboundRequestSummary(ContractModel):
    method: str = Field(min_length=1, max_length=16)
    path: str = Field(min_length=1, max_length=500)
    count: int = Field(ge=1)


GradeValue = str | int | bool | list[str] | None


class EvalGrade(ContractModel):
    name: str
    passed: bool
    expected: GradeValue
    actual: GradeValue


class EvalTurnTrace(ContractModel):
    turn_index: int = Field(ge=1)
    user_message: str
    model_steps: list[EvalModelStep]
    event_types: list[str]
    terminal_status: RunStatus
    outcome: RunOutcome | None
    completion_reason_code: str | None
    tool_ids: list[str]
    evidence_ids: list[str]
    grades: list[EvalGrade]
    passed: bool


class EvalTrace(ContractModel):
    trace_version: Literal["1.0"] = "1.0"
    eval_run_id: str
    replayed_from_eval_run_id: str | None = None
    case_id: str
    started_at: datetime
    completed_at: datetime
    environment_kind: str = "unknown"
    evidence_source_system: str = "unknown"
    runtime_version: str = "unknown"
    outbound_requests: list[EvalOutboundRequestSummary] = Field(default_factory=list)
    model_provider: str = "scripted"
    model_name: str = "scripted"
    prompt_version: str = "unknown"
    model_requests: list[EvalModelRequestRecord]
    model_steps: list[EvalModelStep]
    total_tokens: int = Field(ge=0)
    event_types: list[str]
    terminal_status: RunStatus
    outcome: RunOutcome | None
    completion_reason_code: str | None
    tool_ids: list[str]
    evidence_ids: list[str]
    grades: list[EvalGrade]
    turns: list[EvalTurnTrace] = Field(default_factory=list)
    passed: bool


class EvalCaseReport(ContractModel):
    case_id: str
    passed: bool
    trace_path: str


class EvalSuiteReport(ContractModel):
    suite_version: Literal["1.0"] = "1.0"
    suite_run_id: str
    started_at: datetime
    completed_at: datetime
    total_cases: int = Field(ge=0)
    passed_cases: int = Field(ge=0)
    failed_cases: int = Field(ge=0)
    pass_at_1: float = Field(ge=0, le=1)
    case_results: list[EvalCaseReport]
