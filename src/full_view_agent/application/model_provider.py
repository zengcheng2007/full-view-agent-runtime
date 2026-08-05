from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol


@dataclass(frozen=True)
class ModelMessage:
    role: Literal["system", "user", "assistant", "tool"]
    content: str | None = None
    tool_call_id: str | None = None
    tool_calls: tuple[ModelToolCall, ...] | None = None


@dataclass(frozen=True)
class ModelToolDefinition:
    tool_id: str
    description: str
    input_schema: dict[str, object]
    # Server-owned arguments are never sent to the model. ModelPlanner injects
    # them into the accepted ToolAction before it reaches a durable checkpoint.
    server_arguments: dict[str, object] = field(default_factory=dict)
    # Some catalog subjects are narrower than their historical internal IDs.
    # These server-side requirements prevent the model from silently replacing
    # a broad user request with a specialized dataset (for example, general
    # population -> solitary elderly). Providers do not serialize this field.
    subject_intent_terms: dict[str, tuple[str, ...]] = field(default_factory=dict)


@dataclass(frozen=True)
class ModelRequest:
    messages: tuple[ModelMessage, ...]
    tools: tuple[ModelToolDefinition, ...] = ()
    max_output_tokens: int | None = None
    prompt_version: str | None = None


@dataclass(frozen=True)
class ModelToolCall:
    tool_id: str
    arguments: dict[str, object]
    call_id: str | None = None


@dataclass(frozen=True)
class ModelUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


@dataclass(frozen=True)
class ModelResponse:
    content: str | None
    tool_calls: tuple[ModelToolCall, ...]
    finish_reason: str
    usage: ModelUsage = ModelUsage()


class ModelProvider(Protocol):
    async def complete(self, request: ModelRequest) -> ModelResponse: ...
