from __future__ import annotations

import base64
import binascii
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Response, status
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
from full_view_agent.application.errors import ResourceNotFound
from full_view_agent.application.knowledge_service import (
    KnowledgeResourceNotFound,
    KnowledgeService,
)
from full_view_agent.application.session_run_service import new_id
from full_view_agent.domain.contract_model import ContractModel


class KnowledgeBaseCreateBody(ContractModel):
    app_id: str = Field(min_length=2, max_length=128)
    knowledge_base_id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=2_000)


class KnowledgeDocumentBody(ContractModel):
    document_id: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=500)
    media_type: Literal["text/plain", "text/markdown"]
    content: str = Field(min_length=1, max_length=5_000_000)
    data_source_id: str = Field(default="inline", min_length=1, max_length=128)


class KnowledgeImportBody(ContractModel):
    data_source_id: str = Field(min_length=1, max_length=128)
    document_id: str = Field(min_length=1, max_length=128)
    filename: str = Field(min_length=1, max_length=500)
    media_type: Literal["text/plain", "text/markdown"]
    content_base64: str = Field(min_length=1, max_length=8_000_000)


class KnowledgeSearchBody(ContractModel):
    query: str = Field(min_length=1, max_length=4_000)
    knowledge_base_ids: list[str] = Field(min_length=1, max_length=50)
    limit: int = Field(default=5, ge=1, le=100)


class KnowledgeAccessPolicyBody(ContractModel):
    public_within_app: bool
    allowed_user_ids: list[str] = Field(default_factory=list, max_length=500)
    allowed_roles: list[str] = Field(default_factory=list, max_length=100)


class KnowledgeActionBody(ContractModel):
    reason: str = Field(min_length=1, max_length=2_000)

    @field_validator("reason")
    @classmethod
    def reason_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("reason must not be blank")
        return value


class KnowledgeEnvelope(ContractModel):
    data: dict[str, object]
    meta: ResponseMeta


class KnowledgeListEnvelope(ContractModel):
    data: list[dict[str, object]]
    meta: ResponseMeta


def create_knowledge_router(service: KnowledgeService) -> APIRouter:
    router = APIRouter(prefix="/capability-api/v1", tags=["knowledge-management"])
    authorizer = ControlPlaneAuthorizer.compatibility_default()

    def meta() -> ResponseMeta:
        return ResponseMeta(request_id=new_id("req"))

    def tenant(user: CurrentUser) -> str:
        return user.identity.principal.tenant_id

    def actor(user: CurrentUser) -> str:
        return user.identity.principal.user_id

    def require(user: CurrentUser, permission: ControlPlanePermission) -> None:
        authorizer.require(user.identity, permission)

    async def translate(coro):
        try:
            return await coro
        except KnowledgeResourceNotFound as exc:
            raise ResourceNotFound(str(exc)) from exc

    @router.get("/knowledge-bases")
    async def list_bases(
        app_id: str,
        user: Annotated[CurrentUser, Depends(require_capability_identity)],
    ) -> KnowledgeListEnvelope:
        require(user, ControlPlanePermission.CAPABILITY_READ)
        items = await service.list_knowledge_bases(
            tenant_id=tenant(user), app_id=app_id
        )
        data: list[dict[str, object]] = []
        for item in items:
            row = item.model_dump(mode="json")
            index = await service.get_index_status(
                tenant_id=tenant(user),
                app_id=app_id,
                knowledge_base_id=item.knowledge_base_id,
            ) if item.published_version is not None else None
            documents = await service.list_documents(
                tenant_id=tenant(user), app_id=app_id,
                knowledge_base_id=item.knowledge_base_id,
            )
            row["document_count"] = len(documents)
            row["index_status"] = index.status if index else "not_built"
            data.append(row)
        return KnowledgeListEnvelope(data=data, meta=meta())

    @router.post("/knowledge-bases", status_code=status.HTTP_201_CREATED)
    async def create_base(
        body: KnowledgeBaseCreateBody,
        user: Annotated[CurrentUser, Depends(require_capability_identity)],
    ) -> KnowledgeEnvelope:
        require(user, ControlPlanePermission.CAPABILITY_MANAGE)
        item = await service.create_knowledge_base(
            tenant_id=tenant(user), actor_id=actor(user), **body.model_dump()
        )
        return KnowledgeEnvelope(data=item.model_dump(mode="json"), meta=meta())

    @router.get("/knowledge-bases/{knowledge_base_id}/documents")
    async def list_documents(
        knowledge_base_id: str, app_id: str,
        user: Annotated[CurrentUser, Depends(require_capability_identity)],
    ) -> KnowledgeListEnvelope:
        require(user, ControlPlanePermission.CAPABILITY_READ)
        items = await translate(service.list_documents(
            tenant_id=tenant(user), app_id=app_id,
            knowledge_base_id=knowledge_base_id,
        ))
        return KnowledgeListEnvelope(
            data=[item.model_dump(mode="json", exclude={"content"}) for item in items],
            meta=meta(),
        )

    @router.post("/knowledge-bases/{knowledge_base_id}/documents", status_code=201)
    async def put_document(
        knowledge_base_id: str, app_id: str, body: KnowledgeDocumentBody,
        user: Annotated[CurrentUser, Depends(require_capability_identity)],
    ) -> KnowledgeEnvelope:
        require(user, ControlPlanePermission.CAPABILITY_MANAGE)
        item = await translate(service.put_document(
            tenant_id=tenant(user), app_id=app_id,
            knowledge_base_id=knowledge_base_id, actor_id=actor(user),
            **body.model_dump(),
        ))
        return KnowledgeEnvelope(
            data=item.model_dump(mode="json", exclude={"content"}), meta=meta()
        )

    @router.post("/knowledge-bases/{knowledge_base_id}/documents/import", status_code=201)
    async def import_document(
        knowledge_base_id: str, app_id: str, body: KnowledgeImportBody,
        user: Annotated[CurrentUser, Depends(require_capability_identity)],
    ) -> KnowledgeEnvelope:
        require(user, ControlPlanePermission.CAPABILITY_MANAGE)
        try:
            payload = base64.b64decode(body.content_base64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("content_base64 is invalid") from exc
        item = await translate(service.import_document(
            tenant_id=tenant(user), app_id=app_id,
            knowledge_base_id=knowledge_base_id,
            data_source_id=body.data_source_id, document_id=body.document_id,
            filename=body.filename, media_type=body.media_type, payload=payload,
            actor_id=actor(user),
        ))
        return KnowledgeEnvelope(
            data=item.model_dump(mode="json", exclude={"content"}), meta=meta()
        )

    @router.delete("/knowledge-bases/{knowledge_base_id}/documents/{document_id}", status_code=204)
    async def delete_document(
        knowledge_base_id: str, document_id: str, app_id: str,
        user: Annotated[CurrentUser, Depends(require_capability_identity)],
    ) -> Response:
        require(user, ControlPlanePermission.CAPABILITY_MANAGE)
        await translate(service.delete_document(
            tenant_id=tenant(user), app_id=app_id, knowledge_base_id=knowledge_base_id,
            document_id=document_id, actor_id=actor(user),
        ))
        return Response(status_code=204)

    @router.post("/knowledge-bases/{knowledge_base_id}/publish")
    async def publish(
        knowledge_base_id: str, app_id: str, body: KnowledgeActionBody,
        user: Annotated[CurrentUser, Depends(require_capability_identity)],
    ) -> KnowledgeEnvelope:
        require(user, ControlPlanePermission.CAPABILITY_PUBLISH)
        item = await translate(service.publish(
            tenant_id=tenant(user), app_id=app_id, knowledge_base_id=knowledge_base_id,
            actor_id=actor(user), reason=body.reason,
        ))
        return KnowledgeEnvelope(data=item.model_dump(mode="json"), meta=meta())

    @router.get("/knowledge-bases/{knowledge_base_id}/index-status")
    async def index_status(
        knowledge_base_id: str, app_id: str,
        user: Annotated[CurrentUser, Depends(require_capability_identity)],
    ) -> KnowledgeEnvelope:
        require(user, ControlPlanePermission.CAPABILITY_READ)
        item = await translate(service.get_index_status(
            tenant_id=tenant(user), app_id=app_id, knowledge_base_id=knowledge_base_id,
        ))
        return KnowledgeEnvelope(data=item.model_dump(mode="json"), meta=meta())

    @router.post("/knowledge-bases/{knowledge_base_id}/rebuild-index")
    async def rebuild(
        knowledge_base_id: str, app_id: str, body: KnowledgeActionBody,
        user: Annotated[CurrentUser, Depends(require_capability_identity)],
    ) -> KnowledgeEnvelope:
        require(user, ControlPlanePermission.CAPABILITY_MANAGE)
        item = await translate(service.rebuild_index(
            tenant_id=tenant(user), app_id=app_id, knowledge_base_id=knowledge_base_id,
            actor_id=actor(user), reason=body.reason,
        ))
        return KnowledgeEnvelope(data=item.model_dump(mode="json"), meta=meta())

    @router.post("/knowledge-bases/search-test")
    async def search_test(
        app_id: str, body: KnowledgeSearchBody,
        user: Annotated[CurrentUser, Depends(require_capability_identity)],
    ) -> KnowledgeListEnvelope:
        require(user, ControlPlanePermission.CAPABILITY_READ)
        hits = await service.search(
            tenant_id=tenant(user), app_id=app_id,
            user_id=actor(user), roles=user.identity.principal.roles,
            **body.model_dump(),
        )
        return KnowledgeListEnvelope(
            data=[hit.model_dump(mode="json") for hit in hits], meta=meta()
        )

    @router.patch("/knowledge-bases/{knowledge_base_id}/access-policy")
    async def access_policy(
        knowledge_base_id: str, app_id: str, body: KnowledgeAccessPolicyBody,
        user: Annotated[CurrentUser, Depends(require_capability_identity)],
    ) -> KnowledgeEnvelope:
        require(user, ControlPlanePermission.CAPABILITY_MANAGE)
        item = await translate(service.set_access_policy(
            tenant_id=tenant(user), app_id=app_id, knowledge_base_id=knowledge_base_id,
            actor_id=actor(user), **body.model_dump(),
        ))
        return KnowledgeEnvelope(data=item.model_dump(mode="json"), meta=meta())

    @router.post("/knowledge-bases/{knowledge_base_id}/binding/{action}")
    async def binding(
        knowledge_base_id: str, app_id: str,
        action: Literal["enable", "disable"], body: KnowledgeActionBody,
        user: Annotated[CurrentUser, Depends(require_capability_identity)],
    ) -> KnowledgeEnvelope:
        require(user, ControlPlanePermission.CAPABILITY_MANAGE)
        item = await translate(service.set_application_binding(
            tenant_id=tenant(user), app_id=app_id, knowledge_base_id=knowledge_base_id,
            enabled=action == "enable", actor_id=actor(user), reason=body.reason,
        ))
        return KnowledgeEnvelope(data=item.model_dump(mode="json"), meta=meta())

    @router.get("/knowledge-bases/{knowledge_base_id}/audit-events")
    async def audit_events(
        knowledge_base_id: str, app_id: str,
        user: Annotated[CurrentUser, Depends(require_capability_identity)],
    ) -> KnowledgeListEnvelope:
        require(user, ControlPlanePermission.CAPABILITY_READ)
        items = await service.list_audit_events(
            tenant_id=tenant(user), app_id=app_id, knowledge_base_id=knowledge_base_id,
        )
        return KnowledgeListEnvelope(
            data=[item.model_dump(mode="json") for item in items], meta=meta()
        )

    @router.delete("/knowledge-bases/{knowledge_base_id}", status_code=204)
    async def delete_base(
        knowledge_base_id: str, app_id: str,
        user: Annotated[CurrentUser, Depends(require_capability_identity)],
    ) -> Response:
        require(user, ControlPlanePermission.CAPABILITY_MANAGE)
        await translate(service.delete_knowledge_base(
            tenant_id=tenant(user), app_id=app_id, knowledge_base_id=knowledge_base_id,
            actor_id=actor(user),
        ))
        return Response(status_code=204)

    return router
