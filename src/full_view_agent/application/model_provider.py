from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol

from full_view_agent.domain.capability import ToolSemanticContract


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
    # Exact published contracts are server-only routing authority. Providers
    # never receive these objects; they are consumed before a model call.
    semantic_contracts: tuple[ToolSemanticContract, ...] = ()
    # Some catalog subjects are narrower than their historical internal IDs.
    # These server-side requirements prevent the model from silently replacing
    # a broad user request with a specialized dataset (for example, general
    # population -> solitary elderly). Providers do not serialize this field.
    subject_intent_terms: dict[str, tuple[str, ...]] = field(default_factory=dict)
    # Broad subject words used only by the server to detect an attempted
    # broad-to-specialized substitution before a model call is made.
    subject_trigger_terms: dict[str, tuple[str, ...]] = field(default_factory=dict)
    # Canonical specialized Tools use this direct requirement when they are
    # exposed without a semantic facade (for example in reduced test/runtime
    # configurations). It is server metadata and is never sent to providers.
    required_intent_terms: tuple[str, ...] = ()
    # Optional filter values may represent a materially narrower business
    # object than their parent subject. The runtime accepts those values only
    # when the latest user message contains an explicit matching term.
    specialized_filter_intent_terms: dict[
        str, dict[str, tuple[str, ...]]
    ] = field(default_factory=dict)
    # Server-only metadata for the virtual Skill entry. Providers serialize
    # only ``tool_id``, ``description`` and ``input_schema``. The planner uses
    # these maps to unwrap and validate a Skill-owned Tool call.
    skill_tool_allowlists: dict[str, tuple[str, ...]] = field(default_factory=dict)
    wrapped_tool_definitions: dict[str, ModelToolDefinition] = field(
        default_factory=dict
    )


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
