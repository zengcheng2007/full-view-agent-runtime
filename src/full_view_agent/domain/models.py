import re
from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Literal

from pydantic import ConfigDict, Field, JsonValue, field_validator, model_validator

from full_view_agent.domain.analysis_report import AnalysisReportDataResult
from full_view_agent.domain.capability import ToolSemanticContract
from full_view_agent.domain.contract_model import ContractModel


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

    def authorization_area_scope(self) -> "MetricQueryScope | None":
        area_code = self.context_area_code or self.parent_area_code
        if area_code is None:
            return None
        return MetricQueryScope(area_code=area_code)


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


class PopulationMetricOrder(ContractModel):
    field: Literal["person_count"]
    direction: Literal["asc", "desc"] = "asc"


class PopulationMetricQuerySpec(ContractModel):
    schema_version: Literal["1.1"] = "1.1"
    metrics: list[Literal["person_count"]] = Field(min_length=1, max_length=1)
    operator: Literal[
        "list", "sum", "avg", "min", "max", "top", "bottom", "rank"
    ] = "list"
    scope: MetricQueryScope
    filters: list[PopulationMetricFilter] = Field(default_factory=list, max_length=10)
    group_by: list[
        Literal[
            "district",
            "street",
            "community",
            "grid",
            "descendant_street",
            "descendant_community",
            "gender",
            "age_band",
        ]
    ] = Field(default_factory=list, max_length=2)
    order_by: list[PopulationMetricOrder] = Field(default_factory=list, max_length=1)
    limit: int = Field(default=200, ge=1, le=1000)
    presentation_hint: Literal["table", "metric", "choropleth"] = "table"


class QueryPopulationMetricsInput(ContractModel):
    query: PopulationMetricQuerySpec

    def authorization_area_scope(self) -> MetricQueryScope:
        return self.query.scope


class HousingMetricQuerySpec(ContractModel):
    schema_version: Literal["1.1"] = "1.1"
    metrics: list[
        Literal["dwelling_count", "building_count", "room_count"]
    ] = Field(default_factory=lambda: ["dwelling_count"], min_length=1, max_length=2)
    scope: MetricQueryScope
    group_by: list[
        Literal["next_area", "descendant_street", "room_use"]
    ] = Field(
        default_factory=list,
        max_length=1,
    )
    limit: int = Field(default=200, ge=1, le=1000)

    @model_validator(mode="after")
    def validate_metric_shape(self) -> "HousingMetricQuerySpec":
        if self.metrics == ["dwelling_count"]:
            return self
        if self.metrics == ["building_count", "room_count"] and not self.group_by:
            return self
        raise ValueError(
            "housing metrics 仅支持 dwelling_count，或不分组的 "
            "[building_count, room_count] 存量总览"
        )


class QueryHousingMetricsInput(ContractModel):
    query: HousingMetricQuerySpec

    def authorization_area_scope(self) -> MetricQueryScope:
        return self.query.scope


_ISO_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")


class EventMetricTimeRange(ContractModel):
    start: date
    end: date

    @field_validator("start", "end", mode="before")
    @classmethod
    def require_iso_calendar_date(cls, value: object) -> object:
        if not isinstance(value, str) or _ISO_DATE.fullmatch(value) is None:
            raise ValueError("事件趋势日期必须使用 yyyy-MM-dd 格式")
        return value

    @model_validator(mode="after")
    def validate_controlled_range(self) -> "EventMetricTimeRange":
        if self.start < date(2021, 1, 1):
            raise ValueError("事件趋势起始日期不得早于 2021-01-01")
        if self.end < self.start:
            raise ValueError("事件趋势结束日期不得早于起始日期")
        calendar_months = (
            (self.end.year - self.start.year) * 12
            + self.end.month
            - self.start.month
            + 1
        )
        if calendar_months > 24:
            raise ValueError("事件趋势最多查询 24 个自然月")
        return self


class EventMetricQuerySpec(ContractModel):
    schema_version: Literal["1.1"] = "1.1"
    metrics: list[Literal["finish_rate", "event_count"]] = Field(
        default_factory=lambda: ["finish_rate"],
        min_length=1,
        max_length=1,
    )
    scope: MetricQueryScope
    group_by: list[Literal["month", "event_category"]] = Field(
        default_factory=list, max_length=1
    )
    time_range: EventMetricTimeRange | None = None
    limit: int = Field(default=200, ge=1, le=1000)

    @model_validator(mode="after")
    def validate_metric_shape(self) -> "EventMetricQuerySpec":
        if (
            self.metrics == ["finish_rate"]
            and not self.group_by
            and self.time_range is None
        ):
            return self
        if (
            self.metrics == ["event_count"]
            and self.group_by == ["month"]
            and self.time_range is not None
        ):
            return self
        if (
            self.metrics == ["event_count"]
            and self.group_by == ["event_category"]
            and self.time_range is None
        ):
            return self
        raise ValueError(
            "事件指标仅支持办结率快照、带时间范围的事件总数月度趋势，"
            "或无时间范围的网格事件一级分类统计"
        )


class QueryEventMetricsInput(ContractModel):
    query: EventMetricQuerySpec

    def authorization_area_scope(self) -> MetricQueryScope:
        return self.query.scope


class GovernanceOverviewQuerySpec(ContractModel):
    schema_version: Literal["1.1"] = "1.1"
    scope: MetricQueryScope


class QueryGovernanceOverviewInput(ContractModel):
    query: GovernanceOverviewQuerySpec

    def authorization_area_scope(self) -> MetricQueryScope:
        return self.query.scope


class GovernancePowerMetricQuerySpec(ContractModel):
    schema_version: Literal["1.0"] = "1.0"
    scope: MetricQueryScope


class QueryGovernancePowerMetricsInput(ContractModel):
    query: GovernancePowerMetricQuerySpec

    def authorization_area_scope(self) -> MetricQueryScope:
        return self.query.scope


class EnterpriseMetricQuerySpec(ContractModel):
    schema_version: Literal["1.1"] = "1.1"
    scope: MetricQueryScope
    group_by: list[
        Literal["next_area", "enterprise_type", "enterprise_scale", "industry_name"]
    ] = Field(
        min_length=1,
        max_length=1,
    )
    limit: int = Field(default=200, ge=1, le=1000)


class QueryEnterpriseMetricsInput(ContractModel):
    query: EnterpriseMetricQuerySpec

    def authorization_area_scope(self) -> MetricQueryScope:
        return self.query.scope


class GovernanceObjectRef(ContractModel):
    object_type: Literal["person", "building", "room", "enterprise", "event"]
    object_id: str = Field(min_length=1, max_length=128)


class GetObjectProfileInput(ContractModel):
    object_ref: GovernanceObjectRef
    scope: MetricQueryScope
    field_sets: list[
        Literal["summary", "demographics", "location", "governance_status", "contact"]
    ] = Field(default_factory=lambda: ["summary"], min_length=1, max_length=5)

    def authorization_area_scope(self) -> MetricQueryScope:
        return self.scope


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
    semantic_contract: ToolSemanticContract | None = None


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
    mode: Literal["agent", "workflow", "analysis"] = "agent"
    inference_mode: Literal["fast", "auto", "deep"] | None = None
    workflow_ref: WorkflowRef | None = None

    @model_validator(mode="after")
    def validate_workflow_reference(self) -> "RunCreateRequest":
        if self.mode == "workflow" and self.workflow_ref is None:
            raise ValueError("workflow_ref is required when mode=workflow")
        if self.mode != "workflow" and self.workflow_ref is not None:
            raise ValueError("workflow_ref is only allowed when mode=workflow")
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
    mode: Literal["agent", "workflow", "analysis"] = "agent"
    inference_mode: Literal["fast", "auto", "deep"] | None = None
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
        "panel.show_table@1.0",
        "map.render_choropleth@1.0",
        "map.render_points@1.0",
        "map.render_cluster@1.0",
        "map.render_heatmap@1.0",
        "map.zoom_to@1.0",
        "map.highlight_area@1.0",
        "layer.clear@1.0",
        "panel.show_summary@1.0",
        "route.navigate@1.0",
    ]


class PanelShowTablePayload(ContractModel):
    result_id: str = Field(min_length=1, max_length=128)


class MapRenderChoroplethPayload(ContractModel):
    result_id: str = Field(min_length=1, max_length=128)
    metric_field: Literal[
        "person_count", "dwelling_count", "enterprise_count"
    ] = "person_count"
    label_field: Literal["area_name"] = "area_name"
    area_code_field: Literal["area_code"] = "area_code"
    legend_title: str = Field(default="人口数量", min_length=1, max_length=100)
    palette: Literal["sequential_blue_5"] = "sequential_blue_5"
    fit_bounds: bool = True


class MapRenderHolographicPayload(ContractModel):
    result_id: str = Field(min_length=1, max_length=128)
    lng_field: str = Field(min_length=1, max_length=128)
    lat_field: str = Field(min_length=1, max_length=128)
    color_field: str | None = Field(default=None, min_length=1, max_length=128)
    radius_field: str | None = Field(default=None, min_length=1, max_length=128)
    weight_field: str | None = Field(default=None, min_length=1, max_length=128)
    label_field: str | None = Field(default=None, min_length=1, max_length=128)
    default_color: str = Field(default="#0187e6", pattern=r"^#[0-9a-fA-F]{6}$")
    default_radius: float = Field(default=6, ge=1, le=100)
    fit_bounds: bool = True


class MapZoomToPayload(ContractModel):
    center: tuple[float, float] | None = None
    zoom: float | None = Field(default=None, ge=0, le=22)
    bounds: tuple[tuple[float, float], tuple[float, float]] | None = None
    fit_features: bool = False

    @model_validator(mode="after")
    def validate_zoom_target(self) -> "MapZoomToPayload":
        if self.zoom is None and self.bounds is None and not self.fit_features:
            raise ValueError("map zoom command requires a target")
        return self


class MapHighlightStyle(ContractModel):
    color: str = Field(default="#ff6b35", pattern=r"^#[0-9a-fA-F]{6}$")
    opacity: float = Field(default=0.5, ge=0, le=1)


class MapHighlightAreaPayload(ContractModel):
    area_code: str = Field(min_length=1, max_length=32)
    style: MapHighlightStyle = Field(default_factory=MapHighlightStyle)


class LayerClearPayload(ContractModel):
    clear_data_only: bool = False
    layer_ids: list[str] = Field(default_factory=list, max_length=100)


class SummaryItem(ContractModel):
    label: str = Field(min_length=1, max_length=100)
    value: str | int | float | bool
    unit: str | None = Field(default=None, max_length=32)


class PanelShowSummaryPayload(ContractModel):
    title: str = Field(min_length=1, max_length=100)
    items: list[SummaryItem] = Field(min_length=1, max_length=100)


class RouteNavigatePayload(ContractModel):
    path: str = Field(min_length=1, max_length=2048)

    @model_validator(mode="after")
    def validate_local_path(self) -> "RouteNavigatePayload":
        if not self.path.startswith("/") or self.path.startswith("//"):
            raise ValueError("route navigation requires a local absolute path")
        return self


class FrontendCommand(ContractModel):
    schema_version: Literal["1.1"] = "1.1"
    command_id: str = Field(min_length=1, max_length=128)
    run_id: str = Field(min_length=1, max_length=128)
    target_client_instance_id: str = Field(min_length=1, max_length=128)
    type: Literal[
        "panel.show_table",
        "map.render_choropleth",
        "map.render_points",
        "map.render_cluster",
        "map.render_heatmap",
        "map.zoom_to",
        "map.highlight_area",
        "layer.clear",
        "panel.show_summary",
        "route.navigate",
    ]
    target: Literal["result_panel", "map_panel", "summary_panel", "app_router"] = (
        "result_panel"
    )
    issued_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    expires_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC) + timedelta(minutes=5)
    )
    preconditions: FrontendCommandPreconditions
    payload: (
        PanelShowTablePayload
        | MapRenderChoroplethPayload
        | MapRenderHolographicPayload
        | MapZoomToPayload
        | MapHighlightAreaPayload
        | LayerClearPayload
        | PanelShowSummaryPayload
        | RouteNavigatePayload
    )

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
            "map.render_points": (
                "map_panel",
                "map.render_points@1.0",
                MapRenderHolographicPayload,
            ),
            "map.render_cluster": (
                "map_panel",
                "map.render_cluster@1.0",
                MapRenderHolographicPayload,
            ),
            "map.render_heatmap": (
                "map_panel",
                "map.render_heatmap@1.0",
                MapRenderHolographicPayload,
            ),
            "map.zoom_to": ("map_panel", "map.zoom_to@1.0", MapZoomToPayload),
            "map.highlight_area": (
                "map_panel",
                "map.highlight_area@1.0",
                MapHighlightAreaPayload,
            ),
            "layer.clear": ("map_panel", "layer.clear@1.0", LayerClearPayload),
            "panel.show_summary": (
                "summary_panel",
                "panel.show_summary@1.0",
                PanelShowSummaryPayload,
            ),
            "route.navigate": (
                "app_router",
                "route.navigate@1.0",
                RouteNavigatePayload,
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
    "rejected_protocol",
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
    analysis_plan_id: str | None = Field(default=None, min_length=1, max_length=128)
    analysis_request_id: str | None = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode="after")
    def validate_analysis_references(self) -> "PendingInputRequest":
        if (self.analysis_plan_id is None) != (self.analysis_request_id is None):
            raise ValueError(
                "analysis_plan_id and analysis_request_id must be supplied together"
            )
        return self


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
    analysis_plan_id: str | None = Field(default=None, min_length=1, max_length=128)
    analysis_request_id: str | None = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode="after")
    def validate_analysis_references(self) -> "RunInputBody":
        if (self.analysis_plan_id is None) != (self.analysis_request_id is None):
            raise ValueError(
                "analysis_plan_id and analysis_request_id must be supplied together"
            )
        return self


class SessionContext(ContractModel):
    version: int = Field(default=1, ge=1)
    area: dict[str, str] | None = None
    selected_object_refs: list[dict[str, str]] = Field(default_factory=list)
    recent_result_refs: list[str] = Field(default_factory=list)


class AgentSession(ContractModel):
    schema_version: Literal["1.1"] = "1.1"
    session_id: str
    owner_tenant_id: str = Field(default="legacy", exclude=True)
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
    area_code: str = Field(title="区域编码")
    area_name: str = Field(title="区域")
    person_count: int = Field(
        ge=0,
        title="人口数",
        json_schema_extra={"unit": "人"},
    )


class PopulationMetricTable(ContractModel):
    rows: list[PopulationMetricRow]


class PopulationRankingRow(ContractModel):
    rank: int = Field(ge=1, title="排名")
    area_code: str = Field(title="区域编码")
    area_name: str = Field(title="区域")
    person_count: int = Field(
        ge=0,
        title="人口数",
        json_schema_extra={"unit": "人"},
    )


class PopulationRankingTable(ContractModel):
    rows: list[PopulationRankingRow]
    candidate_count: int | None = Field(default=None, ge=0, title="参与比较区划数")
    tie_policy: Literal["include_all"] = Field(
        default="include_all", title="并列处理规则"
    )


class PopulationAggregateRow(ContractModel):
    operator: Literal["sum", "avg", "min", "max"] = Field(title="聚合运算")
    metric: Literal["person_count"] = Field(title="指标")
    value: float = Field(ge=0, title="聚合值")
    area_count: int = Field(ge=0, title="参与聚合区划数")
    completeness: Literal["complete", "partial", "unknown"] = Field(
        title="完整性"
    )


class PopulationAggregateTable(ContractModel):
    rows: list[PopulationAggregateRow]


class HousingLeaseTypeRow(ContractModel):
    lease_type: str = Field(min_length=1, max_length=100, title="出租类型")
    dwelling_count: int = Field(
        ge=0,
        title="出租房数量",
        json_schema_extra={"unit": "套"},
    )


class HousingLeaseTypeTable(ContractModel):
    rows: list[HousingLeaseTypeRow]


class HousingAreaGroupRow(ContractModel):
    area_code: str = Field(min_length=1, max_length=32, title="区域编码")
    area_name: str = Field(min_length=1, max_length=200, title="区域")
    dwelling_count: int = Field(
        ge=0,
        title="出租房数量",
        json_schema_extra={"unit": "套"},
    )


class HousingAreaGroupTable(ContractModel):
    rows: list[HousingAreaGroupRow]


class HousingRoomUseRow(ContractModel):
    room_use: str = Field(min_length=1, max_length=100, title="户室用途")
    dwelling_count: int = Field(
        ge=0,
        title="户室数量",
        json_schema_extra={"unit": "套"},
    )


class HousingRoomUseTable(ContractModel):
    rows: list[HousingRoomUseRow]


class HousingStockOverviewRow(ContractModel):
    building_count: int = Field(
        ge=0,
        title="楼幢总数",
        json_schema_extra={"unit": "栋"},
    )
    room_count: int = Field(
        ge=0,
        title="户室总数",
        json_schema_extra={"unit": "间"},
    )


class HousingStockOverviewTable(ContractModel):
    rows: list[HousingStockOverviewRow]


class EventFinishRateRow(ContractModel):
    level: Literal["grid", "community", "street"] = Field(
        title="层级",
        json_schema_extra={
            "value_labels": {
                "grid": "网格",
                "community": "社区",
                "street": "街道",
            }
        },
    )
    finish_rate: float = Field(
        ge=0,
        le=100,
        title="事件办结率",
        json_schema_extra={"unit": "%"},
    )


class EventFinishRateTable(ContractModel):
    rows: list[EventFinishRateRow]


class EventTrendRow(ContractModel):
    month: str = Field(
        min_length=7,
        max_length=7,
        pattern=r"^[0-9]{4}-(0[1-9]|1[0-2])$",
        title="月份",
    )
    event_count: int = Field(
        ge=0,
        title="事件总数",
        json_schema_extra={"unit": "件"},
    )


class EventTrendTable(ContractModel):
    rows: list[EventTrendRow]


class EventCategoryRow(ContractModel):
    category_code: str = Field(min_length=1, max_length=64, title="分类编码")
    category_name: str = Field(min_length=1, max_length=200, title="一级分类")
    event_count: int = Field(
        ge=0, title="事件数量", json_schema_extra={"unit": "件"}
    )


class EventCategoryTable(ContractModel):
    rows: list[EventCategoryRow]


class GovernanceOverviewRow(ContractModel):
    subject: Literal["person", "house", "enterprise", "event", "matter"] = Field(
        title="治理要素",
        json_schema_extra={
            "value_labels": {
                "person": "人",
                "house": "房",
                "enterprise": "企",
                "event": "事",
                "matter": "物",
            }
        },
    )
    subject_label: str = Field(min_length=1, max_length=20, title="治理要素名称")
    related_count: int = Field(
        ge=0, title="治理关联数", json_schema_extra={"unit": "个"}
    )
    total_count: int = Field(ge=0, title="要素总数", json_schema_extra={"unit": "个"})
    coverage_rate: float = Field(
        ge=0, title="治理覆盖率", json_schema_extra={"unit": "%"}
    )


class GovernanceOverviewTable(ContractModel):
    rows: list[GovernanceOverviewRow]


class GovernancePowerMetricRow(ContractModel):
    type_code: Literal["roomNum", "10", "11", "12", "13", "nGridSum", "gridUnitSum"]
    type_name: str = Field(min_length=1, max_length=20)
    count: int = Field(ge=0)


class GovernancePowerMetricTable(ContractModel):
    rows: list[GovernancePowerMetricRow]


class EnterpriseMetricRow(ContractModel):
    area_code: str = Field(min_length=1, max_length=32, title="区域编码")
    area_name: str = Field(min_length=1, max_length=200, title="区域")
    enterprise_count: int = Field(
        ge=0,
        title="企业数量",
        json_schema_extra={"unit": "家"},
    )


class EnterpriseMetricTable(ContractModel):
    rows: list[EnterpriseMetricRow]


class EnterpriseTypeDistributionRow(ContractModel):
    enterprise_type: str = Field(min_length=1, max_length=100, title="企业类型")
    enterprise_count: int = Field(
        ge=0,
        title="企业数量",
        json_schema_extra={"unit": "家"},
    )


class EnterpriseTypeDistributionTable(ContractModel):
    rows: list[EnterpriseTypeDistributionRow]


class EnterpriseScaleDistributionRow(ContractModel):
    enterprise_scale: str = Field(min_length=1, max_length=100, title="企业规模")
    enterprise_count: int = Field(
        ge=0,
        title="企业数量",
        json_schema_extra={"unit": "家"},
    )


class EnterpriseScaleDistributionTable(ContractModel):
    rows: list[EnterpriseScaleDistributionRow]


class EnterpriseIndustryDistributionRow(ContractModel):
    industry_name: str = Field(min_length=1, max_length=100, title="行业名称")
    enterprise_count: int = Field(
        ge=0,
        title="企业数量",
        json_schema_extra={"unit": "家"},
    )


class EnterpriseIndustryDistributionTable(ContractModel):
    rows: list[EnterpriseIndustryDistributionRow]


class DynamicTableData(ContractModel):
    """JSON-safe rows returned by a configured read-only Tool."""

    rows: list[dict[str, JsonValue]]


class ResultDisplayField(ContractModel):
    field: str = Field(min_length=1, max_length=100)
    label: str = Field(min_length=1, max_length=100)
    role: Literal["dimension", "metric", "identifier"]
    unit: str | None = Field(default=None, max_length=32)
    value_labels: dict[str, str] = Field(default_factory=dict)


class ResultVisualization(ContractModel):
    kind: Literal["table", "metric", "bar", "line", "choropleth"]
    title: str = Field(min_length=1, max_length=100)
    category_field: str | None = Field(default=None, max_length=100)
    value_field: str | None = Field(default=None, max_length=100)
    label_field: str | None = Field(default=None, max_length=100)
    area_code_field: str | None = Field(default=None, max_length=100)
    x_field: str | None = Field(default=None, max_length=100)
    y_field: str | None = Field(default=None, max_length=100)


class ResultDownload(ContractModel):
    formats: list[Literal["csv"]] = Field(min_length=1, max_length=1)
    path: str = Field(min_length=1, max_length=512)

    @model_validator(mode="after")
    def validate_local_path(self) -> "ResultDownload":
        if not self.path.startswith("/agent-api/v1/results/"):
            raise ValueError("result download path must be a local result API path")
        return self


class ResultPresentation(ContractModel):
    """Optional business-facing metadata; stable machine fields remain authoritative."""

    locale: Literal["zh-CN"] = "zh-CN"
    title: str = Field(min_length=1, max_length=100)
    summary: str = Field(min_length=1, max_length=500)
    status_label: str = Field(min_length=1, max_length=32)
    fields: list[ResultDisplayField] = Field(default_factory=list, max_length=100)
    visualizations: list[ResultVisualization] = Field(default_factory=list, max_length=10)
    download: ResultDownload | None = None


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
    data: (
        PopulationMetricTable
        | PopulationRankingTable
        | PopulationAggregateTable
        | HousingLeaseTypeTable
        | HousingAreaGroupTable
        | HousingRoomUseTable
        | HousingStockOverviewTable
        | EventFinishRateTable
        | EventTrendTable
        | EventCategoryTable
        | GovernanceOverviewTable
        | GovernancePowerMetricTable
        | EnterpriseMetricTable
        | EnterpriseTypeDistributionTable
        | EnterpriseScaleDistributionTable
        | EnterpriseIndustryDistributionTable
        | DynamicTableData
    )
    row_count: int = Field(ge=0)
    truncated: bool = False
    presentation: ResultPresentation | None = None


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
    presentation: ResultPresentation | None = None

    def authorization_area_scope(self) -> MetricQueryScope | None:
        if self.data.resolved_area_code is None:
            return None
        return MetricQueryScope(
            area_code=self.data.resolved_area_code,
            include_descendants=False,
        )


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

    def authorization_area_scope(self) -> MetricQueryScope:
        return MetricQueryScope(
            area_code=self.data.area_code,
            include_descendants=False,
        )


DataResult = Annotated[
    AreaCandidatesResult
    | TableDataResult
    | ObjectProfileResult
    | AnalysisReportDataResult,
    Field(discriminator="kind"),
]


class ResultMetadata(ContractModel):
    result_id: str
    kind: Literal["area_candidates", "table", "object_profile", "analysis_report"]
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


class EvidenceDisplay(ContractModel):
    locale: Literal["zh-CN"] = "zh-CN"
    title: str = Field(default="数据证据", min_length=1, max_length=100)
    source_label: str = Field(min_length=1, max_length=100)
    dataset_label: str = Field(min_length=1, max_length=100)
    tool_label: str = Field(min_length=1, max_length=100)
    area_summary: str = Field(min_length=1, max_length=300)
    freshness_label: str = Field(min_length=1, max_length=100)
    method_note: str | None = Field(default=None, min_length=1, max_length=300)


class Evidence(ContractModel):
    schema_version: Literal["1.1"] = "1.1"
    evidence_id: str
    result_id: str
    result_fingerprint: str
    source_system: str
    dataset_id: str
    dataset_snapshot_version: str | None = None
    semantic_registry_version: str | None = None
    semantic_contract_fingerprint: str | None = None
    semantic_shape_id: str | None = None
    semantic_operator: str | None = None
    semantic_completeness: str | None = None
    semantic_tie_policy: str | None = None
    metric_definitions: list[EvidenceMetricDefinition] = Field(default_factory=list)
    effective_area_codes: list[str] = Field(default_factory=list)
    time_range: dict[str, datetime] | None = None
    as_of: datetime | None = None
    retrieved_at: datetime
    query_fingerprint: str
    policy_fingerprint: str
    tool: EvidenceToolRef
    freshness: EvidenceFreshness
    display: EvidenceDisplay | None = None


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


class SemanticFilterLineage(ContractModel):
    """Server-resolved semantic filter context safe for answer rendering."""

    field: str = Field(min_length=1, max_length=64)
    operator: str = Field(min_length=1, max_length=64)
    value: str | int | float | bool | list[str | int | float] | None = None
    display_label: str | None = Field(default=None, min_length=1, max_length=100)


class SemanticResultLineage(ContractModel):
    """S1-A：``governance.semantic_query`` 解析执行的血缘。

    记录虚拟语义入口到真实能力 Tool 的可追溯链路：语义 spec 与计划
    的版本指纹、目录版本、主题/数据集、规范 Tool 标识与区域/输出形态。
    只由 SemanticToolExecutor 在成功执行后附加；Evidence 持久化与前端
    地图命令从该血缘读取规范 Tool 信息，而非解析原始模型参数。
    """

    schema_version: Literal["1.0"] = "1.0"
    virtual_tool_id: str = Field(min_length=1, max_length=128)
    virtual_tool_version: str = Field(min_length=1, max_length=32)
    spec_version: str = Field(min_length=1, max_length=32)
    catalog_version: str = Field(min_length=1, max_length=64)
    subject: str = Field(min_length=1, max_length=64)
    logical_dataset_id: str = Field(min_length=1, max_length=64)
    canonical_tool_id: str = Field(min_length=1, max_length=128)
    canonical_tool_version: str = Field(min_length=1, max_length=32)
    spec_fingerprint: str = Field(min_length=1, max_length=200)
    plan_fingerprint: str = Field(min_length=1, max_length=200)
    semantic_contract_fingerprint: str | None = Field(
        default=None, min_length=1, max_length=200
    )
    semantic_shape_id: str | None = Field(default=None, min_length=1, max_length=64)
    semantic_operator: str | None = Field(default=None, min_length=1, max_length=32)
    semantic_completeness: str | None = Field(default=None, min_length=1, max_length=32)
    semantic_tie_policy: str | None = Field(default=None, min_length=1, max_length=32)
    area_code: str = Field(min_length=1, max_length=32)
    output: str = Field(min_length=1, max_length=32)
    metric_definitions: list[EvidenceMetricDefinition] = Field(default_factory=list)
    filter_contexts: list[SemanticFilterLineage] = Field(default_factory=list)


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
    semantic_lineage: SemanticResultLineage | None = None

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
