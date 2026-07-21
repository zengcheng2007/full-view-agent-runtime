from datetime import UTC, datetime, timedelta

from pydantic import BaseModel

from full_view_agent.application.authorization_scope import (
    area_is_within_scope,
    extract_area_scope,
)
from full_view_agent.application.fingerprints import canonical_fingerprint
from full_view_agent.application.session_run_service import new_id
from full_view_agent.domain.models import (
    AuthContext,
    DataResult,
    GetObjectProfileInput,
    InternalToolManifest,
    PolicyDecision,
)


class MinimalPolicyAdapter:
    def evaluate(
        self,
        *,
        manifest: InternalToolManifest,
        auth_context: AuthContext,
        arguments: BaseModel,
    ) -> PolicyDecision:
        return self._evaluate(
            manifest=manifest,
            auth_context=auth_context,
            arguments=arguments,
            phase="pre_execution",
            result=None,
        )

    def evaluate_post_result(
        self,
        *,
        manifest: InternalToolManifest,
        auth_context: AuthContext,
        arguments: BaseModel,
        result: DataResult,
    ) -> PolicyDecision:
        return self._evaluate(
            manifest=manifest,
            auth_context=auth_context,
            arguments=arguments,
            phase="post_result",
            result=result,
        )

    def _evaluate(
        self,
        *,
        manifest: InternalToolManifest,
        auth_context: AuthContext,
        arguments: BaseModel,
        phase: str,
        result: DataResult | None,
    ) -> PolicyDecision:
        now = datetime.now(UTC)
        arguments_fingerprint = canonical_fingerprint(
            domain=f"tool-arguments:{manifest.tool_id}:{manifest.tool_version}",
            value=arguments,
        )
        request_fingerprint = canonical_fingerprint(
            domain="policy-request:1.1",
            value={
                "tool_id": manifest.tool_id,
                "tool_version": manifest.tool_version,
                "arguments_fingerprint": arguments_fingerprint,
                "auth_context_fingerprint": auth_context.auth_context_fingerprint,
                "purpose": auth_context.purpose,
                "phase": phase,
                "result_fingerprint": (
                    result.result_fingerprint if result is not None else None
                ),
            },
        )
        argument_scope = extract_area_scope(arguments)
        result_scope = extract_area_scope(result)
        requested_area = (
            result_scope.area_code
            if result_scope is not None
            else argument_scope.area_code if argument_scope is not None else None
        )
        result_outside_request_scope = bool(
            result_scope is not None
            and argument_scope is not None
            and not area_is_within_scope(result_scope.area_code, argument_scope)
        )
        decision, reasons, message = self._decide(
            manifest=manifest,
            auth_context=auth_context,
            arguments=arguments,
            now=now,
            requested_area=requested_area,
            result_outside_request_scope=result_outside_request_scope,
        )
        allowed_field_sets, denied_field_sets = _field_sets(arguments, auth_context)
        effective_area_codes = [requested_area] if requested_area else [
            scope.area_code for scope in auth_context.data_scopes.areas
        ]
        effective_scope = {
            "area_codes": effective_area_codes if decision != "deny" else [],
            "datasets": [manifest.dataset_id] if decision != "deny" else [],
            "allowed_field_sets": allowed_field_sets if decision != "deny" else [],
            "denied_field_sets": denied_field_sets,
            "result_limit": manifest.limits.max_result_rows if decision != "deny" else 0,
        }
        expires_at = min(auth_context.expires_at, now + timedelta(seconds=60))
        policy_payload = {
            "decision": decision,
            "tool_id": manifest.tool_id,
            "tool_version": manifest.tool_version,
            "arguments_fingerprint": arguments_fingerprint,
            "request_fingerprint": request_fingerprint,
            "effective_scope": effective_scope,
            "policy_version": auth_context.policy_version,
            "phase": phase,
            "result_fingerprint": (
                result.result_fingerprint if result is not None else None
            ),
        }
        return PolicyDecision.model_validate(
            {
                "policy_decision_id": new_id("pol"),
                "decision": decision,
                "phase": phase,
                "auth_context_fingerprint": auth_context.auth_context_fingerprint,
                "tool_id": manifest.tool_id,
                "tool_version": manifest.tool_version,
                "arguments_fingerprint": arguments_fingerprint,
                "request_fingerprint": request_fingerprint,
                "result_fingerprint": (
                    result.result_fingerprint if result is not None else None
                ),
                "reason_codes": reasons,
                "user_message": message,
                "effective_scope": effective_scope,
                "policy_fingerprint": canonical_fingerprint(
                    domain="policy-decision:1.1",
                    value=policy_payload,
                ),
                "policy_version": auth_context.policy_version,
                "issued_at": now,
                "expires_at": expires_at,
            }
        )

    def _decide(
        self,
        *,
        manifest: InternalToolManifest,
        auth_context: AuthContext,
        arguments: BaseModel,
        now: datetime,
        requested_area: str | None,
        result_outside_request_scope: bool,
    ) -> tuple[str, list[str], str]:
        if auth_context.expires_at <= now:
            return "deny", ["AUTH_CONTEXT_EXPIRED"], "登录授权已过期，请重新认证。"
        if not set(manifest.required_permissions).issubset(auth_context.entitlements):
            return "deny", ["TOOL_NOT_ENTITLED"], "当前用户无权使用该能力。"
        if manifest.dataset_id not in auth_context.data_scopes.datasets:
            return "deny", ["DATASET_NOT_AUTHORIZED"], "当前数据集不在授权范围内。"
        if result_outside_request_scope:
            return (
                "deny",
                ["RESULT_AREA_OUTSIDE_REQUEST_SCOPE"],
                "结果区域与请求范围不一致。",
            )
        if requested_area and not _area_is_authorized(requested_area, auth_context):
            return "deny", ["AREA_OUT_OF_SCOPE"], "请求区域不在当前授权范围内。"
        allowed_field_sets, denied_field_sets = _field_sets(arguments, auth_context)
        if denied_field_sets and allowed_field_sets:
            return "mask", ["FIELD_RESTRICTED"], "部分敏感字段已按权限隐藏。"
        if denied_field_sets:
            return "deny", ["FIELD_NOT_AUTHORIZED"], "请求字段不在当前授权范围内。"
        return "allow", [], "允许执行。"


def _area_is_authorized(area_code: str, auth_context: AuthContext) -> bool:
    return any(
        area_code == scope.area_code
        or (scope.include_descendants and area_code.startswith(scope.area_code))
        for scope in auth_context.data_scopes.areas
    )


def _field_sets(
    arguments: BaseModel,
    auth_context: AuthContext,
) -> tuple[list[str], list[str]]:
    if not isinstance(arguments, GetObjectProfileInput):
        return [], []
    allowed = []
    denied = []
    for field_set in arguments.field_sets:
        if (
            field_set == "contact"
            and "governance.object.contact.read" not in auth_context.entitlements
        ):
            denied.append(field_set)
        else:
            allowed.append(field_set)
    return allowed, denied
