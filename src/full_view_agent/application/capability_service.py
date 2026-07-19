import json
from datetime import UTC, datetime
from typing import Protocol

from pydantic import BaseModel, ValidationError

from full_view_agent.application.errors import (
    PolicyBindingMismatch,
    SemanticValidationError,
    UpstreamContractError,
    UpstreamTimeout,
    UpstreamUnavailable,
)
from full_view_agent.application.fingerprints import canonical_fingerprint
from full_view_agent.application.tool_registry import ToolRegistry
from full_view_agent.domain.models import (
    AuthContext,
    DataResult,
    GetObjectProfileInput,
    InternalToolManifest,
    ObjectProfileResult,
    PolicyDecision,
    QueryPopulationMetricsInput,
    ResolveAreaInput,
    ToolResult,
    ToolResultPolicy,
)


class ToolAdapter(Protocol):
    async def execute(
        self,
        *,
        manifest: InternalToolManifest,
        arguments: BaseModel,
        policy_decision: PolicyDecision,
        auth_context: AuthContext,
    ) -> DataResult: ...


class PolicyEvaluator(Protocol):
    def evaluate(
        self,
        *,
        manifest: InternalToolManifest,
        auth_context: AuthContext,
        arguments: BaseModel,
    ) -> PolicyDecision: ...

    def evaluate_post_result(
        self,
        *,
        manifest: InternalToolManifest,
        auth_context: AuthContext,
        arguments: BaseModel,
        result: DataResult,
    ) -> PolicyDecision: ...


class AuthContextRefresher(Protocol):
    async def refresh(self, auth_context: AuthContext) -> AuthContext: ...


class DenialLedger(Protocol):
    async def contains(
        self,
        *,
        manifest: InternalToolManifest,
        arguments: BaseModel,
        auth_context: AuthContext,
    ) -> bool: ...

    async def record(
        self,
        *,
        manifest: InternalToolManifest,
        arguments: BaseModel,
        auth_context: AuthContext,
        decision: PolicyDecision,
    ) -> None: ...


TOOL_INPUT_MODELS: dict[str, type[BaseModel]] = {
    "governance.resolve_area": ResolveAreaInput,
    "governance.query_population_metrics": QueryPopulationMetricsInput,
    "governance.get_object_profile": GetObjectProfileInput,
}


def _normalize_typed_object_fields(
    input_model: type[BaseModel],
    raw_arguments: dict[str, object],
) -> dict[str, object]:
    normalized = dict(raw_arguments)
    for field_name, field in input_model.model_fields.items():
        value = normalized.get(field_name)
        annotation = field.annotation
        if not isinstance(value, str):
            continue
        if not isinstance(annotation, type) or not issubclass(annotation, BaseModel):
            continue
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            normalized[field_name] = parsed
    return normalized


class CapabilityService:
    def __init__(
        self,
        *,
        registry: ToolRegistry,
        policy: PolicyEvaluator,
        adapter: ToolAdapter,
        auth_context_refresher: AuthContextRefresher | None = None,
        denial_ledger: DenialLedger | None = None,
    ) -> None:
        self._registry = registry
        self._policy = policy
        self._adapter = adapter
        self._auth_context_refresher = auth_context_refresher
        self._denial_ledger = denial_ledger

    async def execute(
        self,
        *,
        tool_call_id: str,
        tool_id: str,
        raw_arguments: dict[str, object],
        auth_context: AuthContext,
    ) -> ToolResult:
        manifest = self._registry.get_manifest(tool_id)
        input_model = TOOL_INPUT_MODELS[tool_id]
        try:
            arguments = input_model.model_validate(
                _normalize_typed_object_fields(input_model, raw_arguments)
            )
        except ValidationError as exc:
            invalid_fields = sorted(
                {
                    ".".join(str(part) for part in error["loc"])
                    for error in exc.errors(include_url=False, include_input=False)
                }
            )
            return ToolResult(
                tool_call_id=tool_call_id,
                tool_id=manifest.tool_id,
                tool_version=manifest.tool_version,
                status="failed",
                summary=(
                    "Tool 参数不符合 Schema，请修正后重试。"
                    f"错误字段：{', '.join(invalid_fields) or 'unknown'}。"
                    "JSON 对象字段必须直接传对象，不能传序列化后的字符串。"
                ),
                warnings=["TOOL_ARGUMENT_VALIDATION_FAILED"],
            )
        if self._denial_ledger is not None and await self._denial_ledger.contains(
            manifest=manifest,
            arguments=arguments,
            auth_context=auth_context,
        ):
            return ToolResult(
                tool_call_id=tool_call_id,
                tool_id=manifest.tool_id,
                tool_version=manifest.tool_version,
                status="denied",
                summary="当前 Run 已拒绝等价的资源范围。",
                warnings=["DENIAL_LEDGER_MATCH"],
            )
        decision = self._policy.evaluate(
            manifest=manifest,
            auth_context=auth_context,
            arguments=arguments,
        )
        self._verify_policy_binding(
            manifest=manifest,
            arguments=arguments,
            auth_context=auth_context,
            decision=decision,
            result=None,
        )
        if decision.decision == "deny":
            await self._record_denial(
                manifest=manifest,
                arguments=arguments,
                auth_context=auth_context,
                decision=decision,
            )
            return ToolResult(
                tool_call_id=tool_call_id,
                tool_id=manifest.tool_id,
                tool_version=manifest.tool_version,
                status="denied",
                summary=decision.user_message,
                policy=_tool_result_policy(decision),
                warnings=decision.reason_codes,
            )
        try:
            data_result = await self._adapter.execute(
                manifest=manifest,
                arguments=arguments,
                policy_decision=decision,
                auth_context=auth_context,
            )
        except (
            SemanticValidationError,
            UpstreamContractError,
            UpstreamTimeout,
            UpstreamUnavailable,
        ) as exc:
            if isinstance(exc, SemanticValidationError):
                summary = f"当前业务能力暂不支持该查询组合：{exc}。"
            else:
                summary = "现有业务服务暂时不可用。"
            return ToolResult(
                tool_call_id=tool_call_id,
                tool_id=manifest.tool_id,
                tool_version=manifest.tool_version,
                status="failed",
                summary=summary,
                warnings=[exc.code],
            )
        final_result = data_result
        post_decision: PolicyDecision | None = None
        requires_post_policy = (
            "sensitive" in manifest.data_classifications
            or decision.expires_at <= datetime.now(UTC)
        )
        if requires_post_policy:
            if self._auth_context_refresher is None:
                raise PolicyBindingMismatch(
                    "sensitive result requires a refreshed auth context"
                )
            refreshed_context = await self._auth_context_refresher.refresh(auth_context)
            refreshed_scope = self._policy.evaluate(
                manifest=manifest,
                auth_context=refreshed_context,
                arguments=arguments,
            )
            self._verify_policy_binding(
                manifest=manifest,
                arguments=arguments,
                auth_context=refreshed_context,
                decision=refreshed_scope,
                result=None,
            )
            final_result = _apply_result_scope(data_result, refreshed_scope)
            post_decision = self._policy.evaluate_post_result(
                manifest=manifest,
                auth_context=refreshed_context,
                arguments=arguments,
                result=final_result,
            )
            self._verify_policy_binding(
                manifest=manifest,
                arguments=arguments,
                auth_context=refreshed_context,
                decision=post_decision,
                result=final_result,
            )
            if post_decision.decision == "deny":
                await self._record_denial(
                    manifest=manifest,
                    arguments=arguments,
                    auth_context=refreshed_context,
                    decision=post_decision,
                )
                return ToolResult(
                    tool_call_id=tool_call_id,
                    tool_id=manifest.tool_id,
                    tool_version=manifest.tool_version,
                    status="denied",
                    summary=post_decision.user_message,
                    policy=_tool_result_policy(decision, post_decision),
                    warnings=post_decision.reason_codes,
                )
        final_decision = post_decision or decision
        return ToolResult(
            tool_call_id=tool_call_id,
            tool_id=manifest.tool_id,
            tool_version=manifest.tool_version,
            status="success",
            summary="Tool 执行成功。",
            data_result=final_result,
            policy=_tool_result_policy(decision, post_decision),
            warnings=final_decision.reason_codes,
        )

    async def _record_denial(
        self,
        *,
        manifest: InternalToolManifest,
        arguments: BaseModel,
        auth_context: AuthContext,
        decision: PolicyDecision,
    ) -> None:
        if self._denial_ledger is not None:
            await self._denial_ledger.record(
                manifest=manifest,
                arguments=arguments,
                auth_context=auth_context,
                decision=decision,
            )

    @staticmethod
    def _verify_policy_binding(
        *,
        manifest: InternalToolManifest,
        arguments: BaseModel,
        auth_context: AuthContext,
        decision: PolicyDecision,
        result: DataResult | None,
    ) -> None:
        expected_arguments_fingerprint = canonical_fingerprint(
            domain=f"tool-arguments:{manifest.tool_id}:{manifest.tool_version}",
            value=arguments,
        )
        expected_request_fingerprint = canonical_fingerprint(
            domain="policy-request:1.1",
            value={
                "tool_id": manifest.tool_id,
                "tool_version": manifest.tool_version,
                "arguments_fingerprint": expected_arguments_fingerprint,
                "auth_context_fingerprint": auth_context.auth_context_fingerprint,
                "purpose": auth_context.purpose,
                "phase": "post_result" if result is not None else "pre_execution",
                "result_fingerprint": (
                    result.result_fingerprint if result is not None else None
                ),
            },
        )
        expected_phase = "post_result" if result is not None else "pre_execution"
        if (
            decision.tool_id != manifest.tool_id
            or decision.tool_version != manifest.tool_version
            or decision.auth_context_fingerprint
            != auth_context.auth_context_fingerprint
            or decision.arguments_fingerprint != expected_arguments_fingerprint
            or decision.request_fingerprint != expected_request_fingerprint
            or decision.phase != expected_phase
            or decision.result_fingerprint
            != (result.result_fingerprint if result is not None else None)
            or decision.expires_at <= datetime.now(UTC)
        ):
            raise PolicyBindingMismatch("policy decision does not match tool execution")


def _tool_result_policy(
    pre_decision: PolicyDecision,
    post_decision: PolicyDecision | None = None,
) -> ToolResultPolicy:
    decision = post_decision or pre_decision
    return ToolResultPolicy(
        decision=decision.decision,
        policy_decision_id=decision.policy_decision_id,
        pre_policy_decision_id=pre_decision.policy_decision_id,
        post_policy_decision_id=(
            post_decision.policy_decision_id if post_decision is not None else None
        ),
        auth_context_fingerprint=decision.auth_context_fingerprint,
        arguments_fingerprint=decision.arguments_fingerprint,
        request_fingerprint=decision.request_fingerprint,
        policy_fingerprint=decision.policy_fingerprint,
        masked_fields=decision.effective_scope.denied_field_sets,
    )


def _apply_result_scope(
    result: DataResult,
    decision: PolicyDecision,
) -> DataResult:
    if not isinstance(result, ObjectProfileResult):
        return result
    if "contact" not in decision.effective_scope.denied_field_sets:
        return result
    visible_data = result.data.model_copy(
        update={
            "fields": [
                field
                for field in result.data.fields
                if field.classification != "sensitive"
            ]
        }
    )
    return result.model_copy(
        update={
            "data": visible_data,
            "result_fingerprint": canonical_fingerprint(
                domain="data-result:object-profile:1.0.0",
                value=visible_data,
            ),
        }
    )
