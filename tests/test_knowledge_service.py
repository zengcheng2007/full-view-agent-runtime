from __future__ import annotations

from types import SimpleNamespace

import pytest

from full_view_agent.application.knowledge_service import (
    KnowledgeSearchRejected,
    KnowledgeService,
)
from full_view_agent.domain.knowledge import KnowledgeChunk
from full_view_agent.infrastructure.knowledge_repository import (
    InMemoryKeywordRetriever,
    InMemoryKnowledgeRepository,
)
from full_view_agent.infrastructure.knowledge_tool_adapter import (
    KnowledgeAwareToolAdapter,
)


def _service(
    *,
    repository: InMemoryKnowledgeRepository | None = None,
    retriever: InMemoryKeywordRetriever | None = None,
) -> KnowledgeService:
    return KnowledgeService(
        repository=repository or InMemoryKnowledgeRepository(),
        retriever=retriever or InMemoryKeywordRetriever(),
        chunk_size=12,
        chunk_overlap=2,
    )


@pytest.mark.asyncio
async def test_published_text_document_is_searchable_with_traceable_citation() -> None:
    service = _service()
    await service.create_knowledge_base(
        tenant_id="tenant_a",
        app_id="full_information_view",
        knowledge_base_id="policy_docs",
        name="政策资料",
    )
    await service.put_document(
        tenant_id="tenant_a",
        app_id="full_information_view",
        knowledge_base_id="policy_docs",
        document_id="doc_1",
        title="消防检查.md",
        media_type="text/markdown",
        content="消防检查每月开展。\n发现隐患后限期整改。",
    )

    publication = await service.publish(
        tenant_id="tenant_a",
        app_id="full_information_view",
        knowledge_base_id="policy_docs",
    )
    hits = await service.search(
        tenant_id="tenant_a",
        app_id="full_information_view",
        knowledge_base_ids=["policy_docs"],
        query="消防检查",
        limit=3,
    )

    assert publication.version == 1
    assert hits
    assert "消防检查" in hits[0].content
    assert hits[0].citation.document_id == "doc_1"
    assert hits[0].citation.chunk_id == "doc_1:1:0"
    assert hits[0].citation.knowledge_base_version == 1


@pytest.mark.asyncio
async def test_published_knowledge_is_executable_through_the_registered_tool() -> None:
    from full_view_agent.application.capability_service import CapabilityService
    from full_view_agent.application.policy import MinimalPolicyAdapter
    from full_view_agent.application.tool_registry import ToolRegistry

    from .test_policy import population_auth_context

    class UnexpectedInnerAdapter:
        async def execute(self, **_kwargs):
            raise AssertionError("knowledge search must not call governance HTTP")

    service = _service()
    await service.create_knowledge_base(
        tenant_id="tenant-hz",
        app_id="full_information_view",
        knowledge_base_id="policy_docs",
        name="政策资料",
    )
    await service.put_document(
        tenant_id="tenant-hz",
        app_id="full_information_view",
        knowledge_base_id="policy_docs",
        document_id="doc_1",
        title="养老政策.md",
        media_type="text/markdown",
        content="养老服务补贴由街道受理。",
    )
    await service.publish(
        tenant_id="tenant-hz",
        app_id="full_information_view",
        knowledge_base_id="policy_docs",
    )
    auth = population_auth_context().model_copy(
        update={
            "entitlements": ["knowledge.search"],
            "data_scopes": population_auth_context().data_scopes.model_copy(
                update={"datasets": ["knowledge"]}
            ),
        }
    )
    capability = CapabilityService(
        registry=ToolRegistry.default(),
        policy=MinimalPolicyAdapter(),
        adapter=KnowledgeAwareToolAdapter(
            inner=UnexpectedInnerAdapter(), knowledge_service=service
        ),
    )

    result = await capability.execute(
        tool_call_id="tcl-knowledge-01",
        tool_id="knowledge.search",
        raw_arguments={"query": "养老服务补贴", "limit": 5},
        auth_context=auth,
    )

    assert result.status == "success"
    assert result.data_result is not None
    assert result.data_result.data.rows[0]["document_title"] == "养老政策.md"
    assert result.data_result.data.rows[0]["paragraph_start"] == 1


@pytest.mark.asyncio
async def test_agent_run_knowledge_allowlist_prevents_cross_agent_search() -> None:
    from full_view_agent.application.capability_service import CapabilityService
    from full_view_agent.application.policy import MinimalPolicyAdapter
    from full_view_agent.application.tool_registry import ToolRegistry

    from .test_policy import population_auth_context

    class UnexpectedInnerAdapter:
        async def execute(self, **_kwargs):
            raise AssertionError("knowledge search must not call governance HTTP")

    service = _service()
    for suffix in ("alpha", "beta"):
        await service.create_knowledge_base(
            tenant_id="tenant-hz",
            app_id="full_information_view",
            knowledge_base_id=f"kb_{suffix}",
            name=f"KB {suffix}",
        )
        await service.put_document(
            tenant_id="tenant-hz",
            app_id="full_information_view",
            knowledge_base_id=f"kb_{suffix}",
            document_id=f"doc_{suffix}",
            title=f"{suffix}.txt",
            media_type="text/plain",
            content=f"shared policy marker {suffix}",
        )
        await service.publish(
            tenant_id="tenant-hz",
            app_id="full_information_view",
            knowledge_base_id=f"kb_{suffix}",
        )

    class SnapshotReader:
        def get_snapshot_for_run(self, run_id: str):
            suffix = "alpha" if run_id == "run-alpha" else "beta"
            return SimpleNamespace(
                knowledge_base_versions={f"kb_{suffix}": 1},
                agent_scoped=True,
            )

    capability = CapabilityService(
        registry=ToolRegistry.default(),
        policy=MinimalPolicyAdapter(),
        adapter=KnowledgeAwareToolAdapter(
            inner=UnexpectedInnerAdapter(),
            knowledge_service=service,
            snapshot_reader=SnapshotReader(),
        ),
    )
    base_auth = population_auth_context().model_copy(
        update={
            "entitlements": ["knowledge.search"],
            "data_scopes": population_auth_context().data_scopes.model_copy(
                update={"datasets": ["knowledge"]}
            ),
        }
    )
    for suffix in ("alpha", "beta"):
        result = await capability.execute(
            tool_call_id=f"tcl-{suffix}",
            tool_id="knowledge.search",
            raw_arguments={"query": "shared policy marker", "limit": 5},
            auth_context=base_auth.model_copy(update={"run_id": f"run-{suffix}"}),
        )
        assert result.data_result is not None
        assert {row["knowledge_base_id"] for row in result.data_result.data.rows} == {
            f"kb_{suffix}"
        }


@pytest.mark.asyncio
async def test_unpublished_and_new_draft_content_is_not_searchable() -> None:
    service = _service()
    await service.create_knowledge_base(
        tenant_id="tenant_a",
        app_id="app_a",
        knowledge_base_id="kb_a",
        name="资料",
    )
    await service.put_document(
        tenant_id="tenant_a",
        app_id="app_a",
        knowledge_base_id="kb_a",
        document_id="doc_a",
        title="v1.txt",
        media_type="text/plain",
        content="第一版包含苹果资料",
    )
    assert await service.search(
        tenant_id="tenant_a",
        app_id="app_a",
        knowledge_base_ids=["kb_a"],
        query="苹果",
    ) == []

    await service.publish(
        tenant_id="tenant_a", app_id="app_a", knowledge_base_id="kb_a"
    )
    await service.put_document(
        tenant_id="tenant_a",
        app_id="app_a",
        knowledge_base_id="kb_a",
        document_id="doc_a",
        title="v2.txt",
        media_type="text/plain",
        content="第二版新增香蕉资料",
    )

    assert await service.search(
        tenant_id="tenant_a",
        app_id="app_a",
        knowledge_base_ids=["kb_a"],
        query="香蕉",
    ) == []
    old_hits = await service.search(
        tenant_id="tenant_a",
        app_id="app_a",
        knowledge_base_ids=["kb_a"],
        query="苹果",
    )
    assert old_hits[0].citation.knowledge_base_version == 1


@pytest.mark.asyncio
async def test_search_is_isolated_by_both_tenant_and_application() -> None:
    service = _service()
    for tenant_id, app_id, secret in (
        ("tenant_a", "app_a", "alpha-secret"),
        ("tenant_a", "app_b", "bravo-secret"),
        ("tenant_b", "app_a", "charlie-secret"),
    ):
        await service.create_knowledge_base(
            tenant_id=tenant_id,
            app_id=app_id,
            knowledge_base_id="shared_name",
            name="隔离资料",
        )
        await service.put_document(
            tenant_id=tenant_id,
            app_id=app_id,
            knowledge_base_id="shared_name",
            document_id="doc",
            title="secret.txt",
            media_type="text/plain",
            content=secret,
        )
        await service.publish(
            tenant_id=tenant_id,
            app_id=app_id,
            knowledge_base_id="shared_name",
        )

    assert await service.search(
        tenant_id="tenant_a",
        app_id="app_a",
        knowledge_base_ids=["shared_name"],
        query="bravo-secret",
    ) == []
    own_hits = await service.search(
        tenant_id="tenant_a",
        app_id="app_a",
        knowledge_base_ids=["shared_name"],
        query="alpha-secret",
    )
    assert [hit.content for hit in own_hits] == ["alpha-secret"]


@pytest.mark.asyncio
async def test_republication_creates_new_immutable_version() -> None:
    repository = InMemoryKnowledgeRepository()
    service = _service(repository=repository)
    await service.create_knowledge_base(
        tenant_id="tenant_a", app_id="app_a", knowledge_base_id="kb", name="资料"
    )
    await service.put_document(
        tenant_id="tenant_a",
        app_id="app_a",
        knowledge_base_id="kb",
        document_id="doc",
        title="doc.txt",
        media_type="text/plain",
        content="旧版本词条",
    )
    first = await service.publish(
        tenant_id="tenant_a", app_id="app_a", knowledge_base_id="kb"
    )
    await service.put_document(
        tenant_id="tenant_a",
        app_id="app_a",
        knowledge_base_id="kb",
        document_id="doc",
        title="doc.txt",
        media_type="text/plain",
        content="新版本词条",
    )
    second = await service.publish(
        tenant_id="tenant_a", app_id="app_a", knowledge_base_id="kb"
    )

    old_chunks = await repository.list_chunks(
        tenant_id="tenant_a",
        app_id="app_a",
        knowledge_base_id="kb",
        version=1,
    )
    assert first.version == 1
    assert second.version == 2
    assert old_chunks[0].content == "旧版本词条"
    new_hits = await service.search(
        tenant_id="tenant_a",
        app_id="app_a",
        knowledge_base_ids=["kb"],
        query="新版本",
    )
    assert new_hits[0].citation.knowledge_base_version == 2


@pytest.mark.asyncio
async def test_retriever_failure_is_reported_fail_closed() -> None:
    class BrokenRetriever(InMemoryKeywordRetriever):
        def rank(self, *, query, chunks, limit):  # type: ignore[no-untyped-def]
            raise RuntimeError("index unavailable")

    service = _service(retriever=BrokenRetriever())
    await service.create_knowledge_base(
        tenant_id="tenant_a", app_id="app_a", knowledge_base_id="kb", name="资料"
    )
    await service.put_document(
        tenant_id="tenant_a",
        app_id="app_a",
        knowledge_base_id="kb",
        document_id="doc",
        title="doc.txt",
        media_type="text/plain",
        content="不可返回的敏感内容",
    )
    await service.publish(
        tenant_id="tenant_a", app_id="app_a", knowledge_base_id="kb"
    )

    with pytest.raises(KnowledgeSearchRejected, match="RETRIEVAL_FAILED"):
        await service.search(
            tenant_id="tenant_a",
            app_id="app_a",
            knowledge_base_ids=["kb"],
            query="敏感",
        )


@pytest.mark.asyncio
async def test_repository_scope_contamination_is_rejected_fail_closed() -> None:
    class ContaminatedRepository(InMemoryKnowledgeRepository):
        async def list_chunks(self, **kwargs):  # type: ignore[no-untyped-def]
            chunks = await super().list_chunks(**kwargs)
            return chunks + [
                KnowledgeChunk(
                    tenant_id="tenant_b",
                    app_id="app_a",
                    knowledge_base_id="kb",
                    knowledge_base_version=1,
                    document_id="leaked",
                    document_title="leaked.txt",
                    chunk_id="leaked:1:0",
                    ordinal=0,
                    content="跨租户敏感信息",
                )
            ]

    service = _service(repository=ContaminatedRepository())
    await service.create_knowledge_base(
        tenant_id="tenant_a", app_id="app_a", knowledge_base_id="kb", name="资料"
    )
    await service.put_document(
        tenant_id="tenant_a",
        app_id="app_a",
        knowledge_base_id="kb",
        document_id="doc",
        title="doc.txt",
        media_type="text/plain",
        content="正常信息",
    )
    await service.publish(
        tenant_id="tenant_a", app_id="app_a", knowledge_base_id="kb"
    )

    with pytest.raises(KnowledgeSearchRejected, match="RETRIEVAL_FAILED"):
        await service.search(
            tenant_id="tenant_a",
            app_id="app_a",
            knowledge_base_ids=["kb"],
            query="敏感",
        )


@pytest.mark.asyncio
async def test_retriever_cannot_inject_a_chunk_outside_repository_results() -> None:
    class InjectingRetriever(InMemoryKeywordRetriever):
        def rank(self, *, query, chunks, limit):  # type: ignore[no-untyped-def]
            return [
                (
                    KnowledgeChunk(
                        tenant_id="tenant_b",
                        app_id="app_a",
                        knowledge_base_id="kb",
                        knowledge_base_version=1,
                        document_id="leaked",
                        document_title="leaked.txt",
                        chunk_id="leaked:1:0",
                        ordinal=0,
                        content="索引注入敏感信息",
                    ),
                    99.0,
                )
            ]

    service = _service(retriever=InjectingRetriever())
    await service.create_knowledge_base(
        tenant_id="tenant_a", app_id="app_a", knowledge_base_id="kb", name="资料"
    )
    await service.put_document(
        tenant_id="tenant_a",
        app_id="app_a",
        knowledge_base_id="kb",
        document_id="doc",
        title="doc.txt",
        media_type="text/plain",
        content="正常信息",
    )
    await service.publish(
        tenant_id="tenant_a", app_id="app_a", knowledge_base_id="kb"
    )

    with pytest.raises(KnowledgeSearchRejected, match="RETRIEVAL_FAILED"):
        await service.search(
            tenant_id="tenant_a",
            app_id="app_a",
            knowledge_base_ids=["kb"],
            query="敏感",
        )


@pytest.mark.asyncio
async def test_document_contract_rejects_binary_and_empty_content() -> None:
    service = _service()
    await service.create_knowledge_base(
        tenant_id="tenant_a", app_id="app_a", knowledge_base_id="kb", name="资料"
    )

    with pytest.raises(ValueError, match="media_type"):
        await service.put_document(
            tenant_id="tenant_a",
            app_id="app_a",
            knowledge_base_id="kb",
            document_id="doc",
            title="doc.pdf",
            media_type="application/pdf",
            content="not really a pdf",
        )
    with pytest.raises(ValueError, match="content"):
        await service.put_document(
            tenant_id="tenant_a",
            app_id="app_a",
            knowledge_base_id="kb",
            document_id="doc",
            title="empty.txt",
            media_type="text/plain",
            content="   ",
        )
