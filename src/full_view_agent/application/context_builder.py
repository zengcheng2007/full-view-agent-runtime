import json
from typing import Protocol

from full_view_agent.application.errors import ResourceNotFound
from full_view_agent.application.harness import HarnessState
from full_view_agent.application.model_provider import (
    ModelMessage,
    ModelRequest,
    ModelToolCall,
    ModelToolDefinition,
)
from full_view_agent.application.ports import AgentStore
from full_view_agent.application.prompt_catalog import (
    FULL_VIEW_SYSTEM_PROMPT_VERSION,
    build_full_view_system_prompt,
)
from full_view_agent.application.tool_registry import ToolRegistry
from full_view_agent.domain.models import AgentMessage, AuthContext, MessageContent
from full_view_agent.semantic.presenter import SemanticToolPresentation

NON_RETRYABLE_TOOL_WARNINGS = {
    "upstream_timeout",
    "upstream_unavailable",
    "upstream_contract_error",
}

MAX_CONTEXT_MESSAGES = 20
MAX_CONTEXT_CHARS = 8_000
MAX_OBSERVATION_ROWS = 20
SUMMARY_MARKER = "[会话摘要]"


class SemanticToolPresenting(Protocol):
    """S1-A：按当前授权派生 semantic_query 虚拟 Tool（fail closed）。"""

    @property
    def shadowed_tool_ids(self) -> tuple[str, ...]: ...

    def present(
        self, *, auth_context: AuthContext
    ) -> SemanticToolPresentation | None: ...


class AgentContextBuilder:
    def __init__(
        self,
        *,
        store: AgentStore,
        registry: ToolRegistry,
        max_context_messages: int = MAX_CONTEXT_MESSAGES,
        max_context_chars: int = MAX_CONTEXT_CHARS,
        semantic_presenter: SemanticToolPresenting | None = None,
    ) -> None:
        self._store = store
        self._registry = registry
        self._max_messages = max_context_messages
        self._max_chars = max_context_chars
        self._semantic_presenter = semantic_presenter

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
        entitlements = set(auth_context.entitlements)
        datasets = set(auth_context.data_scopes.datasets)
        # S1-B：语义虚拟 Tool 的可见性与描述由授权派生。它接管的
        # canonical Tool 仍供内部执行，但不再与语义入口同时暴露给模型，
        # 防止绕过 Catalog 约束或对同一规范动作重复查询。
        semantic_presentation = (
            self._semantic_presenter.present(auth_context=auth_context)
            if self._semantic_presenter is not None
            else None
        )
        shadowed_tool_ids = (
            frozenset(self._semantic_presenter.shadowed_tool_ids)
            if self._semantic_presenter is not None
            else frozenset()
        )
        terminal_tool_ids = {
            result.tool_id
            for result in state.tool_results
            if _is_terminal_tool_result(result)
        }
        # 提示词能力清单与模型可选 Tool 共用同一授权过滤条件，
        # 保证提示词不宣称未注册或未授权的 Tool。
        authorized_tool_ids = tuple(
            tool_id
            for tool_id in self._registry.list_tool_ids()
            if tool_id not in shadowed_tool_ids
            if tool_id not in terminal_tool_ids
            if self._is_tool_authorized(tool_id, entitlements, datasets)
        )
        messages = [
            ModelMessage(
                role="system",
                content=build_full_view_system_prompt(
                    authorization,
                    tool_ids=authorized_tool_ids,
                    semantic_capabilities=(
                        semantic_presentation.description
                        if semantic_presentation is not None
                        else None
                    ),
                    housing_next_area_enabled=(
                        self._registry.housing_next_area_enabled
                    ),
                ),
            )
        ]
        session_messages = await self._store.list_messages(
            user_id=user_id,
            session_id=auth_context.session_id,
        )
        messages.extend(
            _format_messages(session_messages, self._max_messages, self._max_chars)
        )
        if state.inherited_result_ids:
            inherited_observations: list[dict[str, object]] = []
            for result_id in state.inherited_result_ids:
                try:
                    result = await self._store.get_result(
                        user_id=user_id,
                        result_id=result_id,
                    )
                except ResourceNotFound:
                    continue
                if getattr(result, "payload_status", "available") != "available":
                    continue
                inherited_observations.append(
                    _build_inherited_result_observation(result)
                )
            if inherited_observations:
                messages.append(
                    ModelMessage(
                        role="system",
                        content=(
                            "会话中已有可引用的历史结果（尚未加载为当前运行可复算数据；"
                            "只能使用 reference_only 指向数据面板，不得复述、排序、筛选、"
                            "计算或解释）："
                            + json.dumps(
                                inherited_observations,
                                ensure_ascii=False,
                                sort_keys=True,
                            )
                        ),
                    )
                )
        if state.tool_results:
            if state.tool_actions:
                # Standard OpenAI format: assistant(tool_calls) + tool(result)
                # Use real tool_call_ids so IDs match between
                # assistant tool_calls and tool messages.
                # A tool result must immediately follow the assistant message
                # which requested it. Combining several sequential turns into
                # one assistant message makes the OpenAI tool-call transcript
                # invalid and breaks later planning turns.
                for i, result in enumerate(state.tool_results):
                    tc_id = (
                        state.tool_call_ids[i]
                        if i < len(state.tool_call_ids)
                        else f"call_{i}"
                    )
                    if i < len(state.tool_actions):
                        action = state.tool_actions[i]
                        messages.append(
                            ModelMessage(
                                role="assistant",
                                content=None,
                                tool_calls=(
                                    ModelToolCall(
                                        tool_id=action.tool_id,
                                        arguments=action.arguments,
                                        call_id=tc_id,
                                    ),
                                ),
                            )
                        )
                    obs = _build_observation(result)
                    messages.append(
                        ModelMessage(
                            role="tool",
                            tool_call_id=tc_id,
                            content=json.dumps(
                                obs, ensure_ascii=False, sort_keys=True,
                            ),
                        )
                    )
            else:
                # Fallback for states without tool_actions (e.g. tests)
                observations = [
                    _build_observation(r) for r in state.tool_results
                ]
                messages.append(
                    ModelMessage(
                        role="system",
                        content=(
                            "已验证的 Tool 观察（只能基于这些结果继续或完成）："
                            + json.dumps(
                                observations, ensure_ascii=False,
                                sort_keys=True,
                            )
                        ),
                    )
                )
        if state.completion_feedback:
            feedback_code = (
                f"（原因码：{state.completion_feedback_code}）"
                if state.completion_feedback_code
                else ""
            )
            messages.append(
                ModelMessage(
                    role="system",
                    content=(
                        "上一版最终回答未通过可信回答校验。"
                        + feedback_code
                        + state.completion_feedback
                        + "不得重复原来的无证据表述。"
                    ),
                )
            )

        entitlements = set(auth_context.entitlements)
        datasets = set(auth_context.data_scopes.datasets)
        tools: list[ModelToolDefinition] = []
        for tool_id in self._registry.list_tool_ids():
            if tool_id in shadowed_tool_ids:
                continue
            if tool_id in terminal_tool_ids:
                continue
            if not self._is_tool_authorized(tool_id, entitlements, datasets):
                continue
            descriptor = self._registry.get_model_descriptor(tool_id)
            tools.append(
                ModelToolDefinition(
                    tool_id=tool_id,
                    description=descriptor.description,
                    input_schema=self._registry.get_input_schema(tool_id),
                )
            )
        if (
            semantic_presentation is not None
            and semantic_presentation.tool_id not in terminal_tool_ids
        ):
            tools.append(
                ModelToolDefinition(
                    tool_id=semantic_presentation.tool_id,
                    description=semantic_presentation.description,
                    input_schema=semantic_presentation.input_schema,
                    server_arguments=semantic_presentation.server_arguments,
                )
            )
        return ModelRequest(
            messages=tuple(messages),
            tools=tuple(tools),
            prompt_version=FULL_VIEW_SYSTEM_PROMPT_VERSION,
        )

    def _is_tool_authorized(
        self,
        tool_id: str,
        entitlements: set[str],
        datasets: set[str],
    ) -> bool:
        manifest = self._registry.get_manifest(tool_id)
        if manifest.status != "active":
            return False
        if not set(manifest.required_permissions).issubset(entitlements):
            return False
        return manifest.dataset_id in datasets


def _build_observation(result: object) -> dict[str, object]:
    """Build a sanitized observation dict from a ToolResult."""
    obs: dict[str, object] = {
        "tool_id": result.tool_id,  # type: ignore[attr-defined]
        "status": result.status,  # type: ignore[attr-defined]
        "summary": result.summary,  # type: ignore[attr-defined]
        "warnings": result.warnings,  # type: ignore[attr-defined]
    }
    data_result = result.data_result  # type: ignore[attr-defined]
    if data_result is not None:
        data_ref: dict[str, object] = {
            "result_id": data_result.result_id,
            "result_fingerprint": data_result.result_fingerprint,
            "kind": data_result.kind,
        }
        row_count = getattr(data_result, "row_count", None)
        if row_count is not None:
            data_ref["row_count"] = row_count
        result_data = getattr(data_result, "data", None)
        rows = getattr(result_data, "rows", None)
        if isinstance(rows, list) and rows:
            sample = rows[:MAX_OBSERVATION_ROWS]
            data_ref["sample_rows"] = [
                row.model_dump(mode="json")
                if hasattr(row, "model_dump")
                else row
                for row in sample
            ]
            if len(rows) > MAX_OBSERVATION_ROWS:
                data_ref["truncated"] = True
        if data_result.kind == "area_candidates":
            area_data = data_result.data
            data_ref["resolved_area_code"] = area_data.resolved_area_code
            data_ref["ambiguous"] = area_data.ambiguous
            data_ref["candidate_count"] = data_result.candidate_count
            data_ref["candidates"] = [
                {
                    "area_code": candidate.area_code,
                    "area_name": candidate.area_name,
                    "level": candidate.level,
                    "parent_area_code": candidate.parent_area_code,
                }
                for candidate in area_data.candidates
            ]
        obs["data_result"] = data_ref
    return obs


def _is_terminal_tool_result(result: object) -> bool:
    status = getattr(result, "status", None)
    tool_id = getattr(result, "tool_id", None)
    if tool_id == "governance.resolve_area" and status in {"success", "partial"}:
        data_result = getattr(result, "data_result", None)
        candidate_count = getattr(data_result, "candidate_count", None)
        area_data = getattr(data_result, "data", None)
        resolved_area_code = getattr(area_data, "resolved_area_code", None)
        if candidate_count == 0 and resolved_area_code is None:
            return False
    if status in {"success", "partial", "denied"}:
        return True
    warnings = getattr(result, "warnings", ())
    return status == "failed" and bool(
        NON_RETRYABLE_TOOL_WARNINGS.intersection(warnings)
    )


def _build_inherited_result_observation(result: object) -> dict[str, object]:
    observation: dict[str, object] = {
        "result_id": result.result_id,  # type: ignore[attr-defined]
        "result_fingerprint": result.result_fingerprint,  # type: ignore[attr-defined]
        "kind": result.kind,  # type: ignore[attr-defined]
        "data_schema_ref": result.data_schema_ref,  # type: ignore[attr-defined]
    }
    row_count = getattr(result, "row_count", None)
    if row_count is not None:
        observation["row_count"] = row_count
    if getattr(result, "truncated", False):
        observation["truncated"] = True
    return observation


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
        latest_text = latest.content or ""
        if len(latest_text) > effective_budget:
            formatted[latest_user_idx] = ModelMessage(
                role=latest.role,
                content=latest_text[:effective_budget] + truncation_suffix,
            )

    # Trim from the front, but never remove the latest user message.
    while len(formatted) > max_messages or (
        sum(len(m.content or "") for m in formatted) > effective_budget
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
    total_chars = sum(len(m.content or "") for m in result)
    if total_chars > max_chars:
        # Trim from the oldest non-summary, non-latest-user message
        while total_chars > max_chars and len(result) > 2:
            victim = next(
                (i for i, m in enumerate(result)
                 if m.role != "system"
                 or SUMMARY_MARKER not in (m.content or "")),
                None,
            )
            if victim is None or victim == len(result) - 1:
                break
            total_chars -= len(result[victim].content or "")
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
