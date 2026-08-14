from __future__ import annotations

import pytest

from full_view_agent.application.knowledge_service import KnowledgeService
from full_view_agent.infrastructure.knowledge_repository import (
    InMemoryKeywordRetriever,
    InMemoryKnowledgeRepository,
    TextDocumentParser,
)


def _service() -> KnowledgeService:
    return KnowledgeService(
        repository=InMemoryKnowledgeRepository(),
        retriever=InMemoryKeywordRetriever(),
        parsers=[TextDocumentParser()],
        chunk_size=20,
        chunk_overlap=2,
    )


@pytest.mark.asyncio
async def test_import_build_access_policy_and_paragraph_citation() -> None:
    service = _service()
    await service.create_knowledge_base(
        tenant_id="tenant_a",
        app_id="app_a",
        knowledge_base_id="kb",
        name="制度库",
        actor_id="admin",
    )
    document = await service.import_document(
        tenant_id="tenant_a",
        app_id="app_a",
        knowledge_base_id="kb",
        data_source_id="upload_1",
        document_id="fire_rules",
        filename="消防制度.md",
        media_type="text/markdown",
        payload="# 消防制度\n\n每月开展消防检查。\n\n隐患必须限期整改。".encode(),
        actor_id="editor",
    )
    await service.set_access_policy(
        tenant_id="tenant_a",
        app_id="app_a",
        knowledge_base_id="kb",
        public_within_app=False,
        allowed_user_ids=[],
        allowed_roles=["safety_officer"],
        actor_id="admin",
    )
    publication = await service.publish(
        tenant_id="tenant_a",
        app_id="app_a",
        knowledge_base_id="kb",
        actor_id="publisher",
    )

    assert document.data_source_id == "upload_1"
    assert publication.version == 1
    index = await service.get_index_status(
        tenant_id="tenant_a", app_id="app_a", knowledge_base_id="kb"
    )
    assert index.status == "ready"
    assert await service.search(
        tenant_id="tenant_a",
        app_id="app_a",
        knowledge_base_ids=["kb"],
        query="消防检查",
        user_id="ordinary_user",
        roles=[],
    ) == []
    hits = await service.search(
        tenant_id="tenant_a",
        app_id="app_a",
        knowledge_base_ids=["kb"],
        query="消防检查",
        user_id="user_1",
        roles=["safety_officer"],
    )
    assert hits
    assert hits[0].citation.document_title == "消防制度.md"
    assert hits[0].citation.paragraph_start is not None
    assert hits[0].citation.paragraph_end is not None


@pytest.mark.asyncio
async def test_binding_disable_rebuild_delete_and_audit_lifecycle() -> None:
    service = _service()
    await service.create_knowledge_base(
        tenant_id="tenant_a",
        app_id="app_a",
        knowledge_base_id="kb",
        name="资料库",
        actor_id="admin",
    )
    await service.import_document(
        tenant_id="tenant_a",
        app_id="app_a",
        knowledge_base_id="kb",
        data_source_id="upload_1",
        document_id="doc",
        filename="doc.txt",
        media_type="text/plain",
        payload="可检索内容".encode(),
        actor_id="editor",
    )
    await service.publish(
        tenant_id="tenant_a",
        app_id="app_a",
        knowledge_base_id="kb",
        actor_id="publisher",
    )
    await service.set_application_binding(
        tenant_id="tenant_a",
        app_id="app_a",
        knowledge_base_id="kb",
        enabled=False,
        actor_id="admin",
    )
    assert await service.search(
        tenant_id="tenant_a",
        app_id="app_a",
        knowledge_base_ids=["kb"],
        query="检索",
    ) == []
    await service.set_application_binding(
        tenant_id="tenant_a",
        app_id="app_a",
        knowledge_base_id="kb",
        enabled=True,
        actor_id="admin",
    )
    rebuilt = await service.rebuild_index(
        tenant_id="tenant_a",
        app_id="app_a",
        knowledge_base_id="kb",
        actor_id="operator",
    )
    assert rebuilt.status == "ready"
    await service.delete_document(
        tenant_id="tenant_a",
        app_id="app_a",
        knowledge_base_id="kb",
        document_id="doc",
        actor_id="editor",
    )
    await service.publish(
        tenant_id="tenant_a",
        app_id="app_a",
        knowledge_base_id="kb",
        actor_id="publisher",
    )
    assert await service.search(
        tenant_id="tenant_a",
        app_id="app_a",
        knowledge_base_ids=["kb"],
        query="检索",
    ) == []
    await service.delete_knowledge_base(
        tenant_id="tenant_a",
        app_id="app_a",
        knowledge_base_id="kb",
        actor_id="admin",
    )
    events = await service.list_audit_events(
        tenant_id="tenant_a", app_id="app_a", knowledge_base_id="kb"
    )
    assert {event.action for event in events} >= {
        "knowledge_base.created",
        "document.imported",
        "knowledge_base.published",
        "application_binding.disabled",
        "index.rebuilt",
        "document.deleted",
        "knowledge_base.deleted",
    }


def test_text_parser_rejects_unsupported_or_invalid_payload() -> None:
    parser = TextDocumentParser()
    with pytest.raises(ValueError, match="unsupported"):
        parser.parse(filename="doc.pdf", media_type="application/pdf", payload=b"pdf")
    with pytest.raises(ValueError, match="UTF-8"):
        parser.parse(filename="doc.txt", media_type="text/plain", payload=b"\xff")


@pytest.mark.asyncio
async def test_data_source_list_and_delete_removes_its_draft_documents() -> None:
    service = _service()
    await service.create_knowledge_base(
        tenant_id="tenant_a", app_id="app_a", knowledge_base_id="kb", name="资料库"
    )
    await service.import_document(
        tenant_id="tenant_a",
        app_id="app_a",
        knowledge_base_id="kb",
        data_source_id="upload_1",
        document_id="doc",
        filename="doc.txt",
        media_type="text/plain",
        payload="待删除内容".encode(),
    )

    sources = await service.list_data_sources(
        tenant_id="tenant_a", app_id="app_a", knowledge_base_id="kb"
    )
    assert [source.data_source_id for source in sources] == ["upload_1"]
    await service.delete_data_source(
        tenant_id="tenant_a",
        app_id="app_a",
        knowledge_base_id="kb",
        data_source_id="upload_1",
        actor_id="editor",
    )
    assert await service.list_data_sources(
        tenant_id="tenant_a", app_id="app_a", knowledge_base_id="kb"
    ) == []
    assert await service.list_documents(
        tenant_id="tenant_a", app_id="app_a", knowledge_base_id="kb"
    ) == []


@pytest.mark.asyncio
async def test_missing_data_source_delete_does_not_remove_documents() -> None:
    service = _service()
    await service.create_knowledge_base(
        tenant_id="tenant_a", app_id="app_a", knowledge_base_id="kb", name="资料库"
    )
    await service.put_document(
        tenant_id="tenant_a",
        app_id="app_a",
        knowledge_base_id="kb",
        data_source_id="not_registered",
        document_id="doc",
        title="doc.txt",
        media_type="text/plain",
        content="必须保留",
    )

    with pytest.raises(LookupError, match="data source"):
        await service.delete_data_source(
            tenant_id="tenant_a",
            app_id="app_a",
            knowledge_base_id="kb",
            data_source_id="not_registered",
        )
    documents = await service.list_documents(
        tenant_id="tenant_a", app_id="app_a", knowledge_base_id="kb"
    )
    assert [document.document_id for document in documents] == ["doc"]
