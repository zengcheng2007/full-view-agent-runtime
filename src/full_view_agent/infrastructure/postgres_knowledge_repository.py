"""PostgreSQL persistence for the portable V020 knowledge schema."""

# pyright: reportArgumentType=false, reportCallIssue=false, reportMissingImports=false

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from uuid import uuid4

import psycopg

from full_view_agent.domain.knowledge import (
    KnowledgeAuditEvent,
    KnowledgeBase,
    KnowledgeChunk,
    KnowledgeDataSource,
    KnowledgeDocument,
    KnowledgeIndexStatus,
    KnowledgePublication,
)

_SAFE_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]{0,62}$")


class PostgresKnowledgeRepository:
    """V020 adapter using standard columns and TEXT-encoded policy lists."""

    def __init__(self, *, dsn: str, schema: str = "full_view_agent") -> None:
        if not _SAFE_IDENTIFIER.fullmatch(schema):
            raise ValueError("PostgreSQL schema must be a safe lower-case identifier")
        self._dsn = dsn
        self._schema = schema

    async def create_base(self, knowledge_base: KnowledgeBase) -> KnowledgeBase:
        async with await self._connect() as connection:
            await connection.execute(
                f"INSERT INTO {self._table('knowledge_bases')} "
                "(tenant_id, app_id, knowledge_base_id, name, description, published_version, "
                "active, application_binding_enabled, public_within_app, allowed_user_ids_json, "
                "allowed_roles_json, created_at, updated_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                _base_values(knowledge_base),
            )
        return knowledge_base

    async def get_base(
        self, *, tenant_id: str, app_id: str, knowledge_base_id: str
    ) -> KnowledgeBase | None:
        async with await self._connect() as connection:
            row = await (
                await connection.execute(
                    self._base_select()
                    + " WHERE tenant_id = %s AND app_id = %s AND knowledge_base_id = %s",
                    (tenant_id, app_id, knowledge_base_id),
                )
            ).fetchone()
        return _base_from_row(row) if row is not None else None

    async def list_bases(self, *, tenant_id: str, app_id: str) -> list[KnowledgeBase]:
        async with await self._connect() as connection:
            rows = await (
                await connection.execute(
                    self._base_select()
                    + " WHERE tenant_id = %s AND app_id = %s AND active = TRUE "
                    "ORDER BY knowledge_base_id",
                    (tenant_id, app_id),
                )
            ).fetchall()
        return [_base_from_row(row) for row in rows]

    async def update_base(self, knowledge_base: KnowledgeBase) -> KnowledgeBase:
        async with await self._connect() as connection:
            cursor = await connection.execute(
                f"UPDATE {self._table('knowledge_bases')} SET name = %s, description = %s, "
                "published_version = %s, active = %s, application_binding_enabled = %s, "
                "public_within_app = %s, allowed_user_ids_json = %s, allowed_roles_json = %s, "
                "updated_at = %s WHERE tenant_id = %s AND app_id = %s "
                "AND knowledge_base_id = %s",
                (
                    knowledge_base.name,
                    knowledge_base.description,
                    knowledge_base.published_version,
                    knowledge_base.active,
                    knowledge_base.application_binding_enabled,
                    knowledge_base.public_within_app,
                    json.dumps(knowledge_base.allowed_user_ids, ensure_ascii=False),
                    json.dumps(knowledge_base.allowed_roles, ensure_ascii=False),
                    knowledge_base.updated_at,
                    knowledge_base.tenant_id,
                    knowledge_base.app_id,
                    knowledge_base.knowledge_base_id,
                ),
            )
            if cursor.rowcount != 1:
                raise LookupError("knowledge base not found in application scope")
        return knowledge_base

    async def save_data_source(self, data_source: KnowledgeDataSource) -> KnowledgeDataSource:
        async with await self._connect() as connection:
            await connection.execute(
                f"INSERT INTO {self._table('knowledge_data_sources')} "
                "(tenant_id, app_id, knowledge_base_id, data_source_id, source_type, filename, "
                "media_type, status, created_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (tenant_id, app_id, knowledge_base_id, data_source_id) DO UPDATE SET "
                "source_type = EXCLUDED.source_type, filename = EXCLUDED.filename, "
                "media_type = EXCLUDED.media_type, status = EXCLUDED.status",
                (
                    data_source.tenant_id,
                    data_source.app_id,
                    data_source.knowledge_base_id,
                    data_source.data_source_id,
                    data_source.source_type,
                    data_source.filename,
                    data_source.media_type,
                    data_source.status,
                    data_source.created_at,
                ),
            )
        return data_source

    async def list_data_sources(
        self, *, tenant_id: str, app_id: str, knowledge_base_id: str
    ) -> list[KnowledgeDataSource]:
        async with await self._connect() as connection:
            rows = await (
                await connection.execute(
                    "SELECT tenant_id, app_id, knowledge_base_id, data_source_id, source_type, "
                    "filename, media_type, status, created_at FROM "
                    f"{self._table('knowledge_data_sources')} "
                    "WHERE tenant_id = %s AND app_id = %s AND knowledge_base_id = %s "
                    "AND status = 'ready' ORDER BY data_source_id",
                    (tenant_id, app_id, knowledge_base_id),
                )
            ).fetchall()
        return [_data_source_from_row(row) for row in rows]

    async def delete_data_source(
        self,
        *,
        tenant_id: str,
        app_id: str,
        knowledge_base_id: str,
        data_source_id: str,
    ) -> bool:
        async with await self._connect() as connection:
            cursor = await connection.execute(
                f"UPDATE {self._table('knowledge_data_sources')} SET status = 'deleted' "
                "WHERE tenant_id = %s AND app_id = %s AND knowledge_base_id = %s "
                "AND data_source_id = %s AND status <> 'deleted'",
                (tenant_id, app_id, knowledge_base_id, data_source_id),
            )
        return cursor.rowcount == 1

    async def put_document(self, document: KnowledgeDocument) -> KnowledgeDocument:
        async with await self._connect() as connection:
            await connection.execute(
                f"INSERT INTO {self._table('knowledge_documents')} "
                "(tenant_id, app_id, knowledge_base_id, data_source_id, document_id, title, "
                "media_type, content, updated_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (tenant_id, app_id, knowledge_base_id, document_id) DO UPDATE SET "
                "data_source_id = EXCLUDED.data_source_id, title = EXCLUDED.title, "
                "media_type = EXCLUDED.media_type, content = EXCLUDED.content, "
                "updated_at = EXCLUDED.updated_at",
                (
                    document.tenant_id,
                    document.app_id,
                    document.knowledge_base_id,
                    document.data_source_id,
                    document.document_id,
                    document.title,
                    document.media_type,
                    document.content,
                    document.updated_at,
                ),
            )
        return document

    async def delete_document(
        self,
        *,
        tenant_id: str,
        app_id: str,
        knowledge_base_id: str,
        document_id: str,
    ) -> bool:
        async with await self._connect() as connection:
            cursor = await connection.execute(
                f"DELETE FROM {self._table('knowledge_documents')} WHERE tenant_id = %s "
                "AND app_id = %s AND knowledge_base_id = %s AND document_id = %s",
                (tenant_id, app_id, knowledge_base_id, document_id),
            )
        return cursor.rowcount == 1

    async def list_documents(
        self, *, tenant_id: str, app_id: str, knowledge_base_id: str
    ) -> list[KnowledgeDocument]:
        async with await self._connect() as connection:
            rows = await (
                await connection.execute(
                    "SELECT tenant_id, app_id, knowledge_base_id, data_source_id, document_id, "
                    "title, media_type, content, updated_at FROM "
                    f"{self._table('knowledge_documents')} "
                    "WHERE tenant_id = %s AND app_id = %s AND knowledge_base_id = %s "
                    "ORDER BY document_id",
                    (tenant_id, app_id, knowledge_base_id),
                )
            ).fetchall()
        return [_document_from_row(row) for row in rows]

    async def publish(
        self,
        *,
        knowledge_base: KnowledgeBase,
        publication: KnowledgePublication,
        chunks: Sequence[KnowledgeChunk],
    ) -> KnowledgePublication:
        async with await self._connect() as connection:
            current = await (
                await connection.execute(
                    f"SELECT published_version FROM {self._table('knowledge_bases')} "
                    "WHERE tenant_id = %s AND app_id = %s AND knowledge_base_id = %s FOR UPDATE",
                    (
                        knowledge_base.tenant_id,
                        knowledge_base.app_id,
                        knowledge_base.knowledge_base_id,
                    ),
                )
            ).fetchone()
            expected_previous = publication.version - 1 or None
            if current is None or current[0] != expected_previous:
                raise ValueError("knowledge publication version conflict")
            await connection.execute(
                f"INSERT INTO {self._table('knowledge_publications')} "
                "(tenant_id, app_id, knowledge_base_id, knowledge_base_version, document_count, "
                "chunk_count, published_at) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (
                    publication.tenant_id,
                    publication.app_id,
                    publication.knowledge_base_id,
                    publication.version,
                    publication.document_count,
                    publication.chunk_count,
                    publication.published_at,
                ),
            )
            if chunks:
                cursor = connection.cursor()
                await cursor.executemany(
                    f"INSERT INTO {self._table('knowledge_chunks')} "
                    "(tenant_id, app_id, knowledge_base_id, knowledge_base_version, document_id, "
                    "document_title, chunk_id, ordinal, content, page_number, paragraph_start, "
                    "paragraph_end) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    [_chunk_values(chunk) for chunk in chunks],
                )
            await connection.execute(
                f"UPDATE {self._table('knowledge_bases')} SET published_version = %s, "
                "updated_at = %s WHERE tenant_id = %s AND app_id = %s AND knowledge_base_id = %s",
                (
                    publication.version,
                    knowledge_base.updated_at,
                    knowledge_base.tenant_id,
                    knowledge_base.app_id,
                    knowledge_base.knowledge_base_id,
                ),
            )
        return publication

    async def list_chunks(
        self,
        *,
        tenant_id: str,
        app_id: str,
        knowledge_base_id: str,
        version: int,
    ) -> list[KnowledgeChunk]:
        async with await self._connect() as connection:
            rows = await (
                await connection.execute(
                    "SELECT tenant_id, app_id, knowledge_base_id, knowledge_base_version, "
                    "document_id, document_title, chunk_id, ordinal, content, page_number, "
                    f"paragraph_start, paragraph_end FROM {self._table('knowledge_chunks')} "
                    "WHERE tenant_id = %s AND app_id = %s AND knowledge_base_id = %s "
                    "AND knowledge_base_version = %s ORDER BY document_id, ordinal",
                    (tenant_id, app_id, knowledge_base_id, version),
                )
            ).fetchall()
        return [_chunk_from_row(row) for row in rows]

    async def save_index_status(self, status: KnowledgeIndexStatus) -> KnowledgeIndexStatus:
        async with await self._connect() as connection:
            await connection.execute(
                f"INSERT INTO {self._table('knowledge_index_status')} "
                "(tenant_id, app_id, knowledge_base_id, knowledge_base_version, status, "
                "chunk_count, error_message, updated_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (tenant_id, app_id, knowledge_base_id, knowledge_base_version) "
                "DO UPDATE SET status = EXCLUDED.status, chunk_count = EXCLUDED.chunk_count, "
                "error_message = EXCLUDED.error_message, updated_at = EXCLUDED.updated_at",
                (
                    status.tenant_id,
                    status.app_id,
                    status.knowledge_base_id,
                    status.knowledge_base_version,
                    status.status,
                    status.chunk_count,
                    status.error_message,
                    status.updated_at,
                ),
            )
        return status

    async def get_index_status(
        self, *, tenant_id: str, app_id: str, knowledge_base_id: str
    ) -> KnowledgeIndexStatus | None:
        async with await self._connect() as connection:
            row = await (
                await connection.execute(
                    "SELECT tenant_id, app_id, knowledge_base_id, knowledge_base_version, "
                    "status, chunk_count, error_message, updated_at FROM "
                    f"{self._table('knowledge_index_status')} "
                    "WHERE tenant_id = %s AND app_id = %s AND knowledge_base_id = %s "
                    "ORDER BY knowledge_base_version DESC LIMIT 1",
                    (tenant_id, app_id, knowledge_base_id),
                )
            ).fetchone()
        return _index_from_row(row) if row is not None else None

    async def append_audit_event(self, event: KnowledgeAuditEvent) -> None:
        async with await self._connect() as connection:
            await connection.execute(
                f"INSERT INTO {self._table('knowledge_audit_events')} "
                "(event_id, tenant_id, app_id, knowledge_base_id, action, actor_id, document_id, "
                "knowledge_base_version, detail, occurred_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    str(uuid4()),
                    event.tenant_id,
                    event.app_id,
                    event.knowledge_base_id,
                    event.action,
                    event.actor_id,
                    event.document_id,
                    event.knowledge_base_version,
                    event.detail,
                    event.occurred_at,
                ),
            )

    async def list_audit_events(
        self, *, tenant_id: str, app_id: str, knowledge_base_id: str
    ) -> list[KnowledgeAuditEvent]:
        async with await self._connect() as connection:
            rows = await (
                await connection.execute(
                    "SELECT tenant_id, app_id, knowledge_base_id, action, actor_id, document_id, "
                    "knowledge_base_version, detail, occurred_at FROM "
                    f"{self._table('knowledge_audit_events')} WHERE tenant_id = %s AND app_id = %s "
                    "AND knowledge_base_id = %s ORDER BY occurred_at, event_id",
                    (tenant_id, app_id, knowledge_base_id),
                )
            ).fetchall()
        return [_audit_from_row(row) for row in rows]

    async def _connect(self) -> psycopg.AsyncConnection:
        return await psycopg.AsyncConnection.connect(self._dsn)

    def _table(self, name: str) -> str:
        return f'"{self._schema}"."{name}"'

    def _base_select(self) -> str:
        return (
            "SELECT tenant_id, app_id, knowledge_base_id, name, description, published_version, "
            "active, application_binding_enabled, public_within_app, allowed_user_ids_json, "
            f"allowed_roles_json, created_at, updated_at FROM {self._table('knowledge_bases')}"
        )


def _base_values(item: KnowledgeBase) -> tuple[object, ...]:
    return (
        item.tenant_id,
        item.app_id,
        item.knowledge_base_id,
        item.name,
        item.description,
        item.published_version,
        item.active,
        item.application_binding_enabled,
        item.public_within_app,
        json.dumps(item.allowed_user_ids, ensure_ascii=False),
        json.dumps(item.allowed_roles, ensure_ascii=False),
        item.created_at,
        item.updated_at,
    )


def _base_from_row(row: Sequence[object]) -> KnowledgeBase:
    return KnowledgeBase(
        tenant_id=str(row[0]),
        app_id=str(row[1]),
        knowledge_base_id=str(row[2]),
        name=str(row[3]),
        description=str(row[4]),
        published_version=int(row[5]) if row[5] is not None else None,
        active=bool(row[6]),
        application_binding_enabled=bool(row[7]),
        public_within_app=bool(row[8]),
        allowed_user_ids=json.loads(str(row[9])),
        allowed_roles=json.loads(str(row[10])),
        created_at=row[11],
        updated_at=row[12],
    )


def _document_from_row(row: Sequence[object]) -> KnowledgeDocument:
    return KnowledgeDocument(
        tenant_id=str(row[0]),
        app_id=str(row[1]),
        knowledge_base_id=str(row[2]),
        data_source_id=str(row[3]),
        document_id=str(row[4]),
        title=str(row[5]),
        media_type=str(row[6]),
        content=str(row[7]),
        updated_at=row[8],
    )


def _data_source_from_row(row: Sequence[object]) -> KnowledgeDataSource:
    return KnowledgeDataSource(
        tenant_id=str(row[0]),
        app_id=str(row[1]),
        knowledge_base_id=str(row[2]),
        data_source_id=str(row[3]),
        source_type=str(row[4]),
        filename=str(row[5]),
        media_type=str(row[6]),
        status=str(row[7]),
        created_at=row[8],
    )


def _chunk_values(item: KnowledgeChunk) -> tuple[object, ...]:
    return (
        item.tenant_id,
        item.app_id,
        item.knowledge_base_id,
        item.knowledge_base_version,
        item.document_id,
        item.document_title,
        item.chunk_id,
        item.ordinal,
        item.content,
        item.page_number,
        item.paragraph_start,
        item.paragraph_end,
    )


def _chunk_from_row(row: Sequence[object]) -> KnowledgeChunk:
    return KnowledgeChunk(
        tenant_id=str(row[0]),
        app_id=str(row[1]),
        knowledge_base_id=str(row[2]),
        knowledge_base_version=int(row[3]),
        document_id=str(row[4]),
        document_title=str(row[5]),
        chunk_id=str(row[6]),
        ordinal=int(row[7]),
        content=str(row[8]),
        page_number=int(row[9]) if row[9] is not None else None,
        paragraph_start=int(row[10]) if row[10] is not None else None,
        paragraph_end=int(row[11]) if row[11] is not None else None,
    )


def _index_from_row(row: Sequence[object]) -> KnowledgeIndexStatus:
    return KnowledgeIndexStatus(
        tenant_id=str(row[0]),
        app_id=str(row[1]),
        knowledge_base_id=str(row[2]),
        knowledge_base_version=int(row[3]),
        status=str(row[4]),
        chunk_count=int(row[5]),
        error_message=str(row[6]),
        updated_at=row[7],
    )


def _audit_from_row(row: Sequence[object]) -> KnowledgeAuditEvent:
    return KnowledgeAuditEvent(
        tenant_id=str(row[0]),
        app_id=str(row[1]),
        knowledge_base_id=str(row[2]),
        action=str(row[3]),
        actor_id=str(row[4]),
        document_id=str(row[5]) if row[5] is not None else None,
        knowledge_base_version=int(row[6]) if row[6] is not None else None,
        detail=str(row[7]),
        occurred_at=row[8],
    )
