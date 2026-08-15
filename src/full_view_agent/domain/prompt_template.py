from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import Field, field_validator

from full_view_agent.domain.contract_model import ContractModel

PromptTemplateStatus = Literal[
    "draft", "testing", "pending_approval", "published", "disabled"
]
PromptLayer = Literal["application", "agent"]


class PromptTemplate(ContractModel):
    """Versioned operator guidance appended after the immutable safety prompt."""

    prompt_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{2,127}$")
    app_id: str = Field(min_length=2, max_length=64)
    layer: PromptLayer = "application"
    name: str = Field(min_length=1, max_length=200)
    version: str = Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")
    content: str = Field(min_length=1, max_length=20_000)
    status: PromptTemplateStatus = "draft"
    etag: int = Field(default=1, ge=1)
    created_by: str = Field(min_length=1, max_length=128)
    updated_by: str = Field(min_length=1, max_length=128)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("content")
    @classmethod
    def content_must_not_be_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("content must not be blank")
        return value


class PromptLifecycleEvent(ContractModel):
    event_id: str
    prompt_id: str
    version: str
    from_status: PromptTemplateStatus | None = None
    to_status: PromptTemplateStatus
    actor: str
    reason: str = Field(min_length=1, max_length=2_000)
    changed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class RuntimePromptSnapshot(ContractModel):
    prompt_id: str
    app_id: str
    layer: PromptLayer = "application"
    version: str
    content: str

    @property
    def composite_version(self) -> str:
        return f"{self.prompt_id}@{self.version}"
