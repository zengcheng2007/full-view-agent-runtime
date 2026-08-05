"""P2 capability center API routes.

Mounted under ``/capability-api/v1/`` alongside the existing ``/agent-api/v1/``.
Requires geoToken authentication.  Admin-only routes check for admin role.
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends
from pydantic import Field

from full_view_agent.api._api_deps import (
    CurrentUser,
    ResponseMeta,
    require_geotoken,
)
from full_view_agent.application.capability_management_service import (
    CapabilityManagementService,
)
from full_view_agent.application.model_config_service import ModelConfigService
from full_view_agent.application.session_run_service import new_id
from full_view_agent.domain.capability import (
    CapabilityStatus,
)
from full_view_agent.domain.contract_model import ContractModel

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
    result_kind: Literal["area_candidates", "table", "metric", "object_profile"] = (
        "table"
    )
    data_schema_ref: str = ""
    timeout_ms: int = 8000
    max_attempts: int = 2
    max_result_rows: int = 1000
    cache_enabled: bool = True
    cache_ttl_seconds: int = 60
    credential_ref: str | None = None
    required_permissions: list[str] = []
    dataset_ids: list[str] = []


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
    to_status: CapabilityStatus
    reason: str = ""


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
    nodes: list[dict[str, object]] = []
    edges: list[dict[str, object]] = []
    timeout_seconds: int = 300
    requires_human_confirmation: bool = False


class RollbackBody(ContractModel):
    to_version: str = Field(min_length=1, max_length=32)
    reason: str = ""


class ConnectorCreateBody(ContractModel):
    connector_id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=200)
    base_url: str = Field(min_length=1, max_length=500)
    description: str = ""
    allowed_path_prefixes: list[str] = []
    denied_hosts: list[str] = []
    timeout_ms: int = 8000
    credential_ref: str | None = None


class ModelConfigCreateBody(ContractModel):
    name: str = Field(min_length=1, max_length=100)
    api_base_url: str = Field(min_length=1, max_length=500)
    api_key: str = Field(min_length=1, max_length=1000)
    model_name: str = Field(min_length=1, max_length=100)
    protocol: str = "openai_compatible"
    timeout_seconds: int = 60
    max_output_tokens: int = 32000
    max_retries: int = 1
    notes: str = ""


class ModelConfigUpdateBody(ContractModel):
    name: str | None = None
    api_base_url: str | None = None
    api_key: str | None = None
    model_name: str | None = None
    protocol: str | None = None
    timeout_seconds: int | None = None
    max_output_tokens: int | None = None
    max_retries: int | None = None
    notes: str | None = None


# ---- Response types ----


class CapabilityResponse(ContractModel):
    data: dict[str, object]
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


class LifecycleEventListResponse(ContractModel):
    data: list[dict[str, object]]
    meta: ResponseMeta


def create_capability_router(
    *,
    management_service: CapabilityManagementService,
    model_config_service: ModelConfigService,
) -> APIRouter:
    router = APIRouter(prefix="/capability-api/v1", tags=["capability-center"])

    def _meta() -> ResponseMeta:
        return ResponseMeta(request_id=new_id("req"))

    def _is_admin(user: CurrentUser) -> bool:
        return any(
            role in {"admin", "super_admin", "role_1", "1"}
            for role in user.identity.principal.roles
        )

    def _require_admin(user: CurrentUser) -> None:
        if not _is_admin(user):
            from full_view_agent.application.errors import AuthenticationFailed

            raise AuthenticationFailed("admin role required for capability management")

    # ---- Tools ----

    @router.get("/tools")
    async def list_tools(
        user: Annotated[CurrentUser, Depends(require_geotoken)],
        status: CapabilityStatus | None = None,
    ) -> CapabilityListResponse:
        tools = await management_service.list_capabilities(
            capability_type="tool", status=status
        )
        return CapabilityListResponse(
            data=[t.model_dump(mode="json") for t in tools],
            meta=_meta(),
        )

    @router.post("/tools", status_code=201)
    async def create_tool(
        body: ToolCreateBody,
        user: Annotated[CurrentUser, Depends(require_geotoken)],
    ) -> CapabilityResponse:
        _require_admin(user)
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
        return CapabilityResponse(
            data=tool.model_dump(mode="json"), meta=_meta()
        )

    @router.get("/tools/{capability_id}/{version}")
    async def get_tool(
        capability_id: str,
        version: str,
        user: Annotated[CurrentUser, Depends(require_geotoken)],
    ) -> CapabilityResponse:
        tool = await management_service.get(capability_id, version)
        if tool is None:
            from full_view_agent.application.errors import ResourceNotFound

            raise ResourceNotFound("tool not found")
        return CapabilityResponse(
            data=tool.model_dump(mode="json"), meta=_meta()
        )

    @router.patch("/tools/{capability_id}/{version}")
    async def update_tool(
        capability_id: str,
        version: str,
        body: ToolUpdateBody,
        user: Annotated[CurrentUser, Depends(require_geotoken)],
    ) -> CapabilityResponse:
        _require_admin(user)
        fields = {
            k: v
            for k, v in body.model_dump(mode="python").items()
            if v is not None
        }
        tool = await management_service.update_tool(
            capability_id=capability_id,
            version=version,
            updated_by=user.user_id,
            **fields,
        )
        return CapabilityResponse(
            data=tool.model_dump(mode="json"), meta=_meta()
        )

    @router.post("/tools/{capability_id}/{version}/transition")
    async def transition_tool(
        capability_id: str,
        version: str,
        body: StatusTransitionBody,
        user: Annotated[CurrentUser, Depends(require_geotoken)],
    ) -> CapabilityResponse:
        _require_admin(user)
        result = await management_service.advance_status(
            capability_id=capability_id,
            version=version,
            to_status=body.to_status,
            changed_by=user.user_id,
            reason=body.reason,
        )
        return CapabilityResponse(
            data=result.model_dump(mode="json"), meta=_meta()
        )

    @router.post("/tools/{capability_id}/{version}/publish")
    async def publish_tool(
        capability_id: str,
        version: str,
        user: Annotated[CurrentUser, Depends(require_geotoken)],
    ) -> SnapshotResponse:
        _require_admin(user)
        snapshot = await management_service.publish(
            capability_id=capability_id,
            version=version,
            published_by=user.user_id,
        )
        return SnapshotResponse(
            data=snapshot.model_dump(mode="json"), meta=_meta()
        )

    @router.post("/tools/{capability_id}/{version}/disable")
    async def disable_tool(
        capability_id: str,
        version: str,
        user: Annotated[CurrentUser, Depends(require_geotoken)],
    ) -> CapabilityResponse:
        _require_admin(user)
        result = await management_service.disable(
            capability_id=capability_id,
            version=version,
            changed_by=user.user_id,
        )
        return CapabilityResponse(
            data=result.model_dump(mode="json"), meta=_meta()
        )

    @router.post("/tools/{capability_id}/rollback")
    async def rollback_tool(
        capability_id: str,
        body: RollbackBody,
        user: Annotated[CurrentUser, Depends(require_geotoken)],
    ) -> SnapshotResponse:
        _require_admin(user)
        snapshot = await management_service.rollback(
            capability_id=capability_id,
            to_version=body.to_version,
            changed_by=user.user_id,
            reason=body.reason,
        )
        return SnapshotResponse(
            data=snapshot.model_dump(mode="json"), meta=_meta()
        )

    @router.get("/tools/{capability_id}/snapshot")
    async def get_active_snapshot(
        capability_id: str,
        user: Annotated[CurrentUser, Depends(require_geotoken)],
    ) -> SnapshotResponse:
        snapshot = await management_service.get_active_snapshot(capability_id)
        if snapshot is None:
            from full_view_agent.application.errors import ResourceNotFound

            raise ResourceNotFound("no active snapshot")
        return SnapshotResponse(
            data=snapshot.model_dump(mode="json"), meta=_meta()
        )

    @router.get("/tools/{capability_id}/lifecycle")
    async def get_lifecycle_events(
        capability_id: str,
        user: Annotated[CurrentUser, Depends(require_geotoken)],
    ) -> LifecycleEventListResponse:
        events = await management_service.list_lifecycle_events(capability_id)
        return LifecycleEventListResponse(
            data=[e.model_dump(mode="json") for e in events],
            meta=_meta(),
        )

    # ---- Skills ----

    @router.get("/skills")
    async def list_skills(
        user: Annotated[CurrentUser, Depends(require_geotoken)],
        status: CapabilityStatus | None = None,
    ) -> CapabilityListResponse:
        skills = await management_service.list_capabilities(
            capability_type="skill", status=status
        )
        return CapabilityListResponse(
            data=[s.model_dump(mode="json") for s in skills],
            meta=_meta(),
        )

    @router.post("/skills", status_code=201)
    async def create_skill(
        body: SkillCreateBody,
        user: Annotated[CurrentUser, Depends(require_geotoken)],
    ) -> CapabilityResponse:
        _require_admin(user)
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
        return CapabilityResponse(
            data=skill.model_dump(mode="json"), meta=_meta()
        )

    @router.post("/skills/{capability_id}/{version}/publish")
    async def publish_skill(
        capability_id: str,
        version: str,
        user: Annotated[CurrentUser, Depends(require_geotoken)],
    ) -> SnapshotResponse:
        _require_admin(user)
        snapshot = await management_service.publish(
            capability_id=capability_id,
            version=version,
            published_by=user.user_id,
        )
        return SnapshotResponse(
            data=snapshot.model_dump(mode="json"), meta=_meta()
        )

    # ---- Workflows ----

    @router.get("/workflows")
    async def list_workflows(
        user: Annotated[CurrentUser, Depends(require_geotoken)],
        status: CapabilityStatus | None = None,
    ) -> CapabilityListResponse:
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
        user: Annotated[CurrentUser, Depends(require_geotoken)],
    ) -> CapabilityResponse:
        _require_admin(user)
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
        return CapabilityResponse(
            data=workflow.model_dump(mode="json"), meta=_meta()
        )

    @router.post("/workflows/{capability_id}/{version}/publish")
    async def publish_workflow(
        capability_id: str,
        version: str,
        user: Annotated[CurrentUser, Depends(require_geotoken)],
    ) -> SnapshotResponse:
        _require_admin(user)
        snapshot = await management_service.publish(
            capability_id=capability_id,
            version=version,
            published_by=user.user_id,
        )
        return SnapshotResponse(
            data=snapshot.model_dump(mode="json"), meta=_meta()
        )

    # ---- Connectors ----

    @router.get("/connectors")
    async def list_connectors(
        user: Annotated[CurrentUser, Depends(require_geotoken)],
        active_only: bool = True,
    ) -> ConnectorListResponse:
        connectors = await management_service.list_connectors(
            active_only=active_only
        )
        return ConnectorListResponse(
            data=[c.model_dump(mode="json") for c in connectors],
            meta=_meta(),
        )

    @router.post("/connectors", status_code=201)
    async def create_connector(
        body: ConnectorCreateBody,
        user: Annotated[CurrentUser, Depends(require_geotoken)],
    ) -> ConnectorResponse:
        _require_admin(user)
        connector = await management_service.create_connector(
            connector_id=body.connector_id,
            name=body.name,
            base_url=body.base_url,
            description=body.description,
            allowed_path_prefixes=body.allowed_path_prefixes,
            denied_hosts=body.denied_hosts,
            timeout_ms=body.timeout_ms,
            credential_ref=body.credential_ref,
        )
        return ConnectorResponse(
            data=connector.model_dump(mode="json"), meta=_meta()
        )

    # ---- Model Configs ----

    @router.get("/model-configs")
    async def list_model_configs(
        user: Annotated[CurrentUser, Depends(require_geotoken)],
    ) -> ModelConfigListResponse:
        _require_admin(user)
        configs = await model_config_service.list_configs()
        return ModelConfigListResponse(
            data=[c.model_dump(mode="json") for c in configs],
            meta=_meta(),
        )

    @router.post("/model-configs", status_code=201)
    async def create_model_config(
        body: ModelConfigCreateBody,
        user: Annotated[CurrentUser, Depends(require_geotoken)],
    ) -> ModelConfigResponse:
        _require_admin(user)
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
        return ModelConfigResponse(
            data=config.model_dump(mode="json"), meta=_meta()
        )

    @router.get("/model-configs/{config_id}")
    async def get_model_config(
        config_id: str,
        user: Annotated[CurrentUser, Depends(require_geotoken)],
    ) -> ModelConfigResponse:
        _require_admin(user)
        config = await model_config_service.get_config(config_id)
        return ModelConfigResponse(
            data=config.model_dump(mode="json"), meta=_meta()
        )

    @router.patch("/model-configs/{config_id}")
    async def update_model_config(
        config_id: str,
        body: ModelConfigUpdateBody,
        user: Annotated[CurrentUser, Depends(require_geotoken)],
    ) -> ModelConfigResponse:
        _require_admin(user)
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
        return ModelConfigResponse(
            data=config.model_dump(mode="json"), meta=_meta()
        )

    @router.post("/model-configs/{config_id}/enable")
    async def enable_model_config(
        config_id: str,
        user: Annotated[CurrentUser, Depends(require_geotoken)],
    ) -> dict[str, str]:
        _require_admin(user)
        await model_config_service.enable_config(config_id=config_id)
        return {"status": "ok"}

    @router.post("/model-configs/{config_id}/disable")
    async def disable_model_config(
        config_id: str,
        user: Annotated[CurrentUser, Depends(require_geotoken)],
    ) -> dict[str, str]:
        _require_admin(user)
        await model_config_service.disable_config(config_id=config_id)
        return {"status": "ok"}

    @router.delete("/model-configs/{config_id}")
    async def delete_model_config(
        config_id: str,
        user: Annotated[CurrentUser, Depends(require_geotoken)],
    ) -> dict[str, str]:
        _require_admin(user)
        await model_config_service.delete_config(config_id=config_id)
        return {"status": "ok"}

    @router.post("/model-configs/{config_id}/test-connection")
    async def test_model_connection(
        config_id: str,
        user: Annotated[CurrentUser, Depends(require_geotoken)],
    ) -> ConnectionTestResponse:
        _require_admin(user)
        result = await model_config_service.test_connection(config_id=config_id)
        return ConnectionTestResponse(
            data=result.model_dump(mode="json"), meta=_meta()
        )

    # ---- Runtime discovery ----

    @router.get("/runtime/tools")
    async def get_runtime_tools(
        user: Annotated[CurrentUser, Depends(require_geotoken)],
    ) -> CapabilityListResponse:
        tools = await management_service.get_runtime_tools()
        return CapabilityListResponse(
            data=[t.model_dump(mode="json") for t in tools],
            meta=_meta(),
        )

    return router
