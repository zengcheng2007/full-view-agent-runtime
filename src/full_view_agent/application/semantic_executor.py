"""S1-A：Harness 执行层的语义入口包装。

``SemanticToolExecutor`` 实现 ``HarnessToolExecutor`` 协议，包装生产
``CapabilityService``：

- 非 ``governance.semantic_query`` 的 Tool 调用原样直通，行为不变；
- ``semantic_query`` 先经 ``SemanticActionResolver`` 解析（parse →
  S1-A 白名单 → 必填筛选 → Validator → Compiler → ExecutionGuard
  生产 Policy 复核），再以规范 ToolAction 交给既有 CapabilityService
  执行 —— 权限、拒绝账本、Adapter 与结果标准化全部复用生产链路；
- 语义错误按 failed 归类（模型可修正 spec），纯授权违规按 denied
  归类；成功结果经 Compiler 结果 Schema 复核后附加语义血缘，供
  Evidence 持久化与前端地图命令读取。

``SemanticToolCallFingerprinter`` 把循环检测指纹建立在规范动作上：
同义问法（同一受控 spec）收敛到同一指纹，语义入口与等价的直接
Tool 调用共享重复计数；解析失败回退原始指纹，由执行层落结构化
失败结果，观察指纹与连续失败限制保证收敛。
"""

from typing import Literal, Protocol

from full_view_agent.application.capability_service import (
    tool_result_policy,
)
from full_view_agent.application.harness import (
    ToolAction,
    default_tool_call_fingerprint,
)
from full_view_agent.application.semantic_denial import SemanticDenialRecorder
from full_view_agent.domain.models import (
    AuthContext,
    TableDataResult,
    ToolResult,
    ToolResultPolicy,
)
from full_view_agent.semantic.action_resolver import (
    SEMANTIC_QUERY_TOOL_ID,
    SEMANTIC_QUERY_TOOL_VERSION,
    DeniedSemanticAction,
    RejectedSemanticAction,
    SemanticActionResolver,
)
from full_view_agent.semantic.compiler import SemanticCompiler
from full_view_agent.semantic.errors import ResultSchemaMismatch


class InnerToolExecutor(Protocol):
    async def execute(
        self,
        *,
        tool_call_id: str,
        tool_id: str,
        raw_arguments: dict[str, object],
        auth_context: AuthContext,
    ) -> ToolResult: ...


class SemanticToolExecutor:
    """把 semantic_query 解析为规范 Tool 后复用生产执行链路。"""

    def __init__(
        self,
        *,
        inner: InnerToolExecutor,
        resolver: SemanticActionResolver,
        compiler: SemanticCompiler | None = None,
        denial_recorder: SemanticDenialRecorder | None = None,
    ) -> None:
        self._inner = inner
        self._resolver = resolver
        self._compiler = compiler or SemanticCompiler(
            resolver.catalog,
        )
        self._denial_recorder = denial_recorder

    async def execute(
        self,
        *,
        tool_call_id: str,
        tool_id: str,
        raw_arguments: dict[str, object],
        auth_context: AuthContext,
    ) -> ToolResult:
        if tool_id != SEMANTIC_QUERY_TOOL_ID:
            return await self._inner.execute(
                tool_call_id=tool_call_id,
                tool_id=tool_id,
                raw_arguments=raw_arguments,
                auth_context=auth_context,
            )
        resolution = self._resolver.resolve(raw_arguments, auth_context=auth_context)
        if isinstance(resolution, RejectedSemanticAction):
            recorded_decision = None
            if (
                resolution.is_authorization_denial
                and self._denial_recorder is not None
            ):
                recorded_decision = await self._denial_recorder.record_if_denied(
                    raw_arguments,
                    auth_context=auth_context,
                )
            return self._virtual_result(
                tool_call_id=tool_call_id,
                status=(
                    "denied"
                    if resolution.is_authorization_denial
                    or any(
                        code
                        in {
                            "CATALOG_VERSION_MISMATCH",
                            "CATALOG_FINGERPRINT_MISMATCH",
                        }
                        for code in resolution.codes
                    )
                    else "failed"
                ),
                summary=resolution.user_message,
                warnings=list(resolution.codes),
                policy=(
                    tool_result_policy(recorded_decision)
                    if recorded_decision is not None
                    else None
                ),
            )
        if isinstance(resolution, DeniedSemanticAction):
            if self._denial_recorder is not None:
                await self._denial_recorder.record_if_denied(
                    raw_arguments,
                    auth_context=auth_context,
                )
            policy = (
                tool_result_policy(resolution.decisions[-1])
                if resolution.decisions
                else None
            )
            return self._virtual_result(
                tool_call_id=tool_call_id,
                status="denied",
                summary=resolution.user_message,
                warnings=list(resolution.codes),
                policy=policy,
            )
        canonical = resolution.canonical_action
        result = await self._inner.execute(
            tool_call_id=tool_call_id,
            tool_id=canonical.tool_id,
            raw_arguments=canonical.arguments,
            auth_context=auth_context,
        )
        if result.status in {"success", "partial"}:
            data_result = result.data_result
            if not isinstance(data_result, TableDataResult):
                return self._virtual_result(
                    tool_call_id=tool_call_id,
                    status="failed",
                    summary="语义查询编译期望表格结果，实际结果形态不符。",
                    warnings=["SEMANTIC_RESULT_SCHEMA_MISMATCH"],
                )
            try:
                self._compiler.verify_result(resolution.plan, data_result)
            except ResultSchemaMismatch as exc:
                return self._virtual_result(
                    tool_call_id=tool_call_id,
                    status="failed",
                    summary=f"语义计划结果 Schema 复核失败：{exc}",
                    warnings=["SEMANTIC_RESULT_SCHEMA_MISMATCH"],
                )
            return result.model_copy(
                update={"semantic_lineage": resolution.lineage}
            )
        return result

    @staticmethod
    def _virtual_result(
        *,
        tool_call_id: str,
        status: Literal["denied", "failed"],
        summary: str,
        warnings: list[str],
        policy: ToolResultPolicy | None = None,
    ) -> ToolResult:
        return ToolResult(
            tool_call_id=tool_call_id,
            tool_id=SEMANTIC_QUERY_TOOL_ID,
            tool_version=SEMANTIC_QUERY_TOOL_VERSION,
            status=status,
            summary=summary,
            warnings=warnings,
            policy=policy,
        )


class SemanticToolCallFingerprinter:
    """循环检测指纹：semantic_query 归一到规范动作指纹。"""

    def __init__(self, *, resolver: SemanticActionResolver) -> None:
        self._resolver = resolver

    def fingerprint(self, action: ToolAction, *, auth_context: AuthContext) -> str:
        if action.tool_id != SEMANTIC_QUERY_TOOL_ID:
            return default_tool_call_fingerprint(action)
        compiled = self._resolver.compile_action(
            action.arguments, auth_context=auth_context
        )
        if isinstance(compiled, RejectedSemanticAction):
            # 非法/被拒 spec：保留原始指纹，执行层落结构化失败结果，
            # 观察指纹与连续失败限制仍然保证循环收敛。
            return default_tool_call_fingerprint(action)
        return default_tool_call_fingerprint(compiled.canonical_action)
