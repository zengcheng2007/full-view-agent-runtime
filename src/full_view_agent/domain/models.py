from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TextContent(ContractModel):
    type: Literal["text"]
    text: str = Field(min_length=1, max_length=10_000)


class ResultReferenceContent(ContractModel):
    type: Literal["result_reference"]
    result_id: str = Field(min_length=1, max_length=128)
    label: str = Field(min_length=1, max_length=200)


MessageContent = Annotated[
    TextContent | ResultReferenceContent,
    Field(discriminator="type"),
]


class MessageInput(ContractModel):
    client_message_id: str = Field(min_length=1, max_length=128)
    content: list[TextContent] = Field(min_length=1, max_length=1)


class AgentMessage(ContractModel):
    schema_version: Literal["1.1"] = "1.1"
    message_id: str
    session_id: str
    run_id: str
    role: Literal["user", "assistant"]
    content: list[MessageContent] = Field(min_length=1)
    evidence_ids: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ClientCapabilities(ContractModel):
    client_instance_id: str = Field(min_length=1, max_length=128)
    frontend_command_schema_versions: list[str] = Field(default_factory=list)
    supported_commands: list[str] = Field(default_factory=list)


class WorkflowRef(ContractModel):
    workflow_id: str = Field(min_length=1, max_length=128)
    workflow_version: str = Field(min_length=1, max_length=32)


class WorkflowDefinition(ContractModel):
    workflow_id: str = Field(min_length=1, max_length=128)
    workflow_version: str = Field(min_length=1, max_length=32)
    status: Literal["active", "disabled"] = "active"
    required_roles: list[str] = Field(default_factory=list)
    executor_ref: str = Field(min_length=1, max_length=200)


class ResolveAreaInput(ContractModel):
    query: str = Field(min_length=1, max_length=200)
    parent_area_code: str | None = Field(default=None, min_length=1, max_length=32)
    context_area_code: str | None = Field(default=None, min_length=1, max_length=32)
    max_candidates: int = Field(default=5, ge=1, le=20)


class MetricQueryScope(ContractModel):
    area_code: str = Field(min_length=1, max_length=32)
    include_descendants: bool = True


class PopulationMetricFilter(ContractModel):
    field: Literal["person_category", "age", "gender"]
    operator: Literal["eq", "neq", "in", "gte", "gt", "lte", "lt", "between", "exists"]
    value: str | int | float | bool | list[str | int | float] | None = None

    @model_validator(mode="after")
    def validate_registered_operator(self) -> "PopulationMetricFilter":
        allowed_operators = {
            "person_category": {"eq", "neq", "in", "exists"},
            "gender": {"eq", "neq", "in", "exists"},
            "age": {"eq", "neq", "in", "gte", "gt", "lte", "lt", "between", "exists"},
        }
        if self.operator not in allowed_operators[self.field]:
            raise ValueError(f"operator {self.operator} is not registered for {self.field}")
        return self


class PopulationMetricQuerySpec(ContractModel):
    schema_version: Literal["1.1"] = "1.1"
    metrics: list[Literal["person_count"]] = Field(min_length=1, max_length=1)
    scope: MetricQueryScope
    filters: list[PopulationMetricFilter] = Field(default_factory=list, max_length=10)
    group_by: list[
        Literal["street", "community", "grid", "gender", "age_band"]
    ] = Field(default_factory=list, max_length=2)
    limit: int = Field(default=200, ge=1, le=1000)
    presentation_hint: Literal["table", "metric", "choropleth"] = "table"


class QueryPopulationMetricsInput(ContractModel):
    query: PopulationMetricQuerySpec


class GovernanceObjectRef(ContractModel):
    object_type: Literal["person", "building", "room", "enterprise", "event"]
    object_id: str = Field(min_length=1, max_length=128)


class GetObjectProfileInput(ContractModel):
    object_ref: GovernanceObjectRef
    field_sets: list[
        Literal["summary", "demographics", "location", "governance_status", "contact"]
    ] = Field(default_factory=lambda: ["summary"], min_length=1, max_length=5)


class ToolResultSchemaBinding(ContractModel):
    kind: Literal["area_candidates", "table", "metric", "object_profile"]
    data_schema_ref: str


class ToolLimits(ContractModel):
    timeout_ms: int = Field(ge=100, le=120_000)
    max_attempts: int = Field(ge=1, le=5)
    max_result_rows: int = Field(ge=1, le=10_000)
    max_group_buckets: int = Field(ge=1, le=1000)


class ToolCachePolicy(ContractModel):
    enabled: bool
    ttl_seconds: int = Field(ge=0, le=86_400)


class ToolPolicyBinding(ContractModel):
    action: str = Field(min_length=1, max_length=200)
    pre_check: bool = True
    post_filter: bool = True
    denial_scope: Literal["dataset_area_fields", "object_fields"]


class InternalToolManifest(ContractModel):
    schema_version: Literal["1.1"] = "1.1"
    tool_id: str
    tool_version: str
    status: Literal["active", "disabled"] = "active"
    domain: Literal["governance"] = "governance"
    owner: str
    effect: Literal["read"] = "read"
    risk_level: Literal["low", "medium"]
    dataset_id: str
    data_classifications: list[Literal["public", "internal", "aggregated", "sensitive"]]
    required_permissions: list[str]
    input_schema_ref: str
    result_schemas: list[ToolResultSchemaBinding]
    limits: ToolLimits
    cache_policy: ToolCachePolicy
    policy: ToolPolicyBinding
    adapter_ref: str


class ModelInputSchemaReference(ContractModel):
    ref: str = Field(alias="$ref", serialization_alias="$ref")


class ModelToolDescriptor(ContractModel):
    tool_id: str
    tool_version: str
    name: str
    description: str
    input_schema: ModelInputSchemaReference


class Principal(ContractModel):
    tenant_id: str
    user_id: str
    org_id: str
    roles: list[str] = Field(default_factory=list)


class LegacyIdentitySnapshot(ContractModel):
    principal: Principal
    source: str
    source_session_expires_at: datetime
    base_area_codes: list[str] = Field(default_factory=list)


class AgentApplication(ContractModel):
    app_id: str
    agent_id: str


class AuthorizedAreaScope(ContractModel):
    area_code: str = Field(min_length=1, max_length=32)
    include_descendants: bool = True


class AuthDataScopes(ContractModel):
    areas: list[AuthorizedAreaScope]
    datasets: list[str]
    field_policy_set: str


class AuthContext(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.1"] = "1.1"
    auth_context_id: str
    auth_context_fingerprint: str
    principal: Principal
    application: AgentApplication
    entitlements: list[str]
    data_scopes: AuthDataScopes
    purpose: Literal["interactive_analysis", "registered_workflow"]
    session_id: str
    run_id: str
    credential_ref: str
    issued_at: datetime
    expires_at: datetime
    policy_version: str


class CredentialGrant(ContractModel):
    credential_ref: str
    credential_type: Literal["legacy_geo_token"] = "legacy_geo_token"
    subject_user_id: str
    app_id: str
    run_id: str
    created_at: datetime
    expires_at: datetime


class EffectivePolicyScope(ContractModel):
    area_codes: list[str] = Field(default_factory=list)
    datasets: list[str] = Field(default_factory=list)
    allowed_field_sets: list[str] = Field(default_factory=list)
    denied_field_sets: list[str] = Field(default_factory=list)
    result_limit: int = Field(default=1, ge=0, le=10_000)


class PolicyDecision(ContractModel):
    schema_version: Literal["1.1"] = "1.1"
    policy_decision_id: str
    decision: Literal["allow", "mask", "deny", "need_approval"]
    phase: Literal["pre_execution", "post_result"] = "pre_execution"
    auth_context_fingerprint: str
    tool_id: str
    tool_version: str
    arguments_fingerprint: str
    request_fingerprint: str
    result_fingerprint: str | None = None
    reason_codes: list[str] = Field(default_factory=list)
    user_message: str
    effective_scope: EffectivePolicyScope
    policy_fingerprint: str
    policy_version: str
    issued_at: datetime
    expires_at: datetime


class RunCreateRequest(ContractModel):
    input: MessageInput
    client: ClientCapabilities
    mode: Literal["agent", "workflow"] = "agent"
    workflow_ref: WorkflowRef | None = None

    @model_validator(mode="after")
    def validate_workflow_reference(self) -> "RunCreateRequest":
        if self.mode == "workflow" and self.workflow_ref is None:
            raise ValueError("workflow_ref is required when mode=workflow")
        return self


RunStatus = Literal[
    "queued",
    "running",
    "waiting_input",
    "waiting_approval",
    "cancelling",
    "completed",
    "failed",
    "cancelled",
    "expired",
]
RunOutcome = Literal["success", "partial", "denied", "failed", "cancelled", "expired"]


class AgentRun(ContractModel):
    schema_version: Literal["1.1"] = "1.1"
    run_id: str
    session_id: str
    origin_client_instance_id: str
    client_capabilities: ClientCapabilities | None = None
    status: RunStatus
    outcome: RunOutcome | None = None
    completion_reason_code: str | None = None
    mode: Literal["agent", "workflow"] = "agent"
    workflow_ref: WorkflowRef | None = None
    input_message_id: str
    base_context_version: int = Field(ge=1)
    current_phase: str = "queued"
    waiting_for: str | None = None
    state_version: int = Field(default=1, ge=1)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    started_at: datetime | None = None
    completed_at: datetime | None = None

    @model_validator(mode="after")
    def validate_non_terminal_outcome(self) -> "AgentRun":
        terminal_statuses = {"completed", "failed", "cancelled", "expired"}
        if self.status not in terminal_statuses and self.outcome is not None:
            raise ValueError("non-terminal runs cannot have an outcome")
        return self


class FrontendCommandPreconditions(ContractModel):
    session_id: str = Field(min_length=1, max_length=128)
    area_code: str | None = Field(default=None, min_length=1, max_length=32)
    required_client_capability: Literal[
        "panel.show_table@1.0", "map.render_choropleth@1.0"
    ]


class PanelShowTablePayload(ContractModel):
    result_id: str = Field(min_length=1, max_length=128)


class MapRenderChoroplethPayload(ContractModel):
    result_id: str = Field(min_length=1, max_length=128)
    metric_field: Literal["person_count"] = "person_count"
    label_field: Literal["area_name"] = "area_name"
    area_code_field: Literal["area_code"] = "area_code"
    legend_title: str = Field(default="独居老人数量", min_length=1, max_length=100)
    palette: Literal["sequential_blue_5"] = "sequential_blue_5"
    fit_bounds: bool = True


class FrontendCommand(ContractModel):
    schema_version: Literal["1.1"] = "1.1"
    command_id: str = Field(min_length=1, max_length=128)
    run_id: str = Field(min_length=1, max_length=128)
    target_client_instance_id: str = Field(min_length=1, max_length=128)
    type: Literal["panel.show_table", "map.render_choropleth"]
    target: Literal["result_panel", "map_panel"] = "result_panel"
    issued_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    expires_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC) + timedelta(minutes=5)
    )
    preconditions: FrontendCommandPreconditions
    payload: PanelShowTablePayload | MapRenderChoroplethPayload

    @model_validator(mode="after")
    def validate_expiry(self) -> "FrontendCommand":
        if self.expires_at <= self.issued_at:
            raise ValueError("frontend command must expire after it is issued")
        expected = {
            "panel.show_table": (
                "result_panel",
                "panel.show_table@1.0",
                PanelShowTablePayload,
            ),
            "map.render_choropleth": (
                "map_panel",
                "map.render_choropleth@1.0",
                MapRenderChoroplethPayload,
            ),
        }[self.type]
        target, capability, payload_type = expected
        if self.target != target:
            raise ValueError("frontend command target does not match its type")
        if self.preconditions.required_client_capability != capability:
            raise ValueError("frontend command capability does not match its type")
        if not isinstance(self.payload, payload_type):
            raise ValueError("frontend command payload does not match its type")
        return self


FrontendCommandReceiptStatus = Literal[
    "accepted",
    "completed",
    "failed",
    "unsupported",
    "rejected_precondition",
    "expired",
]


class FrontendCommandReceiptError(ContractModel):
    code: str = Field(min_length=1, max_length=128)
    message: str = Field(min_length=1, max_length=1000)


class FrontendCommandReceipt(ContractModel):
    schema_version: Literal["1.1"] = "1.1"
    command_id: str = Field(min_length=1, max_length=128)
    client_instance_id: str = Field(min_length=1, max_length=128)
    status: FrontendCommandReceiptStatus
    received_at: datetime
    completed_at: datetime | None = None
    client_state: dict[str, object] = Field(default_factory=dict)
    error: FrontendCommandReceiptError | None = None

    @model_validator(mode="after")
    def validate_completion(self) -> "FrontendCommandReceipt":
        terminal = self.status != "accepted"
        if terminal and self.completed_at is None:
            raise ValueError("terminal frontend command receipt requires completed_at")
        if not terminal and self.completed_at is not None:
            raise ValueError("accepted frontend command receipt cannot be completed")
        return self


class PendingInputRequest(ContractModel):
    input_request_id: str
    run_id: str
    kind: Literal["clarification", "selection", "approval", "reauth"]
    prompt: str = Field(min_length=1, max_length=1000)
    options: list["InputOption"] = Field(default_factory=list, max_length=100)
    allow_free_text: bool = False
    run_state_version: int = Field(ge=1)
    expires_at: datetime
    closed_at: datetime | None = None


class InputOption(ContractModel):
    option_id: str = Field(min_length=1, max_length=128)
    label: str = Field(min_length=1, max_length=200)


class TextInputResponse(ContractModel):
    type: Literal["text"]
    text: str = Field(min_length=1, max_length=10_000)


class ChoiceInputResponse(ContractModel):
    type: Literal["choice"]
    option_id: str = Field(min_length=1, max_length=128)


class ApprovalInputResponse(ContractModel):
    type: Literal["approval"]
    decision: Literal["approve", "reject"]


class ReauthenticationInputResponse(ContractModel):
    type: Literal["reauthenticated"]


RunInputResponse = Annotated[
    TextInputResponse
    | ChoiceInputResponse
    | ApprovalInputResponse
    | ReauthenticationInputResponse,
    Field(discriminator="type"),
]


class RunInputBody(ContractModel):
    input_request_id: str = Field(min_length=1, max_length=128)
    client_instance_id: str = Field(min_length=1, max_length=128)
    run_state_version: int = Field(ge=1)
    response: RunInputResponse


class SessionContext(ContractModel):
    version: int = Field(default=1, ge=1)
    area: dict[str, str] | None = None
    selected_object_refs: list[dict[str, str]] = Field(default_factory=list)
    recent_result_refs: list[str] = Field(default_factory=list)


class AgentSession(ContractModel):
    schema_version: Literal["1.1"] = "1.1"
    session_id: str
    owner_user_id: str = Field(exclude=True)
    app_id: str = "full_information_view"
    title: str = Field(min_length=1, max_length=200)
    status: Literal["active", "archived"] = "active"
    active_run_id: str | None = None
    context: SessionContext = Field(default_factory=SessionContext)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    version: int = Field(default=1, ge=1)


class AgentEvent(ContractModel):
    schema_version: Literal["1.1"] = "1.1"
    event_id: str
    sequence: int = Field(ge=1)
    type: str = Field(min_length=1, max_length=100)
    session_id: str
    run_id: str
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    trace_id: str
    visibility: Literal["user"] = "user"
    data: dict[str, object]


class PopulationMetricRow(ContractModel):
    area_code: str
    area_name: str
    person_count: int = Field(ge=0)


class PopulationMetricTable(ContractModel):
    rows: list[PopulationMetricRow]


class TableDataResult(ContractModel):
    result_id: str
    kind: Literal["table"] = "table"
    data_schema_ref: str
    result_fingerprint: str
    payload_status: Literal["available", "expired"] = "available"
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    payload_expires_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC) + timedelta(hours=24)
    )
    evidence_ids: list[str] = Field(default_factory=list)
    inline: bool = True
    data: PopulationMetricTable
    row_count: int = Field(ge=0)
    truncated: bool = False


class AreaCandidate(ContractModel):
    area_code: str = Field(min_length=1, max_length=32)
    area_name: str = Field(min_length=1, max_length=200)
    level: Literal["province", "city", "district", "street", "community", "grid"]
    parent_area_code: str | None = Field(default=None, max_length=32)
    bounds: tuple[float, float, float, float] | None = None


class AreaCandidatesData(ContractModel):
    resolved_area_code: str | None = Field(default=None, max_length=32)
    ambiguous: bool
    candidates: list[AreaCandidate] = Field(max_length=20)


class AreaCandidatesResult(ContractModel):
    result_id: str
    kind: Literal["area_candidates"] = "area_candidates"
    data_schema_ref: str
    result_fingerprint: str
    payload_status: Literal["available", "expired"] = "available"
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    payload_expires_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC) + timedelta(hours=24)
    )
    evidence_ids: list[str] = Field(default_factory=list)
    inline: bool = True
    data: AreaCandidatesData
    candidate_count: int = Field(ge=0, le=20)


class ObjectProfileField(ContractModel):
    field_id: str = Field(min_length=1, max_length=100)
    label: str = Field(min_length=1, max_length=100)
    value: str | int | float | bool | None
    classification: Literal["public", "internal", "sensitive"]
    masked: bool = False


class ObjectProfileData(ContractModel):
    object_ref: GovernanceObjectRef
    area_code: str = Field(min_length=1, max_length=32)
    title: str = Field(min_length=1, max_length=200)
    fields: list[ObjectProfileField] = Field(max_length=100)


class ObjectProfileResult(ContractModel):
    result_id: str
    kind: Literal["object_profile"] = "object_profile"
    data_schema_ref: str
    result_fingerprint: str
    payload_status: Literal["available", "expired"] = "available"
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    payload_expires_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC) + timedelta(hours=24)
    )
    evidence_ids: list[str] = Field(default_factory=list)
    inline: bool = True
    data: ObjectProfileData


DataResult = Annotated[
    AreaCandidatesResult | TableDataResult | ObjectProfileResult,
    Field(discriminator="kind"),
]


class ResultMetadata(ContractModel):
    result_id: str
    kind: Literal["area_candidates", "table", "object_profile"]
    data_schema_ref: str
    result_fingerprint: str
    payload_status: Literal["expired"] = "expired"
    title: str
    summary: dict[str, object]
    evidence_ids: list[str] = Field(default_factory=list)
    payload_expires_at: datetime
    rerun_allowed: bool = True


class EvidenceMetricDefinition(ContractModel):
    metric_id: str
    definition_version: str


class EvidenceToolRef(ContractModel):
    tool_id: str
    tool_version: str


class EvidenceFreshness(ContractModel):
    status: Literal["current", "stale", "unknown"]
    expected_update_cycle: str | None = None


class Evidence(ContractModel):
    schema_version: Literal["1.1"] = "1.1"
    evidence_id: str
    result_id: str
    result_fingerprint: str
    source_system: str
    dataset_id: str
    dataset_snapshot_version: str | None = None
    semantic_registry_version: str | None = None
    metric_definitions: list[EvidenceMetricDefinition] = Field(default_factory=list)
    effective_area_codes: list[str] = Field(default_factory=list)
    time_range: dict[str, datetime] | None = None
    as_of: datetime | None = None
    retrieved_at: datetime
    query_fingerprint: str
    policy_fingerprint: str
    tool: EvidenceToolRef
    freshness: EvidenceFreshness


class ToolResultPolicy(ContractModel):
    decision: Literal["allow", "mask", "deny", "need_approval"]
    policy_decision_id: str
    pre_policy_decision_id: str | None = None
    post_policy_decision_id: str | None = None
    auth_context_fingerprint: str
    arguments_fingerprint: str
    request_fingerprint: str
    policy_fingerprint: str
    masked_fields: list[str] = Field(default_factory=list)


class ToolResult(ContractModel):
    schema_version: Literal["1.1"] = "1.1"
    tool_call_id: str
    tool_id: str
    tool_version: str
    status: Literal["success", "partial", "denied", "failed"]
    summary: str
    data_result: DataResult | None = None
    evidence_ids: list[str] = Field(default_factory=list)
    policy: ToolResultPolicy | None = None
    warnings: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_data_result(self) -> "ToolResult":
        if self.status in {"success", "partial"} and self.data_result is None:
            raise ValueError("successful and partial tool results require data_result")
        return self


class Steer(ContractModel):
    steer_id: str
    run_id: str
    client_instance_id: str
    content: str = Field(min_length=1, max_length=10_000)
    delivery: Literal["next_safe_checkpoint"] = "next_safe_checkpoint"
    status: Literal["accepted", "applied"] = "accepted"
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    applied_at: datetime | None = None
