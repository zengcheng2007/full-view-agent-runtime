import pytest

from full_view_agent.application.knowledge_service import KnowledgeService
from full_view_agent.infrastructure.knowledge_repository import (
    InMemoryKeywordRetriever,
    TextDocumentParser,
)
from full_view_agent.infrastructure.postgres_knowledge_repository import (
    PostgresKnowledgeRepository,
)


def test_postgres_knowledge_repository_exposes_full_lifecycle_port() -> None:
    repository = PostgresKnowledgeRepository(
        dsn="postgresql://unused", schema="full_view_agent"
    )

    for method_name in (
        "create_base",
        "get_base",
        "list_bases",
        "update_base",
        "save_data_source",
        "list_data_sources",
        "delete_data_source",
        "put_document",
        "delete_document",
        "list_documents",
        "publish",
        "list_chunks",
        "save_index_status",
        "get_index_status",
        "append_audit_event",
        "list_audit_events",
    ):
        assert callable(getattr(repository, method_name))


def test_postgres_knowledge_repository_rejects_unsafe_schema() -> None:
    with pytest.raises(ValueError, match="schema"):
        PostgresKnowledgeRepository(dsn="postgresql://unused", schema="bad;drop schema")


@pytest.mark.db
@pytest.mark.asyncio
async def test_postgres_repository_runs_publish_search_and_audit_lifecycle(pg_schema) -> None:
    repository = PostgresKnowledgeRepository(
        dsn=pg_schema["dsn"], schema=pg_schema["schema"]
    )
    service = KnowledgeService(
        repository=repository,
        retriever=InMemoryKeywordRetriever(),
        parsers=[TextDocumentParser()],
        chunk_size=30,
        chunk_overlap=3,
    )
    await service.create_knowledge_base(
        tenant_id="tenant_pg",
        app_id="app_pg",
        knowledge_base_id="kb_pg",
        name="PG资料库",
        actor_id="admin",
    )
    await service.import_document(
        tenant_id="tenant_pg",
        app_id="app_pg",
        knowledge_base_id="kb_pg",
        data_source_id="source_pg",
        document_id="doc_pg",
        filename="制度.md",
        media_type="text/markdown",
        payload="第一段。\n\nPG持久化检索内容。".encode(),
        actor_id="editor",
    )
    await service.publish(
        tenant_id="tenant_pg",
        app_id="app_pg",
        knowledge_base_id="kb_pg",
        actor_id="publisher",
    )

    hits = await service.search(
        tenant_id="tenant_pg",
        app_id="app_pg",
        knowledge_base_ids=["kb_pg"],
        query="持久化检索",
    )
    assert hits[0].citation.document_id == "doc_pg"
    assert (await service.get_index_status(
        tenant_id="tenant_pg", app_id="app_pg", knowledge_base_id="kb_pg"
    )).status == "ready"
    assert len(await service.list_audit_events(
        tenant_id="tenant_pg", app_id="app_pg", knowledge_base_id="kb_pg"
    )) >= 3
