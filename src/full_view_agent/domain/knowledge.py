"""Domain contracts for application-scoped, versioned knowledge retrieval."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Literal

from pydantic import Field, field_validator

from full_view_agent.domain.contract_model import ContractModel

_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


def _validate_identifier(value: str) -> str:
    if not _IDENTIFIER_RE.fullmatch(value):
        raise ValueError("must be a controlled identifier")
    return value


class KnowledgeBase(ContractModel):
    tenant_id: str = Field(min_length=1, max_length=128)
    app_id: str = Field(min_length=1, max_length=128)
    knowledge_base_id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)
    published_version: int | None = Field(default=None, ge=1)
    active: bool = True
    application_binding_enabled: bool = True
    public_within_app: bool = True
    allowed_user_ids: list[str] = Field(default_factory=list)
    allowed_roles: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("tenant_id", "app_id", "knowledge_base_id")
    @classmethod
    def validate_identifiers(cls, value: str) -> str:
        return _validate_identifier(value)


class KnowledgeDocument(ContractModel):
    tenant_id: str = Field(min_length=1, max_length=128)
    app_id: str = Field(min_length=1, max_length=128)
    knowledge_base_id: str = Field(min_length=1, max_length=128)
    data_source_id: str = Field(default="inline", min_length=1, max_length=128)
    document_id: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=500)
    media_type: Literal["text/plain", "text/markdown"]
    content: str = Field(min_length=1, max_length=5_000_000)
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator(
        "tenant_id", "app_id", "knowledge_base_id", "data_source_id", "document_id"
    )
    @classmethod
    def validate_identifiers(cls, value: str) -> str:
        return _validate_identifier(value)

    @field_validator("content")
    @classmethod
    def validate_content(cls, value: str) -> str:
        normalized = value.replace("\r\n", "\n").replace("\r", "\n").strip()
        if not normalized:
            raise ValueError("content must not be blank")
        return normalized


class KnowledgePublication(ContractModel):
    tenant_id: str
    app_id: str
    knowledge_base_id: str
    version: int = Field(ge=1)
    document_count: int = Field(ge=0)
    chunk_count: int = Field(ge=0)
    published_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class KnowledgeChunk(ContractModel):
    tenant_id: str
    app_id: str
    knowledge_base_id: str
    knowledge_base_version: int = Field(ge=1)
    document_id: str
    document_title: str
    chunk_id: str
    ordinal: int = Field(ge=0)
    content: str = Field(min_length=1)
    page_number: int | None = Field(default=None, ge=1)
    paragraph_start: int | None = Field(default=None, ge=1)
    paragraph_end: int | None = Field(default=None, ge=1)


class KnowledgeCitation(ContractModel):
    knowledge_base_id: str
    knowledge_base_version: int = Field(ge=1)
    document_id: str
    document_title: str
    chunk_id: str
    chunk_ordinal: int = Field(ge=0)
    page_number: int | None = Field(default=None, ge=1)
    paragraph_start: int | None = Field(default=None, ge=1)
    paragraph_end: int | None = Field(default=None, ge=1)


class KnowledgeSearchHit(ContractModel):
    content: str
    score: float = Field(gt=0)
    citation: KnowledgeCitation


class KnowledgeSearchInput(ContractModel):
    query: str = Field(min_length=1, max_length=4_000)
    limit: int = Field(default=5, ge=1, le=20)


class KnowledgeDataSource(ContractModel):
    tenant_id: str
    app_id: str
    knowledge_base_id: str
    data_source_id: str
    source_type: Literal["upload", "inline"]
    filename: str = Field(min_length=1, max_length=500)
    media_type: Literal["text/plain", "text/markdown"]
    status: Literal["ready", "deleted"] = "ready"
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class KnowledgeIndexStatus(ContractModel):
    tenant_id: str
    app_id: str
    knowledge_base_id: str
    knowledge_base_version: int = Field(ge=1)
    status: Literal["pending", "building", "ready", "failed", "deleted"]
    chunk_count: int = Field(default=0, ge=0)
    error_message: str = Field(default="", max_length=2000)
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class KnowledgeAuditEvent(ContractModel):
    tenant_id: str
    app_id: str
    knowledge_base_id: str
    action: str = Field(min_length=1, max_length=100)
    actor_id: str = Field(min_length=1, max_length=128)
    document_id: str | None = Field(default=None, max_length=128)
    knowledge_base_version: int | None = Field(default=None, ge=1)
    detail: str = Field(default="", max_length=2000)
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
