import json

from full_view_agent.application.harness import HarnessState
from full_view_agent.application.model_provider import (
    ModelMessage,
    ModelRequest,
    ModelToolDefinition,
)
from full_view_agent.application.ports import AgentStore
from full_view_agent.application.prompt_catalog import (
    FULL_VIEW_SYSTEM_PROMPT_VERSION,
    build_full_view_system_prompt,
)
from full_view_agent.application.tool_registry import ToolRegistry
from full_view_agent.domain.models import AgentMessage, AuthContext, MessageContent

NON_RETRYABLE_TOOL_WARNINGS = {
    "upstream_timeout",
    "upstream_unavailable",
    "upstream_contract_error",
}

MAX_CONTEXT_MESSAGES = 20
MAX_CONTEXT_CHARS = 8_000
SUMMARY_MARKER = "[会话摘要]"


class AgentContextBuilder:
    def __init__(
        self,
        *,
        store: AgentStore,
        registry: ToolRegistry,
        max_context_messages: int = MAX_CONTEXT_MESSAGES,
        max_context_chars: int = MAX_CONTEXT_CHARS,
    ) -> None:
        self._store = store
        self._registry = registry
        self._max_messages = max_context_messages
        self._max_chars = max_context_chars

    async def build(
        self,
        *,
        user_id: str,
        auth_context: AuthContext,
        state: HarnessState,
    ) -> ModelRequest:
        authorization = {
            "tenant_id": auth_context.principal.tenant_id,
            "org_id": auth_context.principal.org_id,
            "roles": auth_context.principal.roles,
            "purpose": auth_context.purpose,
            "area_scopes": [
                area.model_dump(mode="json") for area in auth_context.data_scopes.areas
            ],
            "datasets": auth_context.data_scopes.datasets,
            "entitlements": auth_context.entitlements,
        }
        messages = [
            ModelMessage(
                role="system",
                content=build_full_view_system_prompt(authorization),
            )
        ]
        session_messages = await self._store.list_messages(
            user_id=user_id,
            session_id=auth_context.session_id,
        )
        messages.extend(
            _format_messages(session_messages, self._max_messages, self._max_chars)
        )
        if state.tool_results:
            observations = []
            for result in state.tool_results:
                obs: dict[str, object] = {
                    "tool_id": result.tool_id,
                    "status": result.status,
                    "summary": result.summary,
                    "warnings": result.warnings,
                    "evidence_ids": result.evidence_ids,
                }
                if result.data_result is not None:
                    data_ref: dict[str, object] = {
                        "result_id": result.data_result.result_id,
                        "kind": result.data_result.kind,
                    }
                    row_count = getattr(result.data_result, "row_count", None)
                    if row_count is not None:
                        data_ref["row_count"] = row_count
                    # Include sample rows (up to 5) so the model can answer
                    # specific questions without needing another tool call.
                    rows = getattr(result.data_result, "rows", None)
                    if isinstance(rows, list) and rows:
                        sample = rows[:5]
                        data_ref["sample_rows"] = [
                            row.model_dump(mode="json") if hasattr(row, "model_dump") else row
                            for row in sample
                        ]
                        if len(rows) > 5:
                            data_ref["truncated"] = True
                    obs["data_result"] = data_ref
                observations.append(obs)
            messages.append(
                ModelMessage(
                    role="system",
                    content=(
                        "已验证的 Tool 观察（只能基于这些结果继续或完成）："
                        + json.dumps(observations, ensure_ascii=False, sort_keys=True)
                    ),
                )
            )

        entitlements = set(auth_context.entitlements)
        datasets = set(auth_context.data_scopes.datasets)
        terminal_tool_ids = {
            result.tool_id
            for result in state.tool_results
            if result.status in {"success", "partial", "denied"}
            or (
                result.status == "failed"
                and bool(NON_RETRYABLE_TOOL_WARNINGS.intersection(result.warnings))
            )
        }
        tools: list[ModelToolDefinition] = []
        for tool_id in self._registry.list_tool_ids():
            if tool_id in terminal_tool_ids:
                continue
            manifest = self._registry.get_manifest(tool_id)
            if manifest.status != "active":
                continue
            if not set(manifest.required_permissions).issubset(entitlements):
                continue
            if manifest.dataset_id not in datasets:
                continue
            descriptor = self._registry.get_model_descriptor(tool_id)
            tools.append(
                ModelToolDefinition(
                    tool_id=tool_id,
                    description=descriptor.description,
                    input_schema=self._registry.get_input_schema(tool_id),
                )
            )
        return ModelRequest(
            messages=tuple(messages),
            tools=tuple(tools),
            prompt_version=FULL_VIEW_SYSTEM_PROMPT_VERSION,
        )


def _format_messages(
    messages: list[AgentMessage],
    max_messages: int,
    max_chars: int,
) -> list[ModelMessage]:
    formatted = [
        ModelMessage(role=m.role, content=_message_text(m.content))
        for m in messages
    ]
    if not formatted:
        return formatted

    # Always preserve the latest user message; truncate its content if needed.
    # Reserve space for the summary prefix and truncation suffix.
    summary_overhead = len(SUMMARY_MARKER) + 80  # "已省略...条...以控制上下文长度。"
    truncation_suffix = "…（已截断）"
    effective_budget = max(0, max_chars - summary_overhead)

    latest_user_idx = None
    for i in range(len(formatted) - 1, -1, -1):
        if formatted[i].role == "user":
            latest_user_idx = i
            break

    if latest_user_idx is not None:
        latest = formatted[latest_user_idx]
        if len(latest.content) > effective_budget:
            formatted[latest_user_idx] = ModelMessage(
                role=latest.role,
                content=latest.content[:effective_budget] + truncation_suffix,
            )

    # Trim from the front, but never remove the latest user message.
    while len(formatted) > max_messages or (
        sum(len(m.content) for m in formatted) > effective_budget
        and len(formatted) > 1
    ):
        pop_idx = 0
        if latest_user_idx is not None and latest_user_idx == 0:
            pop_idx = 1
        if pop_idx >= len(formatted):
            break
        formatted.pop(pop_idx)
        if latest_user_idx is not None:
            latest_user_idx -= 1
            if latest_user_idx < 0:
                latest_user_idx = 0

    if len(messages) > len(formatted):
        overflow_count = len(messages) - len(formatted)
        summary_text = (
            f"{SUMMARY_MARKER} 已省略更早的 {overflow_count} 条会话记录，"
            f"仅保留最近 {len(formatted)} 条以控制上下文长度。"
        )
        result = [ModelMessage(role="system", content=summary_text), *formatted]
    else:
        result = formatted

    # Enforce final budget: total characters including summary must not exceed max_chars.
    total_chars = sum(len(m.content) for m in result)
    if total_chars > max_chars:
        # Trim from the oldest non-summary, non-latest-user message
        while total_chars > max_chars and len(result) > 2:
            victim = next(
                (i for i, m in enumerate(result)
                 if m.role != "system" or SUMMARY_MARKER not in m.content),
                None,
            )
            if victim is None or victim == len(result) - 1:
                break
            total_chars -= len(result[victim].content)
            result.pop(victim)

    return result


def _message_text(content: list[MessageContent]) -> str:
    parts: list[str] = []
    for item in content:
        if item.type == "text":
            parts.append(item.text)
        else:
            parts.append(f"[结果引用 {item.result_id}] {item.label}")
    return "\n".join(parts)
