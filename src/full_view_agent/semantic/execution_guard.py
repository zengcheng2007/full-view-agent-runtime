"""S0 语义内核候选：执行前计划完整性复核 + 按实际主题复核权限。

执行编译计划前必须 fail closed：
1. 计划完整性复核（先于一切 Policy 评估，不产生 Policy 决策）：
   - plan.catalog_version 必须等于当前 Catalog 版本；
   - plan.subject 必须存在于当前 Catalog 且存在内部能力绑定；
   - plan.logical_dataset_id 必须等于主题逻辑数据集，且等于 manifest
     数据集；
   - 每步 capability_id/capability_version 必须精确等于该主题当前绑定，
     并与 Registry manifest.tool_version 一致；
   - S0 每个主题只允许编译器定义的单步计划，额外步骤、跨主题能力替换
     均拒绝。
2. 完整性通过后，按步骤中的真实能力标识取出生产 manifest，复用生产
   Policy 再次校验 Tool 授权、数据集、区域与字段策略 —— 不能只校验
   通用入口自身的权限。

本模块只提供服务与类型；本轮不接入生产 Registry，生产代码不依赖语义层。
"""

import jsonschema
from pydantic import ValidationError

from full_view_agent.application.authorization_scope import DynamicToolArguments
from full_view_agent.application.capability_service import (
    TOOL_INPUT_MODELS,
    PolicyEvaluator,
)
from full_view_agent.application.errors import ResourceNotFound
from full_view_agent.application.fingerprints import canonical_fingerprint
from full_view_agent.application.tool_registry import ToolRegistry
from full_view_agent.domain.models import AuthContext, ContractModel, PolicyDecision
from full_view_agent.semantic.catalog import SemanticCatalog
from full_view_agent.semantic.compiler import SemanticPlan


class ExecutionRecheck(ContractModel):
    allowed: bool
    decisions: tuple[PolicyDecision, ...] = ()
    denial_codes: tuple[str, ...] = ()
    user_message: str | None = None


class ExecutionGuard:
    def __init__(
        self,
        *,
        catalog: SemanticCatalog,
        registry: ToolRegistry,
        policy: PolicyEvaluator,
    ) -> None:
        self._catalog = catalog
        self._registry = registry
        self._policy = policy

    def recheck(
        self,
        plan: SemanticPlan,
        *,
        auth_context: AuthContext,
    ) -> ExecutionRecheck:
        # S0 每个主题只允许编译器定义的单步计划。
        if len(plan.steps) != 1:
            return self._denied(
                "PLAN_STEPS_INVALID",
                f"S0 仅允许单步执行计划，实际 {len(plan.steps)} 步。",
            )
        # 计划完整性复核：陈旧或篡改计划在此 fail closed，不执行 Policy。
        if plan.catalog_version != self._catalog.catalog_version:
            return self._denied(
                "CATALOG_VERSION_MISMATCH",
                f"计划目录版本 {plan.catalog_version} 与当前语义目录版本"
                f" {self._catalog.catalog_version} 不一致，计划已过期。",
            )
        subject = self._catalog.subject(plan.subject)
        if subject is None:
            return self._denied(
                "UNKNOWN_SUBJECT",
                f"计划主题 {plan.subject} 不在当前语义目录中。",
            )
        binding = self._catalog.binding(plan.subject)
        if binding is None:
            return self._denied(
                "BINDING_MISSING",
                f"主题 {plan.subject} 当前没有已验证能力绑定。",
            )
        if plan.logical_dataset_id != subject.logical_dataset_id:
            return self._denied(
                "LOGICAL_DATASET_MISMATCH",
                f"计划逻辑数据集 {plan.logical_dataset_id} 与主题数据集"
                f" {subject.logical_dataset_id} 不一致。",
            )
        step = plan.steps[0]
        if step.capability_id != binding.capability_id:
            return self._denied(
                "CAPABILITY_MISMATCH",
                f"计划步骤能力 {step.capability_id} 与主题 {plan.subject}"
                f" 当前绑定能力 {binding.capability_id} 不一致。",
            )
        try:
            manifest = self._registry.get_manifest(step.capability_id)
        except ResourceNotFound:
            return self._denied(
                "UNKNOWN_CAPABILITY",
                f"绑定能力 {step.capability_id} 未在生产 Registry 注册。",
            )
        if manifest.tool_version != step.capability_version:
            return self._denied(
                "CAPABILITY_VERSION_MISMATCH",
                f"manifest 工具版本 {manifest.tool_version} 与计划版本"
                f" {step.capability_version} 不一致。",
            )
        if plan.semantic_contract_fingerprint is not None:
            if manifest.semantic_contract is None:
                return self._denied(
                    "SEMANTIC_CONTRACT_UNAVAILABLE",
                    "计划依赖的语义合同在精确 Tool 版本中不存在。",
                )
            actual_contract_fingerprint = canonical_fingerprint(
                domain=(
                    f"tool-semantic-contract:{manifest.tool_id}:"
                    f"{manifest.tool_version}"
                ),
                value=manifest.semantic_contract,
            )
            if actual_contract_fingerprint != plan.semantic_contract_fingerprint:
                return self._denied(
                    "SEMANTIC_CONTRACT_MISMATCH",
                    "计划语义合同指纹与 Run 固化 Tool 合同不一致。",
                )
        if manifest.dataset_id != plan.logical_dataset_id:
            return self._denied(
                "LOGICAL_DATASET_MISMATCH",
                f"manifest 数据集 {manifest.dataset_id} 与计划逻辑数据集"
                f" {plan.logical_dataset_id} 不一致。",
            )
        # 完整性通过：按真实主题 Tool 契约校验参数并复用生产 Policy 复核。
        input_model = TOOL_INPUT_MODELS.get(step.capability_id)
        if input_model is None:
            try:
                jsonschema.validate(
                    instance=step.arguments,
                    schema=self._registry.get_input_schema(step.capability_id),
                )
            except (jsonschema.ValidationError, jsonschema.SchemaError):
                return self._denied(
                    "PLAN_ARGUMENTS_INVALID",
                    "计划参数不符合目标能力契约。",
                )
            arguments = DynamicToolArguments(data=step.arguments)
        else:
            try:
                arguments = input_model.model_validate(step.arguments)
            except ValidationError:
                return self._denied(
                    "PLAN_ARGUMENTS_INVALID",
                    "计划参数不符合目标能力契约。",
                )
        decision = self._policy.evaluate(
            manifest=manifest,
            auth_context=auth_context,
            arguments=arguments,
        )
        if decision.decision != "allow":
            return ExecutionRecheck(
                allowed=False,
                decisions=(decision,),
                denial_codes=tuple(decision.reason_codes),
                user_message=decision.user_message,
            )
        return ExecutionRecheck(allowed=True, decisions=(decision,))

    @staticmethod
    def _denied(code: str, user_message: str) -> ExecutionRecheck:
        return ExecutionRecheck(
            allowed=False,
            denial_codes=(code,),
            user_message=user_message,
        )
