from datetime import datetime
from typing import Literal

from pydantic import Field

from full_view_agent.api._api_deps import ResponseMeta
from full_view_agent.domain.contract_model import ContractModel
from full_view_agent.domain.models import RunOutcome, RunStatus


class RuntimeDependencyStatus(ContractModel):
    name: str
    status: Literal["ok", "error", "missing"]


class RuntimeReadiness(ContractModel):
    status: Literal["ok", "degraded"]
    dependencies: list[RuntimeDependencyStatus]


class RuntimeIdentity(ContractModel):
    version: str
    started_at: datetime
    capability_generation: int = Field(ge=0)
    global_loaded_tools: int = Field(ge=0)
    global_loaded_skills: int = Field(ge=0)
    global_loaded_workflows: int = Field(ge=0)


class RuntimeLatency(ContractModel):
    p50: int | None = Field(default=None, ge=0)
    p95: int | None = Field(default=None, ge=0)


class RuntimeOverview(ContractModel):
    application_id: str
    window_started_at: datetime
    window_ended_at: datetime
    runtime: RuntimeIdentity
    readiness: RuntimeReadiness
    session_count: int = Field(ge=0)
    run_count: int = Field(ge=0)
    active_run_count: int = Field(ge=0)
    terminal_status_distribution: dict[str, int]
    outcome_distribution: dict[str, int]
    success_rate: float | None = Field(default=None, ge=0, le=1)
    latency_ms: RuntimeLatency


class RuntimeSessionItem(ContractModel):
    session_id: str
    title: str
    status: Literal["active", "archived"]
    active_run_id: str | None
    created_at: datetime
    updated_at: datetime
    version: int
    run_count: int = Field(ge=0)


class RuntimeRunItem(ContractModel):
    run_id: str
    session_id: str
    status: RunStatus
    outcome: RunOutcome | None
    mode: Literal["agent", "workflow", "analysis"]
    current_phase: str
    waiting_for: str | None
    completion_reason_code: str | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    duration_ms: int | None = Field(default=None, ge=0)
    agent_release_id: str | None = None
    model_config_id: str | None = None
    model_config_version: int | None = None


class RuntimeCapabilityRef(ContractModel):
    capability_id: str
    version: str | None = None
    type: Literal["tool", "skill", "workflow"]


class RuntimeModelRef(ContractModel):
    config_id: str
    config_version: int
    model_name: str | None = None


class RuntimeTimelineItem(ContractModel):
    timeline_id: str
    occurred_at: datetime
    category: Literal[
        "auth",
        "release",
        "capability",
        "model",
        "tool",
        "skill",
        "workflow",
        "result",
        "evidence",
        "final",
        "frontend_command",
        "run",
    ]
    event_type: str
    status: str | None = None
    duration_ms: int | None = Field(default=None, ge=0)
    stable_error_code: str | None = None
    capability_ref: RuntimeCapabilityRef | None = None
    model_ref: RuntimeModelRef | None = None
    result_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    frontend_command_ids: list[str] = Field(default_factory=list)
    details: dict[str, str | int | float | bool | None] = Field(default_factory=dict)


class RuntimeTimeline(ContractModel):
    application_id: str
    run_id: str
    session_id: str
    events_are_retention_bound: Literal[True] = True
    event_history_status: Literal["within_retention", "expired_or_partial"]
    event_retention_cutoff: datetime | None = None
    run: RuntimeRunItem
    items: list[RuntimeTimelineItem]
    result_ids: list[str]
    evidence_ids: list[str]
    frontend_command_ids: list[str]


class RuntimeModelMetric(ContractModel):
    config_id: str
    config_version: int
    model_name: str | None = None
    agent_id: str | None = None
    request_count: int = Field(ge=0)
    response_count: int = Field(ge=0)
    failure_count: int = Field(ge=0)
    fallback_run_count: int = Field(ge=0)
    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    usage_event_count: int = Field(ge=0)
    success_rate: float | None = Field(default=None, ge=0, le=1)
    latency_ms: RuntimeLatency
    last_error_code: str | None = None


class RuntimeCapabilityMetric(ContractModel):
    capability_id: str
    capability_version: str | None = None
    capability_type: Literal["tool", "skill", "workflow"]
    connector_id: str | None = None
    invocation_count: int = Field(ge=0)
    success_count: int = Field(ge=0)
    failure_count: int = Field(ge=0)
    denied_count: int = Field(ge=0)
    success_rate: float | None = Field(default=None, ge=0, le=1)
    latency_ms: RuntimeLatency
    last_error_code: str | None = None


class RuntimeAlert(ContractModel):
    alert_id: str
    severity: Literal["warning", "critical"]
    code: Literal[
        "readiness_degraded",
        "run_failure_rate_high",
        "run_p95_latency_high",
    ]
    status: Literal["active"] = "active"
    summary: str
    observed_value: float
    threshold: float
    window_started_at: datetime
    window_ended_at: datetime
    capability_ref: RuntimeCapabilityRef | None = None
    model_ref: RuntimeModelRef | None = None


class RuntimeCursorMeta(ResponseMeta):
    has_next: bool
    next_cursor: str | None = None


class RuntimeEventMetricMeta(ResponseMeta):
    event_window_status: Literal["retention_bounded"] = "retention_bounded"
    events_are_retention_bound: Literal[True] = True
    event_retention_seconds: int = Field(gt=0)
    requested_from: datetime
    requested_to: datetime
    effective_from: datetime
    effective_to: datetime
    truncated: bool


class RuntimeOverviewResponse(ContractModel):
    data: RuntimeOverview
    meta: ResponseMeta


class RuntimeSessionListResponse(ContractModel):
    data: list[RuntimeSessionItem]
    meta: RuntimeCursorMeta


class RuntimeRunListResponse(ContractModel):
    data: list[RuntimeRunItem]
    meta: RuntimeCursorMeta


class RuntimeTimelineResponse(ContractModel):
    data: RuntimeTimeline
    meta: ResponseMeta


class RuntimeModelMetricListResponse(ContractModel):
    data: list[RuntimeModelMetric]
    meta: RuntimeEventMetricMeta


class RuntimeCapabilityMetricListResponse(ContractModel):
    data: list[RuntimeCapabilityMetric]
    meta: RuntimeEventMetricMeta


class RuntimeAlertListResponse(ContractModel):
    data: list[RuntimeAlert]
    meta: ResponseMeta
