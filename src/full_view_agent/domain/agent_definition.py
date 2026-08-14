"""Application-scoped Agent definitions and immutable release contracts."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Literal

from pydantic import Field, field_validator

from full_view_agent.domain.contract_model import ContractModel

_ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
_VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")


class AgentDefinition(ContractModel):
    app_id: str = Field(min_length=2, max_length=64)
    agent_id: str = Field(min_length=2, max_length=64)
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)
    status: Literal["active", "disabled"] = "active"
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    etag: int = Field(default=1, ge=1)

    @field_validator("app_id", "agent_id")
    @classmethod
    def validate_identifier(cls, value: str) -> str:
        if not _ID_RE.fullmatch(value):
            raise ValueError("identifier must be controlled lowercase snake_case")
        return value


class AgentVersion(ContractModel):
    app_id: str = Field(min_length=2, max_length=64)
    agent_id: str = Field(min_length=2, max_length=64)
    version: str = Field(min_length=5, max_length=32)
    status: Literal["draft", "published"] = "draft"
    prompt_ref: str | None = Field(default=None, max_length=200)
    capability_refs: tuple[str, ...] = ()
    skill_refs: tuple[str, ...] = ()
    workflow_refs: tuple[str, ...] = ()
    knowledge_base_refs: tuple[str, ...] = ()
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    etag: int = Field(default=1, ge=1)

    @field_validator("version")
    @classmethod
    def validate_version(cls, value: str) -> str:
        if not _VERSION_RE.fullmatch(value):
            raise ValueError("version must use semantic x.y.z format")
        return value


class AgentModelPolicy(ContractModel):
    primary_model_config_id: str = Field(min_length=1, max_length=128)
    fallback_model_config_ids: tuple[str, ...] = ()

    @field_validator("fallback_model_config_ids")
    @classmethod
    def validate_fallbacks(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("fallback model ids must be unique")
        return value


class AgentModelVersionRef(ContractModel):
    model_config_id: str
    config_version: int = Field(ge=1)
    role: Literal["primary", "fallback"]
    order: int = Field(ge=0)


class AgentReleaseSnapshot(ContractModel):
    release_id: str
    app_id: str
    agent_id: str
    agent_version: str
    prompt_ref: str | None = None
    capability_refs: tuple[str, ...] = ()
    skill_refs: tuple[str, ...] = ()
    workflow_refs: tuple[str, ...] = ()
    knowledge_base_refs: tuple[str, ...] = ()
    model_refs: tuple[AgentModelVersionRef, ...]
    published_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    published_by: str
    reason: str


class RunAgentReleaseSnapshot(AgentReleaseSnapshot):
    run_id: str
    tenant_id: str = "legacy"
    bound_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class AgentValidationIssue(ContractModel):
    code: str
    field: str
    message: str


class AgentValidationReport(ContractModel):
    is_valid: bool
    issues: tuple[AgentValidationIssue, ...] = ()
