"""Knowledge ingestion, publication and fail-closed retrieval use cases."""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Protocol

from full_view_agent.domain.knowledge import (
    KnowledgeAuditEvent,
    KnowledgeBase,
    KnowledgeChunk,
    KnowledgeCitation,
    KnowledgeDataSource,
    KnowledgeDocument,
    KnowledgeIndexStatus,
    KnowledgePublication,
    KnowledgeSearchHit,
)


class KnowledgeRepository(Protocol):
    async def create_base(self, knowledge_base: KnowledgeBase) -> KnowledgeBase: ...

    async def get_base(
        self, *, tenant_id: str, app_id: str, knowledge_base_id: str
    ) -> KnowledgeBase | None: ...

    async def list_bases(self, *, tenant_id: str, app_id: str) -> list[KnowledgeBase]: ...

    async def put_document(self, document: KnowledgeDocument) -> KnowledgeDocument: ...

    async def delete_document(
        self,
        *,
        tenant_id: str,
        app_id: str,
        knowledge_base_id: str,
        document_id: str,
    ) -> bool: ...

    async def save_data_source(self, data_source: KnowledgeDataSource) -> KnowledgeDataSource: ...

    async def list_data_sources(
        self, *, tenant_id: str, app_id: str, knowledge_base_id: str
    ) -> list[KnowledgeDataSource]: ...

    async def delete_data_source(
        self,
        *,
        tenant_id: str,
        app_id: str,
        knowledge_base_id: str,
        data_source_id: str,
    ) -> bool: ...

    async def list_documents(
        self, *, tenant_id: str, app_id: str, knowledge_base_id: str
    ) -> list[KnowledgeDocument]: ...

    async def publish(
        self,
        *,
        knowledge_base: KnowledgeBase,
        publication: KnowledgePublication,
        chunks: Sequence[KnowledgeChunk],
    ) -> KnowledgePublication: ...

    async def list_chunks(
        self,
        *,
        tenant_id: str,
        app_id: str,
        knowledge_base_id: str,
        version: int,
    ) -> list[KnowledgeChunk]: ...

    async def save_index_status(self, status: KnowledgeIndexStatus) -> KnowledgeIndexStatus: ...

    async def get_index_status(
        self, *, tenant_id: str, app_id: str, knowledge_base_id: str
    ) -> KnowledgeIndexStatus | None: ...

    async def update_base(self, knowledge_base: KnowledgeBase) -> KnowledgeBase: ...

    async def append_audit_event(self, event: KnowledgeAuditEvent) -> None: ...

    async def list_audit_events(
        self, *, tenant_id: str, app_id: str, knowledge_base_id: str
    ) -> list[KnowledgeAuditEvent]: ...


class KnowledgeRetriever(Protocol):
    async def build(
        self, *, index: KnowledgeIndexStatus, chunks: Sequence[KnowledgeChunk]
    ) -> None: ...

    async def delete(self, *, index: KnowledgeIndexStatus) -> None: ...

    def rank(
        self, *, query: str, chunks: Sequence[KnowledgeChunk], limit: int
    ) -> list[tuple[KnowledgeChunk, float]]: ...


class KnowledgeDocumentParser(Protocol):
    def supports(self, *, filename: str, media_type: str) -> bool: ...

    def parse(self, *, filename: str, media_type: str, payload: bytes) -> str: ...


class KnowledgeResourceNotFound(LookupError):
    pass


class KnowledgeSearchRejected(RuntimeError):
    """Search failed before a trustworthy, scope-safe result could be produced."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"knowledge search rejected [{code}]: {message}")


class KnowledgeService:
    def __init__(
        self,
        *,
        repository: KnowledgeRepository,
        retriever: KnowledgeRetriever,
        parsers: Sequence[KnowledgeDocumentParser] = (),
        chunk_size: int = 800,
        chunk_overlap: int = 100,
        control_plane_tenant_id: str = "legacy",
    ) -> None:
        if chunk_size < 1:
            raise ValueError("chunk_size must be positive")
        if chunk_overlap < 0 or chunk_overlap >= chunk_size:
            raise ValueError("chunk_overlap must be non-negative and smaller than chunk_size")
        self._repository = repository
        self._retriever = retriever
        self._parsers = tuple(parsers)
        self._chunk_size = chunk_size
        self._chunk_overlap = chunk_overlap
        self._control_plane_tenant_id = control_plane_tenant_id

    async def is_ready_version(
        self, *, app_id: str, knowledge_base_id: str, version: int
    ) -> bool:
        """Fail-closed control-plane check for an exact published index."""
        knowledge_base = await self._repository.get_base(
            tenant_id=self._control_plane_tenant_id,
            app_id=app_id,
            knowledge_base_id=knowledge_base_id,
        )
        if (
            knowledge_base is None
            or not knowledge_base.active
            or not knowledge_base.application_binding_enabled
            or knowledge_base.published_version != version
        ):
            return False
        status = await self._repository.get_index_status(
            tenant_id=self._control_plane_tenant_id,
            app_id=app_id,
            knowledge_base_id=knowledge_base_id,
        )
        return bool(
            status is not None
            and status.status == "ready"
            and status.knowledge_base_version == version
        )

    async def create_knowledge_base(
        self,
        *,
        tenant_id: str,
        app_id: str,
        knowledge_base_id: str,
        name: str,
        description: str = "",
        actor_id: str = "system",
    ) -> KnowledgeBase:
        created = await self._repository.create_base(
            KnowledgeBase(
                tenant_id=tenant_id,
                app_id=app_id,
                knowledge_base_id=knowledge_base_id,
                name=name,
                description=description,
            )
        )
        await self._audit(created, action="knowledge_base.created", actor_id=actor_id)
        return created

    async def list_knowledge_bases(
        self, *, tenant_id: str, app_id: str
    ) -> list[KnowledgeBase]:
        return await self._repository.list_bases(tenant_id=tenant_id, app_id=app_id)

    async def put_document(
        self,
        *,
        tenant_id: str,
        app_id: str,
        knowledge_base_id: str,
        document_id: str,
        title: str,
        media_type: str,
        content: str,
        data_source_id: str = "inline",
        actor_id: str = "system",
    ) -> KnowledgeDocument:
        knowledge_base = await self._repository.get_base(
            tenant_id=tenant_id,
            app_id=app_id,
            knowledge_base_id=knowledge_base_id,
        )
        if knowledge_base is None:
            raise KnowledgeResourceNotFound("knowledge base not found in application scope")
        document = KnowledgeDocument(
            tenant_id=tenant_id,
            app_id=app_id,
            knowledge_base_id=knowledge_base_id,
            data_source_id=data_source_id,
            document_id=document_id,
            title=title,
            media_type=media_type,  # type: ignore[arg-type]
            content=content,
        )
        stored = await self._repository.put_document(document)
        await self._audit(
            knowledge_base,
            action="document.updated",
            actor_id=actor_id,
            document_id=document_id,
        )
        return stored

    async def import_document(
        self,
        *,
        tenant_id: str,
        app_id: str,
        knowledge_base_id: str,
        data_source_id: str,
        document_id: str,
        filename: str,
        media_type: str,
        payload: bytes,
        actor_id: str = "system",
    ) -> KnowledgeDocument:
        parser = next(
            (
                candidate
                for candidate in self._parsers
                if candidate.supports(filename=filename, media_type=media_type)
            ),
            None,
        )
        if parser is None:
            raise ValueError("unsupported document media type")
        content = parser.parse(filename=filename, media_type=media_type, payload=payload)
        knowledge_base = await self._require_base(
            tenant_id=tenant_id, app_id=app_id, knowledge_base_id=knowledge_base_id
        )
        data_source = KnowledgeDataSource(
            tenant_id=tenant_id,
            app_id=app_id,
            knowledge_base_id=knowledge_base_id,
            data_source_id=data_source_id,
            source_type="upload",
            filename=filename,
            media_type=media_type,  # type: ignore[arg-type]
        )
        await self._repository.save_data_source(data_source)
        document = await self.put_document(
            tenant_id=tenant_id,
            app_id=app_id,
            knowledge_base_id=knowledge_base_id,
            data_source_id=data_source_id,
            document_id=document_id,
            title=filename,
            media_type=media_type,
            content=content,
            actor_id=actor_id,
        )
        await self._audit(
            knowledge_base,
            action="document.imported",
            actor_id=actor_id,
            document_id=document_id,
        )
        return document

    async def list_documents(
        self, *, tenant_id: str, app_id: str, knowledge_base_id: str
    ) -> list[KnowledgeDocument]:
        await self._require_base(
            tenant_id=tenant_id, app_id=app_id, knowledge_base_id=knowledge_base_id
        )
        return await self._repository.list_documents(
            tenant_id=tenant_id, app_id=app_id, knowledge_base_id=knowledge_base_id
        )

    async def list_data_sources(
        self, *, tenant_id: str, app_id: str, knowledge_base_id: str
    ) -> list[KnowledgeDataSource]:
        await self._require_base(
            tenant_id=tenant_id, app_id=app_id, knowledge_base_id=knowledge_base_id
        )
        return await self._repository.list_data_sources(
            tenant_id=tenant_id, app_id=app_id, knowledge_base_id=knowledge_base_id
        )

    async def delete_data_source(
        self,
        *,
        tenant_id: str,
        app_id: str,
        knowledge_base_id: str,
        data_source_id: str,
        actor_id: str = "system",
    ) -> None:
        knowledge_base = await self._require_base(
            tenant_id=tenant_id, app_id=app_id, knowledge_base_id=knowledge_base_id
        )
        deleted = await self._repository.delete_data_source(
            tenant_id=tenant_id,
            app_id=app_id,
            knowledge_base_id=knowledge_base_id,
            data_source_id=data_source_id,
        )
        if not deleted:
            raise KnowledgeResourceNotFound("knowledge data source not found")
        documents = await self._repository.list_documents(
            tenant_id=tenant_id, app_id=app_id, knowledge_base_id=knowledge_base_id
        )
        for document in documents:
            if document.data_source_id == data_source_id:
                await self._repository.delete_document(
                    tenant_id=tenant_id,
                    app_id=app_id,
                    knowledge_base_id=knowledge_base_id,
                    document_id=document.document_id,
                )
        await self._audit(
            knowledge_base, action="data_source.deleted", actor_id=actor_id
        )

    async def publish(
        self,
        *,
        tenant_id: str,
        app_id: str,
        knowledge_base_id: str,
        actor_id: str = "system",
        reason: str = "",
    ) -> KnowledgePublication:
        knowledge_base = await self._repository.get_base(
            tenant_id=tenant_id,
            app_id=app_id,
            knowledge_base_id=knowledge_base_id,
        )
        if knowledge_base is None:
            raise KnowledgeResourceNotFound("knowledge base not found in application scope")
        documents = await self._repository.list_documents(
            tenant_id=tenant_id,
            app_id=app_id,
            knowledge_base_id=knowledge_base_id,
        )
        version = (knowledge_base.published_version or 0) + 1
        chunks = [
            chunk
            for document in documents
            for chunk in deterministic_chunks(
                document=document,
                version=version,
                chunk_size=self._chunk_size,
                chunk_overlap=self._chunk_overlap,
            )
        ]
        publication = KnowledgePublication(
            tenant_id=tenant_id,
            app_id=app_id,
            knowledge_base_id=knowledge_base_id,
            version=version,
            document_count=len(documents),
            chunk_count=len(chunks),
        )
        published_base = knowledge_base.model_copy(
            update={"published_version": version, "updated_at": datetime.now(UTC)}
        )
        stored_publication = await self._repository.publish(
            knowledge_base=published_base,
            publication=publication,
            chunks=chunks,
        )
        index = KnowledgeIndexStatus(
            tenant_id=tenant_id,
            app_id=app_id,
            knowledge_base_id=knowledge_base_id,
            knowledge_base_version=version,
            status="building",
            chunk_count=len(chunks),
        )
        await self._repository.save_index_status(index)
        try:
            await self._retriever.build(index=index, chunks=chunks)
        except Exception as exc:
            await self._repository.save_index_status(
                index.model_copy(
                    update={
                        "status": "failed",
                        "error_message": str(exc)[:2000],
                        "updated_at": datetime.now(UTC),
                    }
                )
            )
            raise KnowledgeSearchRejected(
                "INDEX_BUILD_FAILED", "keyword index build failed"
            ) from exc
        await self._repository.save_index_status(
            index.model_copy(update={"status": "ready", "updated_at": datetime.now(UTC)})
        )
        await self._audit(
            published_base,
            action="knowledge_base.published",
            actor_id=actor_id,
            version=version,
            detail=reason,
        )
        return stored_publication

    async def search(
        self,
        *,
        tenant_id: str,
        app_id: str,
        knowledge_base_ids: Sequence[str],
        query: str,
        limit: int = 5,
        user_id: str = "anonymous",
        roles: Sequence[str] = (),
        version_constraints: dict[str, int] | None = None,
    ) -> list[KnowledgeSearchHit]:
        if not query.strip():
            raise ValueError("query must not be blank")
        if limit < 1 or limit > 100:
            raise ValueError("limit must be between 1 and 100")
        try:
            chunks: list[KnowledgeChunk] = []
            for knowledge_base_id in dict.fromkeys(knowledge_base_ids):
                knowledge_base = await self._repository.get_base(
                    tenant_id=tenant_id,
                    app_id=app_id,
                    knowledge_base_id=knowledge_base_id,
                )
                # Missing, cross-scope and unpublished bases are indistinguishable.
                if (
                    knowledge_base is None
                    or not knowledge_base.active
                    or not knowledge_base.application_binding_enabled
                    or knowledge_base.published_version is None
                    or not _can_access(knowledge_base, user_id=user_id, roles=roles)
                ):
                    continue
                selected_version = knowledge_base.published_version
                if version_constraints is not None:
                    selected_version = version_constraints.get(knowledge_base_id)
                    if selected_version is None:
                        continue
                else:
                    index_status = await self._repository.get_index_status(
                        tenant_id=tenant_id,
                        app_id=app_id,
                        knowledge_base_id=knowledge_base_id,
                    )
                    if (
                        index_status is None
                        or index_status.knowledge_base_version != selected_version
                        or index_status.status != "ready"
                    ):
                        raise ValueError("knowledge index is not ready for published version")
                scoped_chunks = await self._repository.list_chunks(
                    tenant_id=tenant_id,
                    app_id=app_id,
                    knowledge_base_id=knowledge_base_id,
                    version=selected_version,
                )
                if any(
                    chunk.tenant_id != tenant_id
                    or chunk.app_id != app_id
                    or chunk.knowledge_base_id != knowledge_base_id
                    or chunk.knowledge_base_version != selected_version
                    for chunk in scoped_chunks
                ):
                    raise ValueError("knowledge repository returned data outside requested scope")
                chunks.extend(scoped_chunks)
            ranked = self._retriever.rank(query=query.strip(), chunks=chunks, limit=limit)
            allowed_chunks = {
                (
                    chunk.tenant_id,
                    chunk.app_id,
                    chunk.knowledge_base_id,
                    chunk.knowledge_base_version,
                    chunk.document_id,
                    chunk.chunk_id,
                    chunk.content,
                )
                for chunk in chunks
            }
            if len(ranked) > limit or any(
                (
                    chunk.tenant_id,
                    chunk.app_id,
                    chunk.knowledge_base_id,
                    chunk.knowledge_base_version,
                    chunk.document_id,
                    chunk.chunk_id,
                    chunk.content,
                )
                not in allowed_chunks
                or not math.isfinite(score)
                or score <= 0
                for chunk, score in ranked
            ):
                raise ValueError("knowledge retriever returned an invalid result set")
        except Exception as exc:
            raise KnowledgeSearchRejected(
                "RETRIEVAL_FAILED", "repository or keyword index unavailable"
            ) from exc
        return [
            KnowledgeSearchHit(
                content=chunk.content,
                score=score,
                citation=KnowledgeCitation(
                    knowledge_base_id=chunk.knowledge_base_id,
                    knowledge_base_version=chunk.knowledge_base_version,
                    document_id=chunk.document_id,
                    document_title=chunk.document_title,
                    chunk_id=chunk.chunk_id,
                    chunk_ordinal=chunk.ordinal,
                    page_number=chunk.page_number,
                    paragraph_start=chunk.paragraph_start,
                    paragraph_end=chunk.paragraph_end,
                ),
            )
            for chunk, score in ranked
        ]

    async def get_index_status(
        self, *, tenant_id: str, app_id: str, knowledge_base_id: str
    ) -> KnowledgeIndexStatus:
        status = await self._repository.get_index_status(
            tenant_id=tenant_id, app_id=app_id, knowledge_base_id=knowledge_base_id
        )
        if status is None:
            raise KnowledgeResourceNotFound("knowledge index not found")
        return status

    async def set_access_policy(
        self,
        *,
        tenant_id: str,
        app_id: str,
        knowledge_base_id: str,
        public_within_app: bool,
        allowed_user_ids: Sequence[str],
        allowed_roles: Sequence[str],
        actor_id: str = "system",
    ) -> KnowledgeBase:
        knowledge_base = await self._require_base(
            tenant_id=tenant_id, app_id=app_id, knowledge_base_id=knowledge_base_id
        )
        updated = knowledge_base.model_copy(
            update={
                "public_within_app": public_within_app,
                "allowed_user_ids": sorted(set(allowed_user_ids)),
                "allowed_roles": sorted(set(allowed_roles)),
                "updated_at": datetime.now(UTC),
            }
        )
        await self._repository.update_base(updated)
        await self._audit(updated, action="access_policy.updated", actor_id=actor_id)
        return updated

    async def set_application_binding(
        self,
        *,
        tenant_id: str,
        app_id: str,
        knowledge_base_id: str,
        enabled: bool,
        actor_id: str = "system",
        reason: str = "",
    ) -> KnowledgeBase:
        knowledge_base = await self._require_base(
            tenant_id=tenant_id, app_id=app_id, knowledge_base_id=knowledge_base_id
        )
        updated = knowledge_base.model_copy(
            update={
                "application_binding_enabled": enabled,
                "updated_at": datetime.now(UTC),
            }
        )
        await self._repository.update_base(updated)
        await self._audit(
            updated,
            action=f"application_binding.{'enabled' if enabled else 'disabled'}",
            actor_id=actor_id,
            detail=reason,
        )
        return updated

    async def rebuild_index(
        self,
        *,
        tenant_id: str,
        app_id: str,
        knowledge_base_id: str,
        actor_id: str = "system",
        reason: str = "",
    ) -> KnowledgeIndexStatus:
        knowledge_base = await self._require_base(
            tenant_id=tenant_id, app_id=app_id, knowledge_base_id=knowledge_base_id
        )
        if knowledge_base.published_version is None:
            raise KnowledgeResourceNotFound("knowledge base has no published version")
        chunks = await self._repository.list_chunks(
            tenant_id=tenant_id,
            app_id=app_id,
            knowledge_base_id=knowledge_base_id,
            version=knowledge_base.published_version,
        )
        index = KnowledgeIndexStatus(
            tenant_id=tenant_id,
            app_id=app_id,
            knowledge_base_id=knowledge_base_id,
            knowledge_base_version=knowledge_base.published_version,
            status="building",
            chunk_count=len(chunks),
        )
        await self._repository.save_index_status(index)
        try:
            await self._retriever.build(index=index, chunks=chunks)
        except Exception as exc:
            failed = index.model_copy(
                update={
                    "status": "failed",
                    "error_message": str(exc)[:2000],
                    "updated_at": datetime.now(UTC),
                }
            )
            await self._repository.save_index_status(failed)
            raise KnowledgeSearchRejected(
                "INDEX_BUILD_FAILED", "keyword index rebuild failed"
            ) from exc
        ready = index.model_copy(update={"status": "ready", "updated_at": datetime.now(UTC)})
        await self._repository.save_index_status(ready)
        await self._audit(
            knowledge_base,
            action="index.rebuilt",
            actor_id=actor_id,
            version=knowledge_base.published_version,
            detail=reason,
        )
        return ready

    async def delete_document(
        self,
        *,
        tenant_id: str,
        app_id: str,
        knowledge_base_id: str,
        document_id: str,
        actor_id: str = "system",
    ) -> None:
        knowledge_base = await self._require_base(
            tenant_id=tenant_id, app_id=app_id, knowledge_base_id=knowledge_base_id
        )
        deleted = await self._repository.delete_document(
            tenant_id=tenant_id,
            app_id=app_id,
            knowledge_base_id=knowledge_base_id,
            document_id=document_id,
        )
        if not deleted:
            raise KnowledgeResourceNotFound("knowledge document not found")
        await self._audit(
            knowledge_base,
            action="document.deleted",
            actor_id=actor_id,
            document_id=document_id,
        )

    async def delete_knowledge_base(
        self,
        *,
        tenant_id: str,
        app_id: str,
        knowledge_base_id: str,
        actor_id: str = "system",
    ) -> None:
        knowledge_base = await self._require_base(
            tenant_id=tenant_id, app_id=app_id, knowledge_base_id=knowledge_base_id
        )
        updated = knowledge_base.model_copy(
            update={
                "active": False,
                "application_binding_enabled": False,
                "updated_at": datetime.now(UTC),
            }
        )
        await self._repository.update_base(updated)
        index = await self._repository.get_index_status(
            tenant_id=tenant_id, app_id=app_id, knowledge_base_id=knowledge_base_id
        )
        if index is not None:
            await self._retriever.delete(index=index)
            await self._repository.save_index_status(
                index.model_copy(update={"status": "deleted", "updated_at": datetime.now(UTC)})
            )
        await self._audit(updated, action="knowledge_base.deleted", actor_id=actor_id)

    async def list_audit_events(
        self, *, tenant_id: str, app_id: str, knowledge_base_id: str
    ) -> list[KnowledgeAuditEvent]:
        return await self._repository.list_audit_events(
            tenant_id=tenant_id, app_id=app_id, knowledge_base_id=knowledge_base_id
        )

    async def _require_base(
        self, *, tenant_id: str, app_id: str, knowledge_base_id: str
    ) -> KnowledgeBase:
        knowledge_base = await self._repository.get_base(
            tenant_id=tenant_id, app_id=app_id, knowledge_base_id=knowledge_base_id
        )
        if knowledge_base is None or not knowledge_base.active:
            raise KnowledgeResourceNotFound("knowledge base not found in application scope")
        return knowledge_base

    async def _audit(
        self,
        knowledge_base: KnowledgeBase,
        *,
        action: str,
        actor_id: str,
        document_id: str | None = None,
        version: int | None = None,
        detail: str = "",
    ) -> None:
        await self._repository.append_audit_event(
            KnowledgeAuditEvent(
                tenant_id=knowledge_base.tenant_id,
                app_id=knowledge_base.app_id,
                knowledge_base_id=knowledge_base.knowledge_base_id,
                action=action,
                actor_id=actor_id,
                document_id=document_id,
                knowledge_base_version=version,
                detail=detail,
            )
        )


def deterministic_chunks(
    *,
    document: KnowledgeDocument,
    version: int,
    chunk_size: int,
    chunk_overlap: int,
) -> list[KnowledgeChunk]:
    """Split normalized text into stable character windows."""
    step = chunk_size - chunk_overlap
    result: list[KnowledgeChunk] = []
    paragraph_starts = [0] + [
        index + 1 for index, char in enumerate(document.content) if char == "\n"
    ]
    for ordinal, start in enumerate(range(0, len(document.content), step)):
        content = document.content[start : start + chunk_size]
        if not content:
            break
        result.append(
            KnowledgeChunk(
                tenant_id=document.tenant_id,
                app_id=document.app_id,
                knowledge_base_id=document.knowledge_base_id,
                knowledge_base_version=version,
                document_id=document.document_id,
                document_title=document.title,
                chunk_id=f"{document.document_id}:{version}:{ordinal}",
                ordinal=ordinal,
                content=content,
                paragraph_start=_paragraph_number(paragraph_starts, start),
                paragraph_end=_paragraph_number(
                    paragraph_starts, min(len(document.content) - 1, start + len(content) - 1)
                ),
            )
        )
        if start + chunk_size >= len(document.content):
            break
    return result


def _paragraph_number(starts: Sequence[int], offset: int) -> int:
    return sum(start <= offset for start in starts)


def _can_access(
    knowledge_base: KnowledgeBase, *, user_id: str, roles: Sequence[str]
) -> bool:
    return (
        knowledge_base.public_within_app
        or user_id in knowledge_base.allowed_user_ids
        or bool(set(roles).intersection(knowledge_base.allowed_roles))
    )
