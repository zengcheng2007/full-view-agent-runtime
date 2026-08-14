"""P2 capability center API routes.

Mounted under ``/capability-api/v1/`` alongside the existing ``/agent-api/v1/``.
Authentication may use the transitional legacy identity adapter, while every
operation is authorized against an explicit control-plane permission.
"""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable
from typing import Annotated, Literal

from fastapi import APIRouter, Depends
from pydantic import Field, field_validator

from full_view_agent.api._api_deps import (
    CurrentUser,
    ResponseMeta,
    require_capability_identity,
)
from full_view_agent.application.application_management_service import (
    ApplicationManagementService,
)
from full_view_agent.application.capability_management_service import (
    CapabilityManagementService,
)
from full_view_agent.application.control_plane_authorization import (
    ControlPlaneAuthorizer,
    ControlPlanePermission,
)
from full_view_agent.application.dynamic_skill_workflow_bridge import (
    RuntimeWorkflowGraphSnapshot,
    build_runtime_skill_contract,
    build_runtime_workflow_snapshot,
)
from full_view_agent.application.dynamic_tool_bridge import (
    build_dynamic_input_schemas,
    build_dynamic_tool_registry_entries,
)
from full_view_agent.application.errors import ResourceNotFound, RunStateConflict
from full_view_agent.application.model_config_service import ModelConfigService
from full_view_agent.application.runtime_skill_registry import RuntimeSkillRegistry
from full_view_agent.application.runtime_workflow_registry import RuntimeWorkflowRegistry
from full_view_agent.application.session_run_service import new_id
from full_view_agent.application.tool_registry import ToolRegistry
from full_view_agent.domain.application import AgentApplicationDefinition
from full_view_agent.domain.capability import (
    CapabilityStatus,
    Connector,
    SkillCapability,
    ToolCapability,
    WorkflowCapability,
    WorkflowEdgeDefinition,
    WorkflowNodeDefinition,
)
from full_view_agent.domain.contract_model import ContractModel
from full_view_agent.domain.models import WorkflowRef
from full_view_agent.infrastructure.http_connector_executor import (
    ConnectorConnectionTester,
)

_MODEL_MAX_OUTPUT_TOKENS_MIN = 100
_MODEL_MAX_OUTPUT_TOKENS_MAX = 128_000


def _environment_model_max_output_tokens() -> int:
    """Read and normalise the environment model token limit safely."""

    variable = "FULL_VIEW_MODEL_MAX_OUTPUT_TOKENS"
    raw_value = os.getenv(variable, "32000").strip()
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise RunStateConflict(
            f"{variable} must be an integer between "
            f"{_MODEL_MAX_OUTPUT_TOKENS_MIN} and "
            f"{_MODEL_MAX_OUTPUT_TOKENS_MAX}"
        ) from exc
    if value < _MODEL_MAX_OUTPUT_TOKENS_MIN:
        raise RunStateConflict(
            f"{variable} must be an integer between "
            f"{_MODEL_MAX_OUTPUT_TOKENS_MIN} and "
            f"{_MODEL_MAX_OUTPUT_TOKENS_MAX}"
        )
    return min(value, _MODEL_MAX_OUTPUT_TOKENS_MAX)


# ---- Request / Response bodies ----


class ToolCreateBody(ContractModel):
    capability_id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=200)
    owner: str = Field(min_length=1, max_length=100)
    version: str = Field(min_length=1, max_length=32)
    connector_ref: str = Field(min_length=1, max_length=128)
    resource_path: str = Field(min_length=1, max_length=500)
    domain: str = "governance"
    description: str = ""
    risk_level: Literal["low", "medium", "high"] = "low"
    http_method: Literal["GET", "POST"] = "GET"
    input_schema: dict[str, object] = {}
    output_schema: dict[str, object] = {}
    parameter_mapping: dict[str, object] = {}
    result_mapping: dict[str, object] = {}
    result_kind: Literal["area_candidates", "table", "metric", "object_profile"] = "table"
    data_schema_ref: str = ""
    timeout_ms: int = 8000
    max_attempts: int = 2
    max_result_rows: int = 1000
    cache_enabled: bool = True
    cache_ttl_seconds: int = 60
    credential_ref: str | None = None
    required_permissions: list[str] = []
    dataset_ids: list[str] = Field(min_length=1)


class ToolUpdateBody(ContractModel):
    name: str | None = None
    description: str | None = None
    resource_path: str | None = None
    http_method: Literal["GET", "POST"] | None = None
    input_schema: dict[str, object] | None = None
    output_schema: dict[str, object] | None = None
    parameter_mapping: dict[str, object] | None = None
    result_mapping: dict[str, object] | None = None
    timeout_ms: int | None = None
    max_attempts: int | None = None
    max_result_rows: int | None = None
    cache_enabled: bool | None = None
    cache_ttl_seconds: int | None = None
    risk_level: Literal["low", "medium", "high"] | None = None
    required_permissions: list[str] | None = None
    dataset_ids: list[str] | None = None


class StatusTransitionBody(ContractModel):
    # Approval is deliberately absent: CAPABILITY_MANAGE cannot manufacture
    # pending_approval. That state must come from the future approval adapter.
    to_status: Literal["draft", "testing"]
    reason: str = ""
    expected_etag: int | None = Field(default=None, ge=1)


class AuditedActionBody(ContractModel):
    reason: str = Field(min_length=1, max_length=2000)

    @field_validator("reason")
    @classmethod
    def reason_must_not_be_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("reason must not be blank")
        return value


class LifecycleActionBody(AuditedActionBody):
    expected_etag: int = Field(ge=1)


class SkillCreateBody(ContractModel):
    capability_id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=200)
    owner: str = Field(min_length=1, max_length=100)
    version: str = Field(min_length=1, max_length=32)
    domain: str = "governance"
    description: str = ""
    guidance: str = ""
    allowed_tool_ids: list[str] = []
    applicable_questions: list[str] = []
    examples: list[dict[str, str]] = []
    counter_examples: list[dict[str, str]] = []


class WorkflowCreateBody(ContractModel):
    capability_id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=200)
    owner: str = Field(min_length=1, max_length=100)
    version: str = Field(min_length=1, max_length=32)
    domain: str = "governance"
    description: str = ""
    nodes: list[WorkflowNodeDefinition] = []
    edges: list[WorkflowEdgeDefinition] = []
    timeout_seconds: int = 300
    requires_human_confirmation: bool = False


class RollbackBody(AuditedActionBody):
    to_version: str = Field(min_length=1, max_length=32)
    expected_etag: int = Field(ge=1)


class ConnectorCreateBody(ContractModel):
    connector_id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=200)
    base_url: str = Field(min_length=1, max_length=500)
    description: str = ""
    allowed_path_prefixes: list[str] = []
    denied_hosts: list[str] = []
    timeout_ms: int = 8000
    credential_ref: str | None = None


class ConnectorUpdateBody(AuditedActionBody):
    expected_etag: int = Field(ge=1)
    name: str | None = Field(default=None, min_length=1, max_length=200)
    base_url: str | None = Field(default=None, min_length=1, max_length=500)
    description: str | None = Field(default=None, max_length=500)
    allowed_path_prefixes: list[str] | None = None
    denied_hosts: list[str] | None = None
    timeout_ms: int | None = Field(default=None, ge=100, le=120_000)
    credential_ref: str | None = None


class ModelConfigCreateBody(ContractModel):
    name: str = Field(min_length=1, max_length=100)
    api_base_url: str = Field(min_length=1, max_length=500)
    api_key: str = Field(min_length=1, max_length=1000)
    model_name: str = Field(min_length=1, max_length=100)
    protocol: str = "openai_compatible"
    timeout_seconds: int = Field(default=60, ge=5, le=600)
    max_output_tokens: int = Field(default=32000, ge=100, le=128000)
    max_retries: int = Field(default=1, ge=0, le=5)
    notes: str = ""


class ModelConfigUpdateBody(ContractModel):
    name: str | None = None
    api_base_url: str | None = None
    api_key: str | None = None
    model_name: str | None = None
    protocol: str | None = None
    timeout_seconds: int | None = Field(default=None, ge=5, le=600)
    max_output_tokens: int | None = Field(default=None, ge=100, le=128000)
    max_retries: int | None = Field(default=None, ge=0, le=5)
    notes: str | None = None


class ModelConfigImportEnvironmentBody(ContractModel):
    name: str = Field(default="当前运行环境模型", min_length=1, max_length=100)
    notes: str = Field(default="由运行环境安全纳管", max_length=2_000)


class ApplicationCreateBody(ContractModel):
    app_id: str = Field(min_length=2, max_length=64)
    name: str = Field(min_length=1, max_length=200)
    default_agent_id: str = Field(min_length=1, max_length=128)
    identity_adapter_id: str = Field(min_length=1, max_length=128)
    status: Literal["disabled"] = "disabled"
    description: str = ""


class ApplicationCapabilityBindingBody(ContractModel):
    capability_id: str = Field(min_length=1, max_length=128)
    capability_version: str = Field(min_length=1, max_length=32)
    reason: str = ""


class ApplicationLifecycleBody(AuditedActionBody):
    expected_etag: int = Field(ge=1)


# ---- Response types ----


class CapabilityResponse(ContractModel):
    data: dict[str, object]
    meta: ResponseMeta


class WorkflowDefinitionResponse(ContractModel):
    data: WorkflowCapability
    meta: ResponseMeta


class WorkflowDryRunData(ContractModel):
    kind: Literal["validation"] = "validation"
    valid: bool
    executable: bool
    workflow: RuntimeWorkflowGraphSnapshot


class WorkflowDryRunResponse(ContractModel):
    data: WorkflowDryRunData
    meta: ResponseMeta


class CapabilityListResponse(ContractModel):
    data: list[dict[str, object]]
    meta: ResponseMeta


class SnapshotResponse(ContractModel):
    data: dict[str, object]
    meta: ResponseMeta


class ConnectorResponse(ContractModel):
    data: dict[str, object]
    meta: ResponseMeta


class ConnectorListResponse(ContractModel):
    data: list[dict[str, object]]
    meta: ResponseMeta


class ModelConfigResponse(ContractModel):
    data: dict[str, object]
    meta: ResponseMeta


class ModelConfigListResponse(ContractModel):
    data: list[dict[str, object]]
    meta: ResponseMeta


class ConnectionTestResponse(ContractModel):
    data: dict[str, object]
    meta: ResponseMeta


class OperationStatusResponse(ContractModel):
    data: dict[str, str]
    meta: ResponseMeta


class LifecycleEventListResponse(ContractModel):
    data: list[dict[str, object]]
    meta: ResponseMeta


def create_capability_router(
    *,
    management_service: CapabilityManagementService,
    application_management_service: ApplicationManagementService,
    model_config_service: ModelConfigService,
    connector_connection_tester: ConnectorConnectionTester,
    reload_runtime_capabilities: Callable[[], Awaitable[int]] | None = None,
    validate_runtime_transition: Callable[
        [str, str, CapabilityStatus], Awaitable[None]
    ]
    | None = None,
    validate_runtime_rollback: Callable[[str, str], Awaitable[None]] | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/capability-api/v1", tags=["capability-center"])

    def _meta() -> ResponseMeta:
        return ResponseMeta(request_id=new_id("req"))

    def _connector_response_data(connector: Connector) -> dict[str, object]:
        data = connector.model_dump(mode="json")
        data["credential_ref"] = "***" if connector.credential_ref else None
        return data

    authorizer = ControlPlaneAuthorizer.compatibility_default()

    def _require(user: CurrentUser, permission: ControlPlanePermission) -> None:
        authorizer.require(user.identity, permission)

    async def _reload_runtime() -> None:
        if reload_runtime_capabilities is not None:
            await reload_runtime_capabilities()

    async def _validate_transition(
        capability_id: str,
        version: str,
        to_status: CapabilityStatus,
    ) -> None:
        if validate_runtime_transition is not None:
            await validate_runtime_transition(capability_id, version, to_status)

    async def _validate_rollback(capability_id: str, to_version: str) -> None:
        if validate_runtime_rollback is not None:
            await validate_runtime_rollback(capability_id, to_version)

    # ---- Applications ----

    @router.get("/applications")
    async def list_applications(
        user: Annotated[CurrentUser, Depends(require_capability_identity)],
        active_only: bool = False,
    ) -> CapabilityListResponse:
        _require(user, ControlPlanePermission.CAPABILITY_READ)
        applications = await application_management_service.list_applications(
            active_only=active_only
        )
        return CapabilityListResponse(
            data=[item.model_dump(mode="json") for item in applications],
            meta=_meta(),
        )

    @router.post("/applications", status_code=201)
    async def register_application(
        body: ApplicationCreateBody,
        user: Annotated[CurrentUser, Depends(require_capability_identity)],
    ) -> CapabilityResponse:
        _require(user, ControlPlanePermission.APPLICATION_MANAGE)
        application = await application_management_service.register_application(
            AgentApplicationDefinition.model_validate(body.model_dump(mode="python")),
            changed_by=user.user_id,
        )
        return CapabilityResponse(
            data=application.model_dump(mode="json"),
            meta=_meta(),
        )

    @router.post("/applications/{app_id}/enable")
    async def enable_application(
        app_id: str,
        body: ApplicationLifecycleBody,
        user: Annotated[CurrentUser, Depends(require_capability_identity)],
    ) -> CapabilityResponse:
        _require(user, ControlPlanePermission.APPLICATION_MANAGE)
        application = await application_management_service.enable_application(
            app_id=app_id,
            expected_etag=body.expected_etag,
            changed_by=user.user_id,
            reason=body.reason,
        )
        return CapabilityResponse(data=application.model_dump(mode="json"), meta=_meta())

    @router.post("/applications/{app_id}/disable")
    async def disable_application(
        app_id: str,
        body: ApplicationLifecycleBody,
        user: Annotated[CurrentUser, Depends(require_capability_identity)],
    ) -> CapabilityResponse:
        _require(user, ControlPlanePermission.APPLICATION_MANAGE)
        application = await application_management_service.disable_application(
            app_id=app_id,
            expected_etag=body.expected_etag,
            changed_by=user.user_id,
            reason=body.reason,
        )
        return CapabilityResponse(data=application.model_dump(mode="json"), meta=_meta())

    @router.get("/applications/{app_id}/capabilities")
    async def list_application_capabilities(
        app_id: str,
        user: Annotated[CurrentUser, Depends(require_capability_identity)],
        enabled_only: bool = False,
    ) -> CapabilityListResponse:
        _require(user, ControlPlanePermission.CAPABILITY_READ)
        bindings = await application_management_service.list_capability_bindings(
            app_id=app_id,
            enabled_only=enabled_only,
        )
        return CapabilityListResponse(
            data=[item.model_dump(mode="json") for item in bindings],
            meta=_meta(),
        )

    @router.post("/applications/{app_id}/capabilities", status_code=201)
    async def bind_application_capability(
        app_id: str,
        body: ApplicationCapabilityBindingBody,
        user: Annotated[CurrentUser, Depends(require_capability_identity)],
    ) -> CapabilityResponse:
        _require(user, ControlPlanePermission.APPLICATION_BIND)
        binding = await application_management_service.bind_capability(
            app_id=app_id,
            capability_id=body.capability_id,
            capability_version=body.capability_version,
            changed_by=user.user_id,
            reason=body.reason,
        )
        return CapabilityResponse(
            data=binding.model_dump(mode="json"),
            meta=_meta(),
        )

    @router.post("/applications/{app_id}/capabilities/{capability_id}/{version}/enable")
    async def enable_application_capability(
        app_id: str,
        capability_id: str,
        version: str,
        body: ApplicationLifecycleBody,
        user: Annotated[CurrentUser, Depends(require_capability_identity)],
    ) -> CapabilityResponse:
        _require(user, ControlPlanePermission.APPLICATION_BIND)
        binding = await application_management_service.enable_capability_binding(
            app_id=app_id,
            capability_id=capability_id,
            capability_version=version,
            expected_etag=body.expected_etag,
            changed_by=user.user_id,
            reason=body.reason,
        )
        return CapabilityResponse(data=binding.model_dump(mode="json"), meta=_meta())

    @router.post("/applications/{app_id}/capabilities/{capability_id}/{version}/disable")
    async def disable_application_capability(
        app_id: str,
        capability_id: str,
        version: str,
        body: ApplicationLifecycleBody,
        user: Annotated[CurrentUser, Depends(require_capability_identity)],
    ) -> CapabilityResponse:
        _require(user, ControlPlanePermission.APPLICATION_BIND)
        binding = await application_management_service.disable_capability_binding(
            app_id=app_id,
            capability_id=capability_id,
            capability_version=version,
            expected_etag=body.expected_etag,
            changed_by=user.user_id,
            reason=body.reason,
        )
        return CapabilityResponse(data=binding.model_dump(mode="json"), meta=_meta())

    # ---- Tools ----

    @router.get("/tools")
    async def list_tools(
        user: Annotated[CurrentUser, Depends(require_capability_identity)],
        status: CapabilityStatus | None = None,
    ) -> CapabilityListResponse:
        _require(user, ControlPlanePermission.CAPABILITY_READ)
        tools = await management_service.list_capabilities(capability_type="tool", status=status)
        return CapabilityListResponse(
            data=[t.model_dump(mode="json") for t in tools],
            meta=_meta(),
        )

    @router.post("/tools", status_code=201)
    async def create_tool(
        body: ToolCreateBody,
        user: Annotated[CurrentUser, Depends(require_capability_identity)],
    ) -> CapabilityResponse:
        _require(user, ControlPlanePermission.CAPABILITY_MANAGE)
        tool = await management_service.create_tool(
            capability_id=body.capability_id,
            name=body.name,
            owner=body.owner,
            version=body.version,
            connector_ref=body.connector_ref,
            resource_path=body.resource_path,
            domain=body.domain,
            description=body.description,
            risk_level=body.risk_level,
            http_method=body.http_method,
            input_schema=body.input_schema,
            output_schema=body.output_schema,
            parameter_mapping=body.parameter_mapping,
            result_mapping=body.result_mapping,
            result_kind=body.result_kind,
            data_schema_ref=body.data_schema_ref,
            timeout_ms=body.timeout_ms,
            max_attempts=body.max_attempts,
            max_result_rows=body.max_result_rows,
            cache_enabled=body.cache_enabled,
            cache_ttl_seconds=body.cache_ttl_seconds,
            credential_ref=body.credential_ref,
            required_permissions=body.required_permissions,
            dataset_ids=body.dataset_ids,
            created_by=user.user_id,
        )
        return CapabilityResponse(data=tool.model_dump(mode="json"), meta=_meta())

    # Static audit sub-resources must be registered before the generic
    # ``/{version}`` route. FastAPI resolves same-method routes in declaration
    # order; otherwise "lifecycle" and "snapshot" are consumed as versions.
    @router.get("/tools/{capability_id}/snapshot")
    async def get_active_snapshot(
        capability_id: str,
        user: Annotated[CurrentUser, Depends(require_capability_identity)],
    ) -> SnapshotResponse:
        _require(user, ControlPlanePermission.CAPABILITY_READ)
        snapshot = await management_service.get_active_snapshot(capability_id)
        if snapshot is None:
            from full_view_agent.application.errors import ResourceNotFound

            raise ResourceNotFound("no active snapshot")
        return SnapshotResponse(data=snapshot.model_dump(mode="json"), meta=_meta())

    @router.get("/tools/{capability_id}/lifecycle")
    async def get_lifecycle_events(
        capability_id: str,
        user: Annotated[CurrentUser, Depends(require_capability_identity)],
    ) -> LifecycleEventListResponse:
        _require(user, ControlPlanePermission.CAPABILITY_READ)
        events = await management_service.list_lifecycle_events(capability_id)
        return LifecycleEventListResponse(
            data=[e.model_dump(mode="json") for e in events],
            meta=_meta(),
        )

    @router.get("/tools/{capability_id}/{version}")
    async def get_tool(
        capability_id: str,
        version: str,
        user: Annotated[CurrentUser, Depends(require_capability_identity)],
    ) -> CapabilityResponse:
        _require(user, ControlPlanePermission.CAPABILITY_READ)
        tool = await management_service.get(capability_id, version)
        if tool is None:
            from full_view_agent.application.errors import ResourceNotFound

            raise ResourceNotFound("tool not found")
        return CapabilityResponse(data=tool.model_dump(mode="json"), meta=_meta())

    @router.patch("/tools/{capability_id}/{version}")
    async def update_tool(
        capability_id: str,
        version: str,
        body: ToolUpdateBody,
        user: Annotated[CurrentUser, Depends(require_capability_identity)],
    ) -> CapabilityResponse:
        _require(user, ControlPlanePermission.CAPABILITY_MANAGE)
        fields = {k: v for k, v in body.model_dump(mode="python").items() if v is not None}
        tool = await management_service.update_tool(
            capability_id=capability_id,
            version=version,
            updated_by=user.user_id,
            **fields,
        )
        return CapabilityResponse(data=tool.model_dump(mode="json"), meta=_meta())

    @router.post("/tools/{capability_id}/{version}/transition")
    async def transition_tool(
        capability_id: str,
        version: str,
        body: StatusTransitionBody,
        user: Annotated[CurrentUser, Depends(require_capability_identity)],
    ) -> CapabilityResponse:
        _require(user, ControlPlanePermission.CAPABILITY_MANAGE)
        result = await management_service.advance_status(
            capability_id=capability_id,
            version=version,
            to_status=body.to_status,
            changed_by=user.user_id,
            reason=body.reason,
            expected_etag=body.expected_etag,
        )
        return CapabilityResponse(data=result.model_dump(mode="json"), meta=_meta())

    @router.post("/tools/{capability_id}/{version}/testing")
    async def mark_tool_testing(
        capability_id: str,
        version: str,
        body: LifecycleActionBody,
        user: Annotated[CurrentUser, Depends(require_capability_identity)],
    ) -> CapabilityResponse:
        _require(user, ControlPlanePermission.CAPABILITY_MANAGE)
        result = await management_service.mark_testing(
            capability_id=capability_id,
            version=version,
            expected_etag=body.expected_etag,
            changed_by=user.user_id,
            reason=body.reason,
        )
        return CapabilityResponse(data=result.model_dump(mode="json"), meta=_meta())

    @router.post("/tools/{capability_id}/{version}/approve")
    async def approve_tool(
        capability_id: str,
        version: str,
        body: LifecycleActionBody,
        user: Annotated[CurrentUser, Depends(require_capability_identity)],
    ) -> CapabilityResponse:
        _require(user, ControlPlanePermission.CAPABILITY_APPROVE)
        result = await management_service.advance_status(
            capability_id=capability_id,
            version=version,
            to_status="pending_approval",
            changed_by=user.user_id,
            reason=body.reason,
            expected_etag=body.expected_etag,
        )
        return CapabilityResponse(data=result.model_dump(mode="json"), meta=_meta())

    @router.post("/tools/{capability_id}/{version}/publish")
    async def publish_tool(
        capability_id: str,
        version: str,
        body: LifecycleActionBody,
        user: Annotated[CurrentUser, Depends(require_capability_identity)],
    ) -> SnapshotResponse:
        _require(user, ControlPlanePermission.CAPABILITY_PUBLISH)
        await _validate_transition(capability_id, version, "published")
        snapshot = await management_service.publish(
            capability_id=capability_id,
            version=version,
            published_by=user.user_id,
            reason=body.reason,
            expected_etag=body.expected_etag,
        )
        await _reload_runtime()
        return SnapshotResponse(data=snapshot.model_dump(mode="json"), meta=_meta())

    @router.post("/tools/{capability_id}/{version}/disable")
    async def disable_tool(
        capability_id: str,
        version: str,
        body: LifecycleActionBody,
        user: Annotated[CurrentUser, Depends(require_capability_identity)],
    ) -> CapabilityResponse:
        _require(user, ControlPlanePermission.CAPABILITY_MANAGE)
        await _validate_transition(capability_id, version, "disabled")
        result = await management_service.disable(
            capability_id=capability_id,
            version=version,
            changed_by=user.user_id,
            reason=body.reason,
            expected_etag=body.expected_etag,
        )
        await _reload_runtime()
        return CapabilityResponse(data=result.model_dump(mode="json"), meta=_meta())

    @router.post("/tools/{capability_id}/rollback")
    async def rollback_tool(
        capability_id: str,
        body: RollbackBody,
        user: Annotated[CurrentUser, Depends(require_capability_identity)],
    ) -> SnapshotResponse:
        _require(user, ControlPlanePermission.CAPABILITY_PUBLISH)
        await _validate_rollback(capability_id, body.to_version)
        snapshot = await management_service.rollback(
            capability_id=capability_id,
            to_version=body.to_version,
            changed_by=user.user_id,
            reason=body.reason,
            expected_etag=body.expected_etag,
        )
        await _reload_runtime()
        return SnapshotResponse(data=snapshot.model_dump(mode="json"), meta=_meta())

    # ---- Skills ----

    @router.get("/skills")
    async def list_skills(
        user: Annotated[CurrentUser, Depends(require_capability_identity)],
        status: CapabilityStatus | None = None,
    ) -> CapabilityListResponse:
        _require(user, ControlPlanePermission.CAPABILITY_READ)
        skills = await management_service.list_capabilities(capability_type="skill", status=status)
        return CapabilityListResponse(
            data=[s.model_dump(mode="json") for s in skills],
            meta=_meta(),
        )

    @router.post("/skills", status_code=201)
    async def create_skill(
        body: SkillCreateBody,
        user: Annotated[CurrentUser, Depends(require_capability_identity)],
    ) -> CapabilityResponse:
        _require(user, ControlPlanePermission.CAPABILITY_MANAGE)
        skill = await management_service.create_skill(
            capability_id=body.capability_id,
            name=body.name,
            owner=body.owner,
            version=body.version,
            domain=body.domain,
            description=body.description,
            guidance=body.guidance,
            allowed_tool_ids=body.allowed_tool_ids,
            applicable_questions=body.applicable_questions,
            examples=body.examples,
            counter_examples=body.counter_examples,
            created_by=user.user_id,
        )
        return CapabilityResponse(data=skill.model_dump(mode="json"), meta=_meta())

    @router.get("/skills/{capability_id}/{version}")
    async def get_skill_definition(
        capability_id: str,
        version: str,
        user: Annotated[CurrentUser, Depends(require_capability_identity)],
    ) -> CapabilityResponse:
        _require(user, ControlPlanePermission.CAPABILITY_READ)
        skill = await management_service.get(capability_id, version)
        if not isinstance(skill, SkillCapability):
            raise ResourceNotFound("skill version not found")
        return CapabilityResponse(data=skill.model_dump(mode="json"), meta=_meta())

    @router.post("/skills/{capability_id}/{version}/dry-run")
    async def validate_skill_definition(
        capability_id: str,
        version: str,
        user: Annotated[CurrentUser, Depends(require_capability_identity)],
    ) -> CapabilityResponse:
        _require(user, ControlPlanePermission.CAPABILITY_MANAGE)
        skill = await management_service.get(capability_id, version)
        if not isinstance(skill, SkillCapability):
            raise ResourceNotFound("skill version not found")
        contract = build_runtime_skill_contract(skill)
        available = set(ToolRegistry.default().list_tool_ids())
        published = await management_service.list_capabilities(
            capability_type="tool", status="published"
        )
        available.update(
            item.capability_id for item in published if isinstance(item, ToolCapability)
        )
        missing = sorted(set(contract.allowed_tool_ids) - available)
        return CapabilityResponse(
            data={
                "kind": "validation",
                "valid": not missing,
                "executable": not missing,
                "missing_tool_ids": missing,
                "skill": contract.model_dump(mode="json"),
            },
            meta=_meta(),
        )

    @router.post("/skills/{capability_id}/{version}/publish")
    async def publish_skill(
        capability_id: str,
        version: str,
        body: LifecycleActionBody,
        user: Annotated[CurrentUser, Depends(require_capability_identity)],
    ) -> SnapshotResponse:
        _require(user, ControlPlanePermission.CAPABILITY_PUBLISH)
        await _validate_transition(capability_id, version, "published")
        snapshot = await management_service.publish(
            capability_id=capability_id,
            version=version,
            published_by=user.user_id,
            reason=body.reason,
            expected_etag=body.expected_etag,
        )
        await _reload_runtime()
        return SnapshotResponse(data=snapshot.model_dump(mode="json"), meta=_meta())

    @router.post("/skills/{capability_id}/{version}/testing")
    async def mark_skill_testing(
        capability_id: str,
        version: str,
        body: LifecycleActionBody,
        user: Annotated[CurrentUser, Depends(require_capability_identity)],
    ) -> CapabilityResponse:
        _require(user, ControlPlanePermission.CAPABILITY_MANAGE)
        result = await management_service.mark_testing(
            capability_id=capability_id,
            version=version,
            expected_etag=body.expected_etag,
            changed_by=user.user_id,
            reason=body.reason,
        )
        return CapabilityResponse(data=result.model_dump(mode="json"), meta=_meta())

    @router.post("/skills/{capability_id}/{version}/approve")
    async def approve_skill(
        capability_id: str,
        version: str,
        body: LifecycleActionBody,
        user: Annotated[CurrentUser, Depends(require_capability_identity)],
    ) -> CapabilityResponse:
        _require(user, ControlPlanePermission.CAPABILITY_APPROVE)
        result = await management_service.advance_status(
            capability_id=capability_id,
            version=version,
            to_status="pending_approval",
            changed_by=user.user_id,
            reason=body.reason,
            expected_etag=body.expected_etag,
        )
        return CapabilityResponse(data=result.model_dump(mode="json"), meta=_meta())

    @router.post("/skills/{capability_id}/{version}/disable")
    async def disable_skill(
        capability_id: str,
        version: str,
        body: LifecycleActionBody,
        user: Annotated[CurrentUser, Depends(require_capability_identity)],
    ) -> CapabilityResponse:
        _require(user, ControlPlanePermission.CAPABILITY_MANAGE)
        await _validate_transition(capability_id, version, "disabled")
        result = await management_service.disable(
            capability_id=capability_id,
            version=version,
            changed_by=user.user_id,
            reason=body.reason,
            expected_etag=body.expected_etag,
        )
        await _reload_runtime()
        return CapabilityResponse(data=result.model_dump(mode="json"), meta=_meta())

    @router.post("/skills/{capability_id}/rollback")
    async def rollback_skill(
        capability_id: str,
        body: RollbackBody,
        user: Annotated[CurrentUser, Depends(require_capability_identity)],
    ) -> SnapshotResponse:
        _require(user, ControlPlanePermission.CAPABILITY_PUBLISH)
        await _validate_rollback(capability_id, body.to_version)
        snapshot = await management_service.rollback(
            capability_id=capability_id,
            to_version=body.to_version,
            changed_by=user.user_id,
            reason=body.reason,
            expected_etag=body.expected_etag,
        )
        await _reload_runtime()
        return SnapshotResponse(data=snapshot.model_dump(mode="json"), meta=_meta())

    # ---- Workflows ----

    @router.get("/workflows")
    async def list_workflows(
        user: Annotated[CurrentUser, Depends(require_capability_identity)],
        status: CapabilityStatus | None = None,
    ) -> CapabilityListResponse:
        _require(user, ControlPlanePermission.CAPABILITY_READ)
        workflows = await management_service.list_capabilities(
            capability_type="workflow", status=status
        )
        return CapabilityListResponse(
            data=[w.model_dump(mode="json") for w in workflows],
            meta=_meta(),
        )

    @router.post("/workflows", status_code=201)
    async def create_workflow(
        body: WorkflowCreateBody,
        user: Annotated[CurrentUser, Depends(require_capability_identity)],
    ) -> WorkflowDefinitionResponse:
        _require(user, ControlPlanePermission.CAPABILITY_MANAGE)
        workflow = await management_service.create_workflow(
            capability_id=body.capability_id,
            name=body.name,
            owner=body.owner,
            version=body.version,
            domain=body.domain,
            description=body.description,
            nodes=body.nodes,
            edges=body.edges,
            timeout_seconds=body.timeout_seconds,
            requires_human_confirmation=body.requires_human_confirmation,
            created_by=user.user_id,
        )
        return WorkflowDefinitionResponse(data=workflow, meta=_meta())

    @router.get("/workflows/{capability_id}/{version}")
    async def get_workflow_definition(
        capability_id: str,
        version: str,
        user: Annotated[CurrentUser, Depends(require_capability_identity)],
    ) -> WorkflowDefinitionResponse:
        _require(user, ControlPlanePermission.CAPABILITY_READ)
        workflow = await management_service.get(capability_id, version)
        if not isinstance(workflow, WorkflowCapability):
            raise ResourceNotFound("workflow version not found")
        return WorkflowDefinitionResponse(data=workflow, meta=_meta())

    @router.post("/workflows/{capability_id}/{version}/dry-run")
    async def validate_workflow_definition(
        capability_id: str,
        version: str,
        user: Annotated[CurrentUser, Depends(require_capability_identity)],
    ) -> WorkflowDryRunResponse:
        _require(user, ControlPlanePermission.CAPABILITY_MANAGE)
        workflow = await management_service.get(capability_id, version)
        if not isinstance(workflow, WorkflowCapability):
            raise ResourceNotFound("workflow version not found")
        base = ToolRegistry.default()
        allowed_tool_refs = {
            (tool_id, base.get_manifest(tool_id).tool_version)
            for tool_id in base.list_tool_ids()
        }
        published = await management_service.list_capabilities(
            capability_type="tool", status="published"
        )
        published_tools = [
            item for item in published if isinstance(item, ToolCapability)
        ]
        manifests, descriptors = build_dynamic_tool_registry_entries(
            published_tools,
            base_registry=base,
        )
        candidate_registry = base.merge_dynamic(
            manifests=manifests,
            descriptors=descriptors,
            dynamic_input_schemas=build_dynamic_input_schemas(published_tools),
        )
        allowed_tool_refs.update(
            (item.capability_id, item.version)
            for item in published_tools
        )
        published_skills = await management_service.list_capabilities(
            capability_type="skill", status="published"
        )
        allowed_skill_refs = {
            (item.capability_id, item.version)
            for item in published_skills
            if isinstance(item, SkillCapability)
        }
        snapshot = build_runtime_workflow_snapshot(
            workflow,
            allowed_tool_refs=allowed_tool_refs,
            allowed_skill_refs=allowed_skill_refs,
        )
        # Constructing the planner performs deterministic graph and node
        # validation without calling an external business service.
        RuntimeWorkflowRegistry((snapshot,)).create_planner(
            workflow_ref=WorkflowRef(
                workflow_id=snapshot.workflow_id,
                workflow_version=snapshot.version,
            ),
            tool_registry=candidate_registry,
            skill_registry=RuntimeSkillRegistry(
                tuple(
                    build_runtime_skill_contract(item)
                    for item in published_skills
                    if isinstance(item, SkillCapability)
                )
            ),
        )
        return WorkflowDryRunResponse(
            data=WorkflowDryRunData(
                valid=True,
                executable=True,
                workflow=snapshot,
            ),
            meta=_meta(),
        )

    @router.post("/workflows/{capability_id}/{version}/publish")
    async def publish_workflow(
        capability_id: str,
        version: str,
        body: LifecycleActionBody,
        user: Annotated[CurrentUser, Depends(require_capability_identity)],
    ) -> SnapshotResponse:
        _require(user, ControlPlanePermission.CAPABILITY_PUBLISH)
        await _validate_transition(capability_id, version, "published")
        snapshot = await management_service.publish(
            capability_id=capability_id,
            version=version,
            published_by=user.user_id,
            reason=body.reason,
            expected_etag=body.expected_etag,
        )
        await _reload_runtime()
        return SnapshotResponse(data=snapshot.model_dump(mode="json"), meta=_meta())

    @router.post("/workflows/{capability_id}/{version}/testing")
    async def mark_workflow_testing(
        capability_id: str,
        version: str,
        body: LifecycleActionBody,
        user: Annotated[CurrentUser, Depends(require_capability_identity)],
    ) -> CapabilityResponse:
        _require(user, ControlPlanePermission.CAPABILITY_MANAGE)
        result = await management_service.mark_testing(
            capability_id=capability_id,
            version=version,
            expected_etag=body.expected_etag,
            changed_by=user.user_id,
            reason=body.reason,
        )
        return CapabilityResponse(data=result.model_dump(mode="json"), meta=_meta())

    @router.post("/workflows/{capability_id}/{version}/approve")
    async def approve_workflow(
        capability_id: str,
        version: str,
        body: LifecycleActionBody,
        user: Annotated[CurrentUser, Depends(require_capability_identity)],
    ) -> CapabilityResponse:
        _require(user, ControlPlanePermission.CAPABILITY_APPROVE)
        result = await management_service.advance_status(
            capability_id=capability_id,
            version=version,
            to_status="pending_approval",
            changed_by=user.user_id,
            reason=body.reason,
            expected_etag=body.expected_etag,
        )
        return CapabilityResponse(data=result.model_dump(mode="json"), meta=_meta())

    @router.post("/workflows/{capability_id}/{version}/disable")
    async def disable_workflow(
        capability_id: str,
        version: str,
        body: LifecycleActionBody,
        user: Annotated[CurrentUser, Depends(require_capability_identity)],
    ) -> CapabilityResponse:
        _require(user, ControlPlanePermission.CAPABILITY_MANAGE)
        await _validate_transition(capability_id, version, "disabled")
        result = await management_service.disable(
            capability_id=capability_id,
            version=version,
            changed_by=user.user_id,
            reason=body.reason,
            expected_etag=body.expected_etag,
        )
        await _reload_runtime()
        return CapabilityResponse(data=result.model_dump(mode="json"), meta=_meta())

    @router.post("/workflows/{capability_id}/rollback")
    async def rollback_workflow(
        capability_id: str,
        body: RollbackBody,
        user: Annotated[CurrentUser, Depends(require_capability_identity)],
    ) -> SnapshotResponse:
        _require(user, ControlPlanePermission.CAPABILITY_PUBLISH)
        await _validate_rollback(capability_id, body.to_version)
        snapshot = await management_service.rollback(
            capability_id=capability_id,
            to_version=body.to_version,
            changed_by=user.user_id,
            reason=body.reason,
            expected_etag=body.expected_etag,
        )
        await _reload_runtime()
        return SnapshotResponse(data=snapshot.model_dump(mode="json"), meta=_meta())

    # ---- Connectors ----

    @router.get("/connectors")
    async def list_connectors(
        user: Annotated[CurrentUser, Depends(require_capability_identity)],
        active_only: bool = True,
    ) -> ConnectorListResponse:
        _require(user, ControlPlanePermission.CAPABILITY_READ)
        connectors = await management_service.list_connectors(active_only=active_only)
        return ConnectorListResponse(
            data=[_connector_response_data(c) for c in connectors],
            meta=_meta(),
        )

    @router.get("/connectors/{connector_id}")
    async def get_connector(
        connector_id: str,
        user: Annotated[CurrentUser, Depends(require_capability_identity)],
    ) -> ConnectorResponse:
        _require(user, ControlPlanePermission.CAPABILITY_READ)
        connector = await management_service.get_connector(connector_id)
        return ConnectorResponse(data=_connector_response_data(connector), meta=_meta())

    @router.post("/connectors", status_code=201)
    async def create_connector(
        body: ConnectorCreateBody,
        user: Annotated[CurrentUser, Depends(require_capability_identity)],
    ) -> ConnectorResponse:
        _require(user, ControlPlanePermission.CAPABILITY_MANAGE)
        connector = await management_service.create_connector(
            connector_id=body.connector_id,
            name=body.name,
            base_url=body.base_url,
            description=body.description,
            allowed_path_prefixes=body.allowed_path_prefixes,
            denied_hosts=body.denied_hosts,
            timeout_ms=body.timeout_ms,
            credential_ref=body.credential_ref,
            created_by=user.user_id,
        )
        return ConnectorResponse(data=_connector_response_data(connector), meta=_meta())

    @router.patch("/connectors/{connector_id}")
    async def update_connector(
        connector_id: str,
        body: ConnectorUpdateBody,
        user: Annotated[CurrentUser, Depends(require_capability_identity)],
    ) -> ConnectorResponse:
        _require(user, ControlPlanePermission.CAPABILITY_MANAGE)
        fields = body.model_dump(
            exclude_unset=True, exclude={"expected_etag", "reason"}
        )
        connector = await management_service.update_connector(
            connector_id=connector_id,
            expected_etag=body.expected_etag,
            actor=user.user_id,
            reason=body.reason,
            **fields,
        )
        return ConnectorResponse(data=_connector_response_data(connector), meta=_meta())

    @router.post("/connectors/{connector_id}/enable")
    async def enable_connector(
        connector_id: str,
        body: LifecycleActionBody,
        user: Annotated[CurrentUser, Depends(require_capability_identity)],
    ) -> ConnectorResponse:
        _require(user, ControlPlanePermission.CAPABILITY_MANAGE)
        connector = await management_service.set_connector_active(
            connector_id=connector_id, is_active=True,
            expected_etag=body.expected_etag, actor=user.user_id,
            reason=body.reason,
        )
        return ConnectorResponse(data=_connector_response_data(connector), meta=_meta())

    @router.post("/connectors/{connector_id}/disable")
    async def disable_connector(
        connector_id: str,
        body: LifecycleActionBody,
        user: Annotated[CurrentUser, Depends(require_capability_identity)],
    ) -> ConnectorResponse:
        _require(user, ControlPlanePermission.CAPABILITY_MANAGE)
        connector = await management_service.set_connector_active(
            connector_id=connector_id, is_active=False,
            expected_etag=body.expected_etag, actor=user.user_id,
            reason=body.reason,
        )
        return ConnectorResponse(data=_connector_response_data(connector), meta=_meta())

    @router.get("/connectors/{connector_id}/audit-events")
    async def list_connector_audit_events(
        connector_id: str,
        user: Annotated[CurrentUser, Depends(require_capability_identity)],
    ) -> ConnectorListResponse:
        _require(user, ControlPlanePermission.CAPABILITY_READ)
        events = await management_service.list_connector_audit_events(connector_id)
        return ConnectorListResponse(
            data=[event.model_dump(mode="json") for event in events], meta=_meta()
        )

    @router.post("/connectors/{connector_id}/test-connection")
    async def test_connector_connection(
        connector_id: str,
        user: Annotated[CurrentUser, Depends(require_capability_identity)],
    ) -> ConnectionTestResponse:
        _require(user, ControlPlanePermission.CAPABILITY_MANAGE)
        result = await connector_connection_tester.test_connection(connector_id)
        return ConnectionTestResponse(data=result.to_dict(), meta=_meta())

    # ---- Model Configs ----

    @router.get("/model-configs")
    async def list_model_configs(
        user: Annotated[CurrentUser, Depends(require_capability_identity)],
    ) -> ModelConfigListResponse:
        _require(user, ControlPlanePermission.MODEL_MANAGE)
        configs = await model_config_service.list_configs()
        return ModelConfigListResponse(
            data=[c.model_dump(mode="json") for c in configs],
            meta=_meta(),
        )

    @router.post("/model-configs", status_code=201)
    async def create_model_config(
        body: ModelConfigCreateBody,
        user: Annotated[CurrentUser, Depends(require_capability_identity)],
    ) -> ModelConfigResponse:
        _require(user, ControlPlanePermission.MODEL_MANAGE)
        config = await model_config_service.create_config(
            name=body.name,
            api_base_url=body.api_base_url,
            api_key=body.api_key,
            model_name=body.model_name,
            protocol=body.protocol,
            timeout_seconds=body.timeout_seconds,
            max_output_tokens=body.max_output_tokens,
            max_retries=body.max_retries,
            notes=body.notes,
            created_by=user.user_id,
        )
        return ModelConfigResponse(data=config.model_dump(mode="json"), meta=_meta())

    @router.get("/model-configs/effective")
    async def get_effective_model_config(
        user: Annotated[CurrentUser, Depends(require_capability_identity)],
    ) -> ModelConfigResponse:
        """Return the model actually selected for new runs, without secrets."""
        _require(user, ControlPlanePermission.MODEL_MANAGE)
        configs = await model_config_service.list_configs()
        enabled = next((config for config in configs if config.is_enabled), None)
        if enabled is not None:
            return ModelConfigResponse(
                data={
                    "source": "database",
                    "config_id": enabled.config_id,
                    "name": enabled.name,
                    "api_base_url": enabled.api_base_url,
                    "model_name": enabled.model_name,
                    "protocol": enabled.protocol,
                    "timeout_seconds": enabled.timeout_seconds,
                    "max_output_tokens": enabled.max_output_tokens,
                    "max_retries": enabled.max_retries,
                },
                meta=_meta(),
            )
        return ModelConfigResponse(
            data={
                "source": "environment",
                "config_id": None,
                "name": "环境变量配置",
                "api_base_url": os.getenv("FULL_VIEW_MODEL_BASE_URL", ""),
                "model_name": os.getenv("FULL_VIEW_MODEL_NAME", ""),
                "protocol": os.getenv(
                    "FULL_VIEW_MODEL_PROVIDER", "deterministic"
                ),
                "timeout_seconds": int(
                    os.getenv("FULL_VIEW_MODEL_TIMEOUT_SECONDS", "60")
                ),
                "max_output_tokens": _environment_model_max_output_tokens(),
                "max_retries": int(
                    os.getenv("FULL_VIEW_MODEL_MAX_RETRIES", "1")
                ),
            },
            meta=_meta(),
        )

    @router.post("/model-configs/import-effective", status_code=201)
    async def import_effective_environment_model(
        body: ModelConfigImportEnvironmentBody,
        user: Annotated[CurrentUser, Depends(require_capability_identity)],
    ) -> ModelConfigResponse:
        """Copy the active environment model into encrypted managed storage."""

        _require(user, ControlPlanePermission.MODEL_MANAGE)
        api_base_url = os.getenv("FULL_VIEW_MODEL_BASE_URL", "").strip()
        model_name = os.getenv("FULL_VIEW_MODEL_NAME", "").strip()
        api_key = os.getenv("FULL_VIEW_MODEL_API_KEY", "")
        provider = os.getenv("FULL_VIEW_MODEL_PROVIDER", "").strip()
        if provider != "openai_compatible" or not all(
            (api_base_url, model_name, api_key)
        ):
            raise RunStateConflict(
                "current environment model is incomplete or is not OpenAI-compatible"
            )
        config = await model_config_service.create_config(
            name=body.name,
            api_base_url=api_base_url,
            api_key=api_key,
            model_name=model_name,
            protocol=provider,
            timeout_seconds=int(os.getenv("FULL_VIEW_MODEL_TIMEOUT_SECONDS", "60")),
            max_output_tokens=_environment_model_max_output_tokens(),
            max_retries=int(os.getenv("FULL_VIEW_MODEL_MAX_RETRIES", "1")),
            notes=body.notes,
            created_by=user.user_id,
        )
        await model_config_service.enable_config(config_id=config.config_id)
        enabled = await model_config_service.get_config(config.config_id)
        return ModelConfigResponse(data=enabled.model_dump(mode="json"), meta=_meta())

    @router.get("/model-configs/{config_id}")
    async def get_model_config(
        config_id: str,
        user: Annotated[CurrentUser, Depends(require_capability_identity)],
    ) -> ModelConfigResponse:
        _require(user, ControlPlanePermission.MODEL_MANAGE)
        config = await model_config_service.get_config(config_id)
        return ModelConfigResponse(data=config.model_dump(mode="json"), meta=_meta())

    @router.patch("/model-configs/{config_id}")
    async def update_model_config(
        config_id: str,
        body: ModelConfigUpdateBody,
        user: Annotated[CurrentUser, Depends(require_capability_identity)],
    ) -> ModelConfigResponse:
        _require(user, ControlPlanePermission.MODEL_MANAGE)
        config = await model_config_service.update_config(
            config_id=config_id,
            updated_by=user.user_id,
            name=body.name,
            api_base_url=body.api_base_url,
            api_key=body.api_key,
            model_name=body.model_name,
            protocol=body.protocol,
            timeout_seconds=body.timeout_seconds,
            max_output_tokens=body.max_output_tokens,
            max_retries=body.max_retries,
            notes=body.notes,
        )
        return ModelConfigResponse(data=config.model_dump(mode="json"), meta=_meta())

    @router.post("/model-configs/{config_id}/enable")
    async def enable_model_config(
        config_id: str,
        user: Annotated[CurrentUser, Depends(require_capability_identity)],
    ) -> OperationStatusResponse:
        _require(user, ControlPlanePermission.MODEL_MANAGE)
        await model_config_service.enable_config(config_id=config_id)
        return OperationStatusResponse(data={"status": "ok"}, meta=_meta())

    @router.post("/model-configs/{config_id}/disable")
    async def disable_model_config(
        config_id: str,
        user: Annotated[CurrentUser, Depends(require_capability_identity)],
    ) -> OperationStatusResponse:
        _require(user, ControlPlanePermission.MODEL_MANAGE)
        await model_config_service.disable_config(config_id=config_id)
        return OperationStatusResponse(data={"status": "ok"}, meta=_meta())

    @router.delete("/model-configs/{config_id}")
    async def delete_model_config(
        config_id: str,
        user: Annotated[CurrentUser, Depends(require_capability_identity)],
    ) -> OperationStatusResponse:
        _require(user, ControlPlanePermission.MODEL_MANAGE)
        await model_config_service.delete_config(config_id=config_id)
        return OperationStatusResponse(data={"status": "ok"}, meta=_meta())

    @router.post("/model-configs/{config_id}/test-connection")
    async def test_model_connection(
        config_id: str,
        user: Annotated[CurrentUser, Depends(require_capability_identity)],
    ) -> ConnectionTestResponse:
        _require(user, ControlPlanePermission.MODEL_MANAGE)
        result = await model_config_service.test_connection(config_id=config_id)
        return ConnectionTestResponse(data=result.model_dump(mode="json"), meta=_meta())

    # ---- Runtime discovery ----

    @router.get("/runtime/tools")
    async def get_runtime_tools(
        user: Annotated[CurrentUser, Depends(require_capability_identity)],
    ) -> CapabilityListResponse:
        _require(user, ControlPlanePermission.CAPABILITY_READ)
        tools = await management_service.get_runtime_tools()
        return CapabilityListResponse(
            data=[t.model_dump(mode="json") for t in tools],
            meta=_meta(),
        )

    return router
