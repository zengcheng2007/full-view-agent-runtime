"""P2-1 Capability Center domain models.

Unified Tool / Skill / Workflow with lifecycle management, publish snapshots,
and optimistic concurrency control.  The Agent Runtime reads only published
snapshots; the static ToolRegistry remains the seed source during migration.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import Field, JsonValue, field_validator, model_validator

from full_view_agent.domain.contract_model import ContractModel

CapabilityType = Literal["tool", "skill", "workflow"]
CapabilityStatus = Literal[
    "draft", "testing", "pending_approval", "published", "disabled"
]
RiskLevel = Literal["low", "medium", "high"]
HttpMethod = Literal["GET", "POST"]
SemanticOperator = Literal[
    "list", "sum", "avg", "min", "max", "top", "bottom", "rank"
]
SemanticFilterOperator = Literal[
    "eq", "ne", "in", "not_in", "gt", "gte", "lt", "lte", "between"
]
SemanticOutputForm = Literal[
    "table", "metric_card", "bar", "line", "choropleth", "csv"
]

_VALID_TRANSITIONS: dict[CapabilityStatus, set[CapabilityStatus]] = {
    "draft": {"testing"},
    "testing": {"pending_approval", "draft"},
    "pending_approval": {"published", "testing"},
    "published": {"disabled"},
    "disabled": {"draft"},
}

_SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")
_CAPABILITY_ID_RE = re.compile(r"^[a-z][a-z0-9]*(?:\.[a-z][a-z0-9_-]+)+$")


def is_valid_transition(
    from_status: CapabilityStatus, to_status: CapabilityStatus
) -> bool:
    return to_status in _VALID_TRANSITIONS.get(from_status, set())


class CapabilityBase(ContractModel):
    """Shared fields for all capability types."""

    capability_id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=200)
    capability_type: CapabilityType
    domain: str = Field(default="governance", max_length=64)
    owner: str = Field(min_length=1, max_length=100)
    version: str = Field(min_length=1, max_length=32)
    status: CapabilityStatus = "draft"
    risk_level: RiskLevel = "low"
    required_permissions: list[str] = Field(default_factory=list, max_length=20)
    dataset_ids: list[str] = Field(default_factory=list, max_length=20)
    description: str = Field(default="", max_length=2000)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    created_by: str = Field(default="system", max_length=100)
    updated_by: str = Field(default="system", max_length=100)
    etag: int = Field(default=1, ge=1)

    @field_validator("capability_id")
    @classmethod
    def validate_capability_id(cls, v: str) -> str:
        if not _CAPABILITY_ID_RE.match(v):
            raise ValueError(
                "capability_id must be dotted-lowercase like 'tool.resolve_area'"
            )
        return v

    @field_validator("version")
    @classmethod
    def validate_version(cls, v: str) -> str:
        if not _SEMVER_RE.match(v):
            raise ValueError("version must be semver (e.g. '1.0.0')")
        return v


class ConnectorRef(ContractModel):
    """Reference to an approved HTTP connector."""

    connector_id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=200)
    base_url: str = Field(min_length=1, max_length=500)
    allowed_path_prefixes: list[str] = Field(default_factory=list)


class ToolSemanticMetric(ContractModel):
    metric_id: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")
    label: str = Field(min_length=1, max_length=100)
    unit: str | None = Field(default=None, max_length=32)
    value_type: Literal["integer", "number"]
    intent_terms: tuple[str, ...] = ()


class ToolSemanticDimension(ContractModel):
    dimension_id: str = Field(
        min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$"
    )
    label: str = Field(min_length=1, max_length=100)
    kind: Literal["category", "administrative_area", "time"]
    intent_terms: tuple[str, ...] = ()


class ToolSemanticOperatorIntent(ContractModel):
    operator: SemanticOperator
    terms: tuple[str, ...] = Field(min_length=1)


class ToolSemanticFilter(ContractModel):
    field: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")
    label: str = Field(min_length=1, max_length=100)
    operators: tuple[SemanticFilterOperator, ...] = Field(min_length=1)
    allowed_values: tuple[str | int | float | bool, ...] = ()


class ToolSemanticSort(ContractModel):
    allowed_fields: tuple[str, ...] = Field(min_length=1)
    default_direction: Literal["asc", "desc"] = "desc"
    tie_policy: Literal["include_all", "secondary_sort"]
    tie_breakers: tuple[str, ...] = ()


class ToolSemanticCompleteness(ContractModel):
    mode: Literal["complete", "partial", "unknown"]
    statement: str = Field(min_length=1, max_length=500)


class ToolSemanticExample(ContractModel):
    question: str = Field(min_length=1, max_length=500)
    operator: SemanticOperator
    metric: str = Field(min_length=1, max_length=64)
    dimension: str | None = Field(default=None, max_length=64)


class ToolSemanticQueryShape(ContractModel):
    shape_id: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")
    metric_selection: tuple[str, ...] = Field(min_length=1)
    dimension_selection: tuple[str, ...]
    operator_selection: tuple[SemanticOperator, ...] = Field(min_length=1)
    scope_levels: tuple[Literal[4, 6, 9, 12, 15], ...] = Field(min_length=1)
    allowed_filters: tuple[str, ...]
    output_forms: tuple[SemanticOutputForm, ...] = Field(min_length=1)
    completeness: ToolSemanticCompleteness
    result_schema_ref: str = Field(min_length=1, max_length=300)
    result_row_fields: tuple[str, ...] = Field(min_length=1)
    result_fingerprint_domain: str = Field(min_length=1, max_length=300)
    argument_template: dict[str, JsonValue] | None = None

    @model_validator(mode="after")
    def require_exact_executable_shape(self) -> ToolSemanticQueryShape:
        if len(self.operator_selection) != 1:
            raise ValueError("semantic query shape must declare exactly one operator")
        if len(self.output_forms) != 1:
            raise ValueError("semantic query shape must declare exactly one output form")
        if len(self.result_row_fields) != len(set(self.result_row_fields)):
            raise ValueError("semantic query shape result fields must be unique")
        if self.argument_template is not None:
            _validate_semantic_argument_template(self.argument_template)
        return self


_SEMANTIC_ARGUMENT_PLACEHOLDERS = frozenset(
    {
        "$semantic.metrics",
        "$semantic.metric",
        "$semantic.operator",
        "$semantic.scope",
        "$semantic.scope.area_code",
        "$semantic.group_by",
        "$semantic.filters",
        "$semantic.order_by",
        "$semantic.limit",
        "$semantic.output",
        "$semantic.time_range",
    }
)


def _validate_semantic_argument_template(value: JsonValue) -> None:
    if isinstance(value, str) and value.startswith("$semantic."):
        if value not in _SEMANTIC_ARGUMENT_PLACEHOLDERS:
            raise ValueError(f"unknown semantic argument placeholder: {value}")
        return
    if isinstance(value, dict):
        for nested in value.values():
            _validate_semantic_argument_template(nested)
        return
    if isinstance(value, list):
        for nested in value:
            _validate_semantic_argument_template(nested)


class ToolSemanticContract(ContractModel):
    """Version-owned analytical meaning exposed by one exact Tool version."""

    schema_version: Literal["1.0"] = "1.0"
    subject: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")
    intent_terms: tuple[str, ...] = ()
    excluded_intent_terms: tuple[str, ...] = ()
    metrics: tuple[ToolSemanticMetric, ...] = Field(min_length=1)
    dimensions: tuple[ToolSemanticDimension, ...]
    filters: tuple[ToolSemanticFilter, ...]
    operators: tuple[SemanticOperator, ...] = Field(min_length=1)
    operator_intents: tuple[ToolSemanticOperatorIntent, ...] = ()
    sort: ToolSemanticSort
    completeness: ToolSemanticCompleteness
    output_forms: tuple[SemanticOutputForm, ...] = Field(min_length=1)
    examples: tuple[ToolSemanticExample, ...]
    limitations: tuple[str, ...]
    query_shapes: tuple[ToolSemanticQueryShape, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_references(self) -> ToolSemanticContract:
        metric_ids = [metric.metric_id for metric in self.metrics]
        dimension_ids = [dimension.dimension_id for dimension in self.dimensions]
        filter_fields = [item.field for item in self.filters]
        if len(metric_ids) != len(set(metric_ids)):
            raise ValueError("semantic metric ids must be unique")
        if len(dimension_ids) != len(set(dimension_ids)):
            raise ValueError("semantic dimension ids must be unique")
        if len(filter_fields) != len(set(filter_fields)):
            raise ValueError("semantic filter fields must be unique")
        if len(self.operators) != len(set(self.operators)):
            raise ValueError("semantic operators must be unique")
        operator_intents = [item.operator for item in self.operator_intents]
        if len(operator_intents) != len(set(operator_intents)):
            raise ValueError("semantic operator intents must be unique")
        if not set(operator_intents).issubset(self.operators):
            raise ValueError("semantic operator intent is not supported")
        term_groups = (
            self.intent_terms,
            self.excluded_intent_terms,
            *(metric.intent_terms for metric in self.metrics),
            *(dimension.intent_terms for dimension in self.dimensions),
            *(item.terms for item in self.operator_intents),
        )
        for terms in term_groups:
            if any(not term.strip() for term in terms):
                raise ValueError("semantic intent terms must not contain blank values")
            if len(terms) != len(set(terms)):
                raise ValueError("semantic intent terms must be unique")
        operator_term_owner: dict[str, SemanticOperator] = {}
        for item in self.operator_intents:
            for term in item.terms:
                owner = operator_term_owner.setdefault(term, item.operator)
                if owner != item.operator:
                    raise ValueError("semantic operator terms must not be ambiguous")
        known_sort_fields = set(metric_ids).union(dimension_ids)
        for field in (*self.sort.allowed_fields, *self.sort.tie_breakers):
            if field not in known_sort_fields:
                raise ValueError(f"semantic sort field is not declared: {field}")
        for example in self.examples:
            if example.operator not in self.operators:
                raise ValueError("semantic example operator is not supported")
            if example.metric not in metric_ids:
                raise ValueError("semantic example metric is not declared")
            if example.dimension is not None and example.dimension not in dimension_ids:
                raise ValueError("semantic example dimension is not declared")
        if any(not item.strip() for item in self.limitations):
            raise ValueError("semantic limitations must not contain blank values")
        shape_ids = [shape.shape_id for shape in self.query_shapes]
        if len(shape_ids) != len(set(shape_ids)):
            raise ValueError("semantic query shape ids must be unique")
        for shape in self.query_shapes:
            if not set(shape.metric_selection).issubset(metric_ids):
                raise ValueError("semantic query shape references an unknown metric")
            if not set(shape.dimension_selection).issubset(dimension_ids):
                raise ValueError("semantic query shape references an unknown dimension")
            if not set(shape.operator_selection).issubset(self.operators):
                raise ValueError("semantic query shape references an unknown operator")
            if not set(shape.allowed_filters).issubset(filter_fields):
                raise ValueError("semantic query shape references an unknown filter")
            if not set(shape.output_forms).issubset(self.output_forms):
                raise ValueError("semantic query shape references an unknown output")
        for example in self.examples:
            dimensions = (example.dimension,) if example.dimension is not None else ()
            if not any(
                shape.metric_selection == (example.metric,)
                and shape.dimension_selection == dimensions
                and example.operator in shape.operator_selection
                for shape in self.query_shapes
            ):
                raise ValueError("semantic example does not match a declared query shape")
        if self.intent_terms:
            if any(not metric.intent_terms for metric in self.metrics):
                raise ValueError("intent-routed semantic metrics require intent terms")
            if any(not dimension.intent_terms for dimension in self.dimensions):
                raise ValueError("intent-routed semantic dimensions require intent terms")
            declared_operator_intents = {item.operator for item in self.operator_intents}
            if declared_operator_intents != set(self.operators):
                raise ValueError(
                    "intent-routed semantic operators require exact intent terms"
                )
            if any(shape.argument_template is None for shape in self.query_shapes):
                raise ValueError(
                    "intent-routed semantic query shapes require argument templates"
                )
        return self


class ToolCapability(CapabilityBase):
    """Atomic read-only HTTP tool capability."""

    capability_type: Literal["tool"] = "tool"
    connector_ref: str = Field(min_length=1, max_length=128)
    http_method: HttpMethod = "GET"
    resource_path: str = Field(min_length=1, max_length=500)
    input_schema: dict[str, object] = Field(default_factory=dict)
    output_schema: dict[str, object] = Field(default_factory=dict)
    parameter_mapping: dict[str, object] = Field(default_factory=dict)
    result_mapping: dict[str, object] = Field(default_factory=dict)
    result_kind: Literal[
        "area_candidates", "table", "metric", "object_profile"
    ] = "table"
    data_schema_ref: str = Field(default="", max_length=200)
    timeout_ms: int = Field(default=8000, ge=100, le=120_000)
    max_attempts: int = Field(default=2, ge=1, le=5)
    max_result_rows: int = Field(default=1000, ge=1, le=10_000)
    cache_enabled: bool = True
    cache_ttl_seconds: int = Field(default=60, ge=0, le=86_400)
    credential_ref: str | None = Field(default=None, max_length=128)
    semantic_contract: ToolSemanticContract | None = None

    @field_validator("resource_path")
    @classmethod
    def validate_resource_path(cls, v: str) -> str:
        if not v.startswith("/"):
            raise ValueError("resource_path must start with /")
        if ".." in v or "//" in v:
            raise ValueError("resource_path must not contain .. or //")
        return v


class SkillCapability(CapabilityBase):
    """Model usage guidance with tool whitelist."""

    capability_type: Literal["skill"] = "skill"
    applicable_questions: list[str] = Field(default_factory=list, max_length=50)
    guidance: str = Field(default="", max_length=5000)
    allowed_tool_ids: list[str] = Field(default_factory=list, max_length=50)
    input_constraints: dict[str, object] = Field(default_factory=dict)
    output_constraints: dict[str, object] = Field(default_factory=dict)
    examples: list[dict[str, str]] = Field(default_factory=list, max_length=20)
    counter_examples: list[dict[str, str]] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def validate_published_has_tools(self) -> SkillCapability:
        if self.status == "published" and not self.allowed_tool_ids:
            raise ValueError(
                "published skills must declare at least one allowed tool"
            )
        return self


class WorkflowNodeDefinition(ContractModel):
    """A single node in a workflow graph."""

    node_id: str = Field(min_length=1, max_length=64)
    node_type: Literal[
        "start",
        "tool",
        "skill",
        "condition",
        "join",
        "human_confirmation",
        "summary",
        "end",
    ]
    tool_capability_id: str | None = None
    tool_version: str | None = None
    skill_capability_id: str | None = None
    skill_version: str | None = None
    condition_expression: str | None = None
    config: dict[str, object] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_tool_node(self) -> WorkflowNodeDefinition:
        if self.node_type == "tool" and not self.tool_capability_id:
            raise ValueError("tool node requires tool_capability_id")
        if self.node_type == "skill" and not self.skill_capability_id:
            raise ValueError("skill node requires skill_capability_id")
        return self


class WorkflowEdgeDefinition(ContractModel):
    """Directed edge between workflow nodes."""

    source_node_id: str = Field(min_length=1, max_length=64)
    target_node_id: str = Field(min_length=1, max_length=64)
    condition: str | None = None


class WorkflowCapability(CapabilityBase):
    """Controlled multi-step orchestration."""

    capability_type: Literal["workflow"] = "workflow"
    nodes: list[WorkflowNodeDefinition] = Field(default_factory=list, max_length=50)
    edges: list[WorkflowEdgeDefinition] = Field(default_factory=list, max_length=100)
    timeout_seconds: int = Field(default=300, ge=10, le=3600)
    requires_human_confirmation: bool = False


Capability = Annotated[
    ToolCapability | SkillCapability | WorkflowCapability,
    Field(discriminator="capability_type"),
]


class CapabilitySnapshot(ContractModel):
    """Immutable published snapshot of a capability version."""

    snapshot_id: str = Field(min_length=1, max_length=128)
    capability_id: str = Field(min_length=1, max_length=128)
    capability_type: CapabilityType
    version: str = Field(min_length=1, max_length=32)
    published_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    published_by: str = Field(min_length=1, max_length=100)
    content: dict[str, object]
    is_active: bool = True


class CapabilityLifecycleEvent(ContractModel):
    """Audit trail for lifecycle transitions."""

    event_id: str = Field(min_length=1, max_length=128)
    capability_id: str = Field(min_length=1, max_length=128)
    from_status: CapabilityStatus
    to_status: CapabilityStatus
    version: str
    changed_by: str = Field(min_length=1, max_length=100)
    changed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    reason: str = Field(default="", max_length=500)


class Connector(ContractModel):
    """Approved HTTP connector for tool capabilities."""

    connector_id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=200)
    base_url: str = Field(min_length=1, max_length=500)
    description: str = Field(default="", max_length=500)
    allowed_path_prefixes: list[str] = Field(default_factory=list)
    denied_hosts: list[str] = Field(default_factory=list)
    is_active: bool = True
    credential_ref: str | None = None
    timeout_ms: int = Field(default=8000, ge=100, le=120_000)
    created_by: str = Field(default="system", min_length=1, max_length=100)
    updated_by: str = Field(default="system", min_length=1, max_length=100)
    etag: int = Field(default=1, ge=1)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, v: str) -> str:
        if not v.startswith(("http://", "https://")):
            raise ValueError("base_url must start with http:// or https://")
        return v


class ConnectorAuditEvent(ContractModel):
    event_id: str = Field(min_length=1, max_length=128)
    connector_id: str = Field(min_length=1, max_length=128)
    action: Literal["update", "enable", "disable"]
    actor: str = Field(min_length=1, max_length=100)
    reason: str = Field(min_length=1, max_length=2000)
    previous_etag: int = Field(ge=1)
    new_etag: int = Field(ge=2)
    changed_fields: list[str] = Field(default_factory=list)
    from_active: bool
    to_active: bool
    changed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ModelConfig(ContractModel):
    """Simplified model provider configuration (P2-2)."""

    config_id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=100)
    api_base_url: str = Field(min_length=1, max_length=500)
    model_name: str = Field(min_length=1, max_length=100)
    protocol: str = Field(default="openai_compatible", max_length=50)
    timeout_seconds: int = Field(default=60, ge=5, le=600)
    max_output_tokens: int = Field(default=32000, ge=100, le=128000)
    max_retries: int = Field(default=1, ge=0, le=5)
    is_enabled: bool = False
    notes: str = Field(default="", max_length=500)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    created_by: str = Field(default="system", max_length=100)
    version: int = Field(default=1, ge=1)


class ModelConfigMasked(ContractModel):
    """Model config with masked API key - safe for API responses."""

    config_id: str
    name: str
    api_base_url: str
    api_key_masked: str = "***"
    model_name: str
    protocol: str
    timeout_seconds: int
    max_output_tokens: int
    max_retries: int
    is_enabled: bool
    notes: str
    created_at: datetime
    updated_at: datetime
    created_by: str
    version: int


class ModelConfigWithKey(ContractModel):
    """Internal model config with resolved API key - never expose to API."""

    config_id: str
    name: str
    api_base_url: str
    api_key_secret: str  # resolved plaintext for runtime use only
    model_name: str
    protocol: str
    timeout_seconds: int
    max_output_tokens: int
    max_retries: int
    is_enabled: bool


class ConnectionTestResult(ContractModel):
    """Result of testing a model config connection."""

    success: bool
    latency_ms: int | None = None
    model_responded: str | None = None
    error_code: str | None = None
    error_message: str | None = None
