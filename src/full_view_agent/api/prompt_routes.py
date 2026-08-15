from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, status
from pydantic import Field, field_validator

from full_view_agent.api._api_deps import (
    CurrentUser,
    ResponseMeta,
    require_capability_identity,
)
from full_view_agent.application.control_plane_authorization import (
    ControlPlaneAuthorizer,
    ControlPlanePermission,
)
from full_view_agent.application.prompt_template_service import PromptTemplateService
from full_view_agent.application.session_run_service import new_id
from full_view_agent.domain.contract_model import ContractModel
from full_view_agent.domain.prompt_template import (
    PromptLayer,
    PromptLifecycleEvent,
    PromptTemplate,
    PromptTemplateStatus,
)


class PromptCreateBody(ContractModel):
    prompt_id: str = Field(min_length=3, max_length=128)
    app_id: str = Field(min_length=2, max_length=64)
    layer: PromptLayer = "application"
    name: str = Field(min_length=1, max_length=200)
    version: str = Field(min_length=5, max_length=32)
    content: str = Field(min_length=1, max_length=20_000)
    reason: str = Field(min_length=1, max_length=2_000)

    @field_validator("content", "reason")
    @classmethod
    def must_not_be_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value


class PromptLifecycleBody(ContractModel):
    expected_etag: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=2_000)

    @field_validator("reason")
    @classmethod
    def reason_must_not_be_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("reason must not be blank")
        return value


class PromptEnvelope(ContractModel):
    data: PromptTemplate | None
    meta: ResponseMeta


class PromptListEnvelope(ContractModel):
    data: list[PromptTemplate]
    meta: ResponseMeta


class PromptAuditListEnvelope(ContractModel):
    data: list[PromptLifecycleEvent]
    meta: ResponseMeta


def create_prompt_router(
    service: PromptTemplateService,
    *,
    refresh_runtime: Callable[[str], Awaitable[None]] | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/capability-api/v1", tags=["prompt-management"])
    authorizer = ControlPlaneAuthorizer.compatibility_default()

    def meta() -> ResponseMeta:
        return ResponseMeta(request_id=new_id("req"))

    def require(user: CurrentUser, permission: ControlPlanePermission) -> None:
        authorizer.require(user.identity, permission)

    def actor(user: CurrentUser) -> str:
        return user.identity.principal.user_id

    @router.get("/prompt-templates")
    async def list_prompts(
        user: Annotated[CurrentUser, Depends(require_capability_identity)],
        app_id: str | None = None,
        layer: PromptLayer | None = None,
    ) -> PromptListEnvelope:
        require(user, ControlPlanePermission.CAPABILITY_READ)
        items = await service.list(app_id=app_id, layer=layer)
        return PromptListEnvelope(data=items, meta=meta())

    @router.get("/prompt-templates/effective")
    async def effective_prompt(
        user: Annotated[CurrentUser, Depends(require_capability_identity)],
        app_id: str = "full_information_view",
    ) -> PromptEnvelope:
        require(user, ControlPlanePermission.CAPABILITY_READ)
        effective = await service.get_effective_template(app_id=app_id)
        return PromptEnvelope(
            data=effective, meta=meta()
        )

    @router.get("/prompt-templates/{prompt_id}/audit-events")
    async def prompt_audit_events(
        prompt_id: str,
        user: Annotated[CurrentUser, Depends(require_capability_identity)],
    ) -> PromptAuditListEnvelope:
        require(user, ControlPlanePermission.CAPABILITY_READ)
        events = await service.list_events(prompt_id=prompt_id)
        return PromptAuditListEnvelope(data=events, meta=meta())

    @router.post(
        "/prompt-templates", status_code=status.HTTP_201_CREATED
    )
    async def create_prompt(
        body: PromptCreateBody,
        user: Annotated[CurrentUser, Depends(require_capability_identity)],
    ) -> PromptEnvelope:
        require(user, ControlPlanePermission.CAPABILITY_MANAGE)
        item = await service.create(
            **body.model_dump(exclude={"reason"}),
            actor=actor(user),
            reason=body.reason,
        )
        return PromptEnvelope(data=item, meta=meta())

    async def transition(
        *,
        prompt_id: str,
        version: str,
        target: PromptTemplateStatus,
        body: PromptLifecycleBody,
        user: CurrentUser,
        permission: ControlPlanePermission,
    ) -> PromptEnvelope:
        require(user, permission)
        item = await service.transition(
            prompt_id=prompt_id,
            version=version,
            to_status=target,
            expected_etag=body.expected_etag,
            actor=actor(user),
            reason=body.reason,
        )
        if target in {"published", "disabled"} and refresh_runtime is not None:
            await refresh_runtime(item.app_id)
        return PromptEnvelope(data=item, meta=meta())

    @router.post("/prompt-templates/{prompt_id}/{version}/{action}")
    async def transition_prompt(
        prompt_id: str,
        version: str,
        action: Literal["testing", "approve", "publish", "disable"],
        body: PromptLifecycleBody,
        user: Annotated[CurrentUser, Depends(require_capability_identity)],
    ) -> PromptEnvelope:
        targets: dict[str, PromptTemplateStatus] = {
            "testing": "testing",
            "approve": "pending_approval",
            "publish": "published",
            "disable": "disabled",
        }
        target = targets[action]
        permission = (
            ControlPlanePermission.CAPABILITY_APPROVE
            if action == "approve"
            else ControlPlanePermission.CAPABILITY_PUBLISH
            if action in {"publish", "disable"}
            else ControlPlanePermission.CAPABILITY_MANAGE
        )
        return await transition(
            prompt_id=prompt_id,
            version=version,
            target=target,
            body=body,
            user=user,
            permission=permission,
        )

    return router
