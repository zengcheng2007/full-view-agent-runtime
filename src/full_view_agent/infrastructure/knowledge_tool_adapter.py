from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel, JsonValue

from full_view_agent.application.capability_service import ToolAdapter
from full_view_agent.application.fingerprints import canonical_fingerprint
from full_view_agent.application.knowledge_service import KnowledgeService
from full_view_agent.application.run_capability_snapshot import RunCapabilitySnapshot
from full_view_agent.application.session_run_service import new_id
from full_view_agent.domain.knowledge import KnowledgeSearchInput
from full_view_agent.domain.models import (
    AuthContext,
    DataResult,
    DynamicTableData,
    InternalToolManifest,
    PolicyDecision,
    ResultDisplayField,
    ResultPresentation,
    ResultVisualization,
    TableDataResult,
)


class RunKnowledgeSnapshotReader(Protocol):
    def get_snapshot_for_run(self, run_id: str) -> RunCapabilitySnapshot | None: ...


class KnowledgeAwareToolAdapter:
    """Route the built-in knowledge Tool without weakening governance adapters."""

    def __init__(
        self,
        *,
        inner: ToolAdapter,
        knowledge_service: KnowledgeService,
        snapshot_reader: RunKnowledgeSnapshotReader | None = None,
    ) -> None:
        self._inner = inner
        self._knowledge_service = knowledge_service
        self._snapshot_reader = snapshot_reader

    async def execute(
        self,
        *,
        manifest: InternalToolManifest,
        arguments: BaseModel,
        policy_decision: PolicyDecision,
        auth_context: AuthContext,
    ) -> DataResult:
        if manifest.tool_id != "knowledge.search":
            return await self._inner.execute(
                manifest=manifest,
                arguments=arguments,
                policy_decision=policy_decision,
                auth_context=auth_context,
            )
        if not isinstance(arguments, KnowledgeSearchInput):
            raise TypeError("knowledge.search requires KnowledgeSearchInput")
        run_snapshot = (
            self._snapshot_reader.get_snapshot_for_run(auth_context.run_id)
            if self._snapshot_reader is not None
            else None
        )
        if run_snapshot is None or not run_snapshot.agent_scoped:
            bases = await self._knowledge_service.list_knowledge_bases(
                tenant_id=auth_context.principal.tenant_id,
                app_id=auth_context.application.app_id,
            )
            knowledge_base_ids = [item.knowledge_base_id for item in bases]
            version_constraints = None
        else:
            version_constraints = dict(run_snapshot.knowledge_base_versions)
            knowledge_base_ids = list(version_constraints)
        hits = await self._knowledge_service.search(
            tenant_id=auth_context.principal.tenant_id,
            app_id=auth_context.application.app_id,
            knowledge_base_ids=knowledge_base_ids,
            query=arguments.query,
            limit=arguments.limit,
            user_id=auth_context.principal.user_id,
            roles=auth_context.principal.roles,
            version_constraints=version_constraints,
        )
        rows: list[dict[str, JsonValue]] = [
            {
                "content": hit.content,
                "score": hit.score,
                "knowledge_base_id": hit.citation.knowledge_base_id,
                "knowledge_base_version": hit.citation.knowledge_base_version,
                "document_id": hit.citation.document_id,
                "document_title": hit.citation.document_title,
                "chunk_id": hit.citation.chunk_id,
                "chunk_ordinal": hit.citation.chunk_ordinal,
                "page_number": hit.citation.page_number,
                "paragraph_start": hit.citation.paragraph_start,
                "paragraph_end": hit.citation.paragraph_end,
            }
            for hit in hits
        ]
        fingerprint = canonical_fingerprint(
            domain="knowledge-search-result:1.0", value={"rows": rows}
        )
        return TableDataResult(
            result_id=new_id("res"),
            data_schema_ref="schema://data/table-data-result/1.0.0",
            result_fingerprint=fingerprint,
            data=DynamicTableData(rows=rows),
            row_count=len(rows),
            presentation=ResultPresentation(
                title="知识库检索结果",
                summary=f"已从当前应用授权知识库召回 {len(rows)} 个可引用片段。",
                status_label="检索完成",
                fields=[
                    ResultDisplayField(field="document_title", label="文档", role="dimension"),
                    ResultDisplayField(field="content", label="引用内容", role="dimension"),
                    ResultDisplayField(field="score", label="匹配度", role="metric"),
                    ResultDisplayField(
                        field="paragraph_start",
                        label="起始段落",
                        role="identifier",
                    ),
                ],
                visualizations=[
                    ResultVisualization(kind="table", title="引用片段")
                ],
            ),
        )
