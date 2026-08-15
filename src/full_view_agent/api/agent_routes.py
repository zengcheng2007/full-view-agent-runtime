"""Application-scoped Agent assembly control-plane API."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import Field

from full_view_agent.api._api_deps import (
    CurrentUser,
    ResponseMeta,
    require_capability_identity,
)
from full_view_agent.application.agent_management_service import AgentManagementService
from full_view_agent.application.control_plane_authorization import (
    ControlPlaneAuthorizer,
    ControlPlanePermission,
)
from full_view_agent.application.session_run_service import new_id
from full_view_agent.domain.agent_definition import (
    AgentDefinition,
    AgentExecutionPolicy,
    AgentModelPolicy,
    AgentReleaseSnapshot,
    AgentValidationReport,
    AgentVersion,
    RunAgentReleaseSnapshot,
)
from full_view_agent.domain.application import AgentApplicationDefinition
from full_view_agent.domain.contract_model import ContractModel


class AgentCreateBody(ContractModel):
    agent_id: str
    name: str
    description: str = ""


class DefaultAgentBody(ContractModel):
    agent_id: str
    expected_etag: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=2000)


class AgentVersionCreateBody(ContractModel):
    version: str
    prompt_ref: str | None = None
    capability_refs: tuple[str, ...] = ()
    skill_refs: tuple[str, ...] = ()
    workflow_refs: tuple[str, ...] = ()
    knowledge_base_refs: tuple[str, ...] = ()
    execution_policy: AgentExecutionPolicy = Field(
        default_factory=AgentExecutionPolicy
    )


class AgentModelPolicyBody(ContractModel):
    primary_model_config_id: str
    fallback_model_config_ids: tuple[str, ...] = ()


class PublishBody(ContractModel):
    reason: str = Field(min_length=1, max_length=2000)


class DataResponse(ContractModel):
    data: dict[str, object]
    meta: ResponseMeta


class ListResponse(ContractModel):
    data: list[dict[str, object]]
    meta: ResponseMeta


class AgentDefinitionResponse(ContractModel):
    data: AgentDefinition
    meta: ResponseMeta


class AgentDefinitionListResponse(ContractModel):
    data: list[AgentDefinition]
    meta: ResponseMeta


class AgentApplicationResponse(ContractModel):
    data: AgentApplicationDefinition
    meta: ResponseMeta


class AgentVersionResponse(ContractModel):
    data: AgentVersion
    meta: ResponseMeta


class AgentVersionListResponse(ContractModel):
    data: list[AgentVersion]
    meta: ResponseMeta


class AgentModelPolicyResponse(ContractModel):
    data: AgentModelPolicy
    meta: ResponseMeta


class AgentValidationResponse(ContractModel):
    data: AgentValidationReport
    meta: ResponseMeta


class AgentReleaseResponse(ContractModel):
    data: AgentReleaseSnapshot
    meta: ResponseMeta


class RunAgentReleaseResponse(ContractModel):
    data: RunAgentReleaseSnapshot
    meta: ResponseMeta


def create_agent_router(service: AgentManagementService) -> APIRouter:
    router = APIRouter(prefix="/capability-api/v1", tags=["agent-management"])
    authorizer = ControlPlaneAuthorizer.compatibility_default()

    def meta() -> ResponseMeta:
        return ResponseMeta(request_id=new_id("req"))

    def require_manage(user: CurrentUser) -> None:
        authorizer.require(user.identity, ControlPlanePermission.APPLICATION_MANAGE)

    @router.get("/applications/{app_id}/agents")
    async def list_agents(
        app_id: str,
        user: Annotated[CurrentUser, Depends(require_capability_identity)],
    ) -> AgentDefinitionListResponse:
        authorizer.require(user.identity, ControlPlanePermission.CAPABILITY_READ)
        items = await service.list_agents(app_id)
        return AgentDefinitionListResponse(data=items, meta=meta())

    @router.post("/applications/{app_id}/agents", status_code=201)
    async def create_agent(
        app_id: str,
        body: AgentCreateBody,
        user: Annotated[CurrentUser, Depends(require_capability_identity)],
    ) -> AgentDefinitionResponse:
        require_manage(user)
        item = await service.create_agent(AgentDefinition(app_id=app_id, **body.model_dump()))
        return AgentDefinitionResponse(data=item, meta=meta())

    @router.get("/applications/{app_id}/agents/{agent_id}")
    async def get_agent(
        app_id: str,
        agent_id: str,
        user: Annotated[CurrentUser, Depends(require_capability_identity)],
    ) -> AgentDefinitionResponse:
        authorizer.require(user.identity, ControlPlanePermission.CAPABILITY_READ)
        item = await service.get_agent(app_id, agent_id)
        return AgentDefinitionResponse(data=item, meta=meta())

    @router.put("/applications/{app_id}/default-agent")
    async def set_default_agent(
        app_id: str,
        body: DefaultAgentBody,
        user: Annotated[CurrentUser, Depends(require_capability_identity)],
    ) -> AgentApplicationResponse:
        require_manage(user)
        item = await service.set_default_agent(
            app_id=app_id,
            agent_id=body.agent_id,
            expected_application_etag=body.expected_etag,
            changed_by=user.user_id,
            reason=body.reason,
        )
        return AgentApplicationResponse(data=item, meta=meta())

    @router.get("/applications/{app_id}/agents/{agent_id}/versions")
    async def list_versions(
        app_id: str,
        agent_id: str,
        user: Annotated[CurrentUser, Depends(require_capability_identity)],
    ) -> AgentVersionListResponse:
        authorizer.require(user.identity, ControlPlanePermission.CAPABILITY_READ)
        items = await service.list_versions(app_id, agent_id)
        return AgentVersionListResponse(data=items, meta=meta())

    @router.post("/applications/{app_id}/agents/{agent_id}/versions", status_code=201)
    async def create_version(
        app_id: str,
        agent_id: str,
        body: AgentVersionCreateBody,
        user: Annotated[CurrentUser, Depends(require_capability_identity)],
    ) -> AgentVersionResponse:
        require_manage(user)
        item = await service.create_version(
            AgentVersion(app_id=app_id, agent_id=agent_id, **body.model_dump())
        )
        return AgentVersionResponse(data=item, meta=meta())

    @router.get("/applications/{app_id}/agents/{agent_id}/versions/{version}/model-policy")
    async def get_model_policy(
        app_id: str,
        agent_id: str,
        version: str,
        user: Annotated[CurrentUser, Depends(require_capability_identity)],
    ) -> AgentModelPolicyResponse:
        authorizer.require(user.identity, ControlPlanePermission.CAPABILITY_READ)
        item = await service.get_model_policy(app_id, agent_id, version)
        return AgentModelPolicyResponse(data=item, meta=meta())

    @router.put("/applications/{app_id}/agents/{agent_id}/versions/{version}/model-policy")
    async def set_model_policy(
        app_id: str,
        agent_id: str,
        version: str,
        body: AgentModelPolicyBody,
        user: Annotated[CurrentUser, Depends(require_capability_identity)],
    ) -> AgentModelPolicyResponse:
        require_manage(user)
        item = await service.set_model_policy(
            app_id=app_id,
            agent_id=agent_id,
            version=version,
            policy=AgentModelPolicy.model_validate(body.model_dump()),
        )
        return AgentModelPolicyResponse(data=item, meta=meta())

    @router.post("/applications/{app_id}/agents/{agent_id}/versions/{version}/validate")
    async def validate_version(
        app_id: str,
        agent_id: str,
        version: str,
        user: Annotated[CurrentUser, Depends(require_capability_identity)],
    ) -> AgentValidationResponse:
        require_manage(user)
        item = await service.validate_version(app_id=app_id, agent_id=agent_id, version=version)
        return AgentValidationResponse(data=item, meta=meta())

    @router.post("/applications/{app_id}/agents/{agent_id}/versions/{version}/publish")
    async def publish_version(
        app_id: str,
        agent_id: str,
        version: str,
        body: PublishBody,
        user: Annotated[CurrentUser, Depends(require_capability_identity)],
    ) -> AgentReleaseResponse:
        authorizer.require(user.identity, ControlPlanePermission.CAPABILITY_PUBLISH)
        item = await service.publish_version(
            app_id=app_id,
            agent_id=agent_id,
            version=version,
            published_by=user.user_id,
            reason=body.reason,
        )
        return AgentReleaseResponse(data=item, meta=meta())

    @router.get("/applications/{app_id}/agents/{agent_id}/releases/active")
    async def effective_release(
        app_id: str,
        agent_id: str,
        user: Annotated[CurrentUser, Depends(require_capability_identity)],
    ) -> AgentReleaseResponse:
        authorizer.require(user.identity, ControlPlanePermission.CAPABILITY_READ)
        item = await service.get_active_release(app_id, agent_id)
        return AgentReleaseResponse(data=item, meta=meta())

    @router.get(
        "/applications/{app_id}/runs/{run_id}/agent-release-snapshot"
    )
    async def application_run_release_snapshot(
        app_id: str,
        run_id: str,
        user: Annotated[CurrentUser, Depends(require_capability_identity)],
    ) -> RunAgentReleaseResponse:
        authorizer.require(user.identity, ControlPlanePermission.CAPABILITY_READ)
        item = await service.get_run_snapshot(run_id)
        if (
            item.tenant_id != user.identity.principal.tenant_id
            or item.app_id != app_id
        ):
            raise HTTPException(
                status_code=403,
                detail="run snapshot is outside application scope",
            )
        return RunAgentReleaseResponse(data=item, meta=meta())

    return router
