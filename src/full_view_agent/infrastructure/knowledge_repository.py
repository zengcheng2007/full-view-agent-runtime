"""In-memory knowledge persistence and keyword retrieval adapters."""

from __future__ import annotations

import asyncio
import math
import re
from collections import Counter
from collections.abc import Sequence

from full_view_agent.domain.knowledge import (
    KnowledgeAuditEvent,
    KnowledgeBase,
    KnowledgeChunk,
    KnowledgeDataSource,
    KnowledgeDocument,
    KnowledgeIndexStatus,
    KnowledgePublication,
)

_WORD_RE = re.compile(r"[a-zA-Z0-9_:-]+|[\u3400-\u9fff]+")


class InMemoryKnowledgeRepository:
    """Process-local adapter preserving immutable published snapshots."""

    def __init__(self) -> None:
        self._bases: dict[tuple[str, str, str], KnowledgeBase] = {}
        self._documents: dict[tuple[str, str, str, str], KnowledgeDocument] = {}
        self._data_sources: dict[tuple[str, str, str, str], KnowledgeDataSource] = {}
        self._publications: dict[tuple[str, str, str, int], KnowledgePublication] = {}
        self._chunks: dict[tuple[str, str, str, int], tuple[KnowledgeChunk, ...]] = {}
        self._indexes: dict[tuple[str, str, str], KnowledgeIndexStatus] = {}
        self._audit_events: list[KnowledgeAuditEvent] = []
        self._lock = asyncio.Lock()

    async def create_base(self, knowledge_base: KnowledgeBase) -> KnowledgeBase:
        key = _base_key(
            knowledge_base.tenant_id,
            knowledge_base.app_id,
            knowledge_base.knowledge_base_id,
        )
        async with self._lock:
            if key in self._bases:
                raise ValueError("knowledge base already exists in application scope")
            self._bases[key] = knowledge_base
        return knowledge_base.model_copy(deep=True)

    async def get_base(
        self, *, tenant_id: str, app_id: str, knowledge_base_id: str
    ) -> KnowledgeBase | None:
        item = self._bases.get(_base_key(tenant_id, app_id, knowledge_base_id))
        return item.model_copy(deep=True) if item is not None else None

    async def list_bases(self, *, tenant_id: str, app_id: str) -> list[KnowledgeBase]:
        return [
            item.model_copy(deep=True)
            for key, item in sorted(self._bases.items())
            if key[:2] == (tenant_id, app_id) and item.active
        ]

    async def update_base(self, knowledge_base: KnowledgeBase) -> KnowledgeBase:
        key = _base_key(
            knowledge_base.tenant_id,
            knowledge_base.app_id,
            knowledge_base.knowledge_base_id,
        )
        async with self._lock:
            if key not in self._bases:
                raise LookupError("knowledge base not found in application scope")
            self._bases[key] = knowledge_base.model_copy(deep=True)
        return knowledge_base.model_copy(deep=True)

    async def put_document(self, document: KnowledgeDocument) -> KnowledgeDocument:
        base_key = _base_key(
            document.tenant_id, document.app_id, document.knowledge_base_id
        )
        if base_key not in self._bases:
            raise LookupError("knowledge base not found in application scope")
        key = (*base_key, document.document_id)
        async with self._lock:
            self._documents[key] = document
        return document.model_copy(deep=True)

    async def delete_document(
        self,
        *,
        tenant_id: str,
        app_id: str,
        knowledge_base_id: str,
        document_id: str,
    ) -> bool:
        key = (*_base_key(tenant_id, app_id, knowledge_base_id), document_id)
        async with self._lock:
            return self._documents.pop(key, None) is not None

    async def save_data_source(self, data_source: KnowledgeDataSource) -> KnowledgeDataSource:
        key = (
            data_source.tenant_id,
            data_source.app_id,
            data_source.knowledge_base_id,
            data_source.data_source_id,
        )
        async with self._lock:
            self._data_sources[key] = data_source.model_copy(deep=True)
        return data_source.model_copy(deep=True)

    async def list_data_sources(
        self, *, tenant_id: str, app_id: str, knowledge_base_id: str
    ) -> list[KnowledgeDataSource]:
        prefix = _base_key(tenant_id, app_id, knowledge_base_id)
        return [
            item.model_copy(deep=True)
            for key, item in sorted(self._data_sources.items())
            if key[:3] == prefix and item.status == "ready"
        ]

    async def delete_data_source(
        self,
        *,
        tenant_id: str,
        app_id: str,
        knowledge_base_id: str,
        data_source_id: str,
    ) -> bool:
        key = (*_base_key(tenant_id, app_id, knowledge_base_id), data_source_id)
        async with self._lock:
            existing = self._data_sources.get(key)
            if existing is None or existing.status == "deleted":
                return False
            self._data_sources[key] = existing.model_copy(update={"status": "deleted"})
        return True

    async def list_documents(
        self, *, tenant_id: str, app_id: str, knowledge_base_id: str
    ) -> list[KnowledgeDocument]:
        prefix = _base_key(tenant_id, app_id, knowledge_base_id)
        return [
            item.model_copy(deep=True)
            for key, item in sorted(self._documents.items())
            if key[:3] == prefix
        ]

    async def publish(
        self,
        *,
        knowledge_base: KnowledgeBase,
        publication: KnowledgePublication,
        chunks: Sequence[KnowledgeChunk],
    ) -> KnowledgePublication:
        base_key = _base_key(
            knowledge_base.tenant_id,
            knowledge_base.app_id,
            knowledge_base.knowledge_base_id,
        )
        publication_key = (*base_key, publication.version)
        async with self._lock:
            current = self._bases.get(base_key)
            expected_previous = publication.version - 1 or None
            if current is None or current.published_version != expected_previous:
                raise ValueError("knowledge publication version conflict")
            if publication_key in self._publications:
                raise ValueError("knowledge publication version already exists")
            self._chunks[publication_key] = tuple(item.model_copy(deep=True) for item in chunks)
            self._publications[publication_key] = publication.model_copy(deep=True)
            self._bases[base_key] = knowledge_base.model_copy(deep=True)
        return publication.model_copy(deep=True)

    async def list_chunks(
        self,
        *,
        tenant_id: str,
        app_id: str,
        knowledge_base_id: str,
        version: int,
    ) -> list[KnowledgeChunk]:
        items = self._chunks.get((*_base_key(tenant_id, app_id, knowledge_base_id), version), ())
        return [item.model_copy(deep=True) for item in items]

    async def save_index_status(self, status: KnowledgeIndexStatus) -> KnowledgeIndexStatus:
        key = _base_key(status.tenant_id, status.app_id, status.knowledge_base_id)
        async with self._lock:
            self._indexes[key] = status.model_copy(deep=True)
        return status.model_copy(deep=True)

    async def get_index_status(
        self, *, tenant_id: str, app_id: str, knowledge_base_id: str
    ) -> KnowledgeIndexStatus | None:
        status = self._indexes.get(_base_key(tenant_id, app_id, knowledge_base_id))
        return status.model_copy(deep=True) if status is not None else None

    async def append_audit_event(self, event: KnowledgeAuditEvent) -> None:
        async with self._lock:
            self._audit_events.append(event.model_copy(deep=True))

    async def list_audit_events(
        self, *, tenant_id: str, app_id: str, knowledge_base_id: str
    ) -> list[KnowledgeAuditEvent]:
        return [
            event.model_copy(deep=True)
            for event in self._audit_events
            if event.tenant_id == tenant_id
            and event.app_id == app_id
            and event.knowledge_base_id == knowledge_base_id
        ]


class InMemoryKeywordRetriever:
    """Deterministic BM25-style keyword ranking; this is not vector search."""

    def __init__(self) -> None:
        self._built_indexes: set[tuple[str, str, str, int]] = set()

    async def build(
        self, *, index: KnowledgeIndexStatus, chunks: Sequence[KnowledgeChunk]
    ) -> None:
        expected_scope = (
            index.tenant_id,
            index.app_id,
            index.knowledge_base_id,
            index.knowledge_base_version,
        )
        if any(
            (
                chunk.tenant_id,
                chunk.app_id,
                chunk.knowledge_base_id,
                chunk.knowledge_base_version,
            )
            != expected_scope
            for chunk in chunks
        ):
            raise ValueError("cannot build keyword index from out-of-scope chunks")
        self._built_indexes.add(expected_scope)

    async def delete(self, *, index: KnowledgeIndexStatus) -> None:
        self._built_indexes.discard(
            (
                index.tenant_id,
                index.app_id,
                index.knowledge_base_id,
                index.knowledge_base_version,
            )
        )

    def rank(
        self, *, query: str, chunks: Sequence[KnowledgeChunk], limit: int
    ) -> list[tuple[KnowledgeChunk, float]]:
        query_terms = _terms(query)
        if not query_terms or not chunks:
            return []
        document_terms = [_terms(chunk.content) for chunk in chunks]
        average_length = sum(map(len, document_terms)) / len(document_terms) or 1.0
        frequencies = Counter(
            term for term in set(query_terms) for terms in document_terms if term in terms
        )
        scored: list[tuple[KnowledgeChunk, float]] = []
        for chunk, terms in zip(chunks, document_terms, strict=True):
            counts = Counter(terms)
            score = 0.0
            for term in query_terms:
                frequency = counts[term]
                if not frequency:
                    continue
                inverse_document_frequency = math.log(
                    1 + (len(chunks) - frequencies[term] + 0.5) / (frequencies[term] + 0.5)
                )
                denominator = frequency + 1.2 * (
                    0.25 + 0.75 * len(terms) / average_length
                )
                score += inverse_document_frequency * frequency * 2.2 / denominator
            if query.casefold() in chunk.content.casefold():
                score += 1.0
            if score > 0:
                scored.append((chunk, score))
        scored.sort(
            key=lambda item: (
                -item[1],
                item[0].knowledge_base_id,
                item[0].document_id,
                item[0].ordinal,
            )
        )
        return scored[:limit]


class TextDocumentParser:
    """Strict UTF-8 parser for the first supported text formats."""

    _MEDIA_TYPES = frozenset({"text/plain", "text/markdown"})
    _EXTENSIONS = frozenset({".txt", ".md", ".markdown"})

    def supports(self, *, filename: str, media_type: str) -> bool:
        lowered = filename.casefold()
        return media_type in self._MEDIA_TYPES and any(
            lowered.endswith(extension) for extension in self._EXTENSIONS
        )

    def parse(self, *, filename: str, media_type: str, payload: bytes) -> str:
        if not self.supports(filename=filename, media_type=media_type):
            raise ValueError("unsupported text document format")
        try:
            content = payload.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ValueError("text document must be valid UTF-8") from exc
        normalized = content.replace("\r\n", "\n").replace("\r", "\n").strip()
        if not normalized:
            raise ValueError("text document content must not be blank")
        return normalized


def _base_key(tenant_id: str, app_id: str, knowledge_base_id: str) -> tuple[str, str, str]:
    return tenant_id, app_id, knowledge_base_id


def _terms(value: str) -> list[str]:
    terms: list[str] = []
    for token in _WORD_RE.findall(value.casefold()):
        if token and "\u3400" <= token[0] <= "\u9fff":
            terms.extend(token[index : index + 2] for index in range(max(1, len(token) - 1)))
        else:
            terms.append(token)
    return terms
