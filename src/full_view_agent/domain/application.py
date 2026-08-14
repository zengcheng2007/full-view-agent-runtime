"""Cross-application registry contracts for the shared Agent Runtime."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Literal

from pydantic import Field, field_validator

from full_view_agent.domain.contract_model import ContractModel

_IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")


class AgentApplicationDefinition(ContractModel):
    app_id: str = Field(min_length=2, max_length=64)
    name: str = Field(min_length=1, max_length=200)
    default_agent_id: str = Field(min_length=1, max_length=128)
    identity_adapter_id: str = Field(min_length=1, max_length=128)
    status: Literal["active", "disabled"] = "active"
    description: str = Field(default="", max_length=2000)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_by: str = Field(default="system", max_length=100)
    last_reason: str = Field(default="", max_length=2000)
    etag: int = Field(default=1, ge=1)

    @field_validator("app_id")
    @classmethod
    def validate_app_id(cls, value: str) -> str:
        if not _IDENTIFIER_RE.fullmatch(value):
            raise ValueError("app_id must be a controlled lowercase identifier")
        return value


class ApplicationCapabilityBinding(ContractModel):
    app_id: str = Field(min_length=2, max_length=64)
    capability_id: str = Field(min_length=1, max_length=128)
    capability_version: str = Field(min_length=1, max_length=32)
    enabled: bool = True
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    changed_by: str = Field(default="system", max_length=100)
    reason: str = Field(default="", max_length=2000)
    etag: int = Field(default=1, ge=1)

    @field_validator("app_id")
    @classmethod
    def validate_app_id(cls, value: str) -> str:
        if not _IDENTIFIER_RE.fullmatch(value):
            raise ValueError("app_id must be a controlled lowercase identifier")
        return value
