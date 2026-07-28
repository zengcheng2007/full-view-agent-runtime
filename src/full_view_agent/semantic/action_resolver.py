"""S1-A：``governance.semantic_query`` 虚拟 Tool 的语义动作解析器。

虚拟 Tool 不是生产 Registry 中的真实能力，而是模型侧的语义规划入口：
模型用受控 SemanticQuerySpec 表达业务语义，Harness 执行层在调用
CapabilityService 之前先经本解析器把它编译为规范 ToolAction。

解析链路（全部 fail closed，结构化拒绝，不静默改写）：

1. 结构解析：``{"spec": SemanticQuerySpec}``，物理命名注入在 Schema 层
   即被 pydantic 拒绝，归一为 ``SEMANTIC_INPUT_INVALID``；
2. S1-A 主题白名单：本期只绑定已真实闭环的 population；housing/event
   保留 Catalog 声明但拒绝解析（``SUBJECT_NOT_BINDABLE``）；
3. 必填筛选强制：Catalog 声明的 ``required_filters`` 缺失即拒绝
   （``REQUIRED_FILTER_MISSING``），不让无筛选查询打到真实 Adapter
   白名单后才失败；
4. Validator：语义与授权反例（未知指标/维度/操作符、越权区域等）；
5. Compiler：编译到已验证能力标识的单步 SemanticPlan；
6. ExecutionGuard：计划完整性 + 按真实主题 Tool 复用生产 Policy 复核
   （catalog/binding/manifest 版本一致，dataset 一致，授权通过）。

解析是无状态纯函数：同一 spec 在任何进程、任何时刻编译出同一规范
动作与指纹；恢复（resume）不携带旧计划，而是按当前 Catalog 与当前
AuthContext 重新解析，版本漂移自然 fail closed。
"""

from dataclasses import dataclass, field
from typing import Literal

from pydantic import ValidationError

from full_view_agent.application.capability_service import PolicyEvaluator
from full_view_agent.application.fingerprints import canonical_fingerprint
from full_view_agent.application.harness import ToolAction
from full_view_agent.application.tool_registry import ToolRegistry
from full_view_agent.domain.models import (
    AuthContext,
    ContractModel,
    EvidenceMetricDefinition,
    PolicyDecision,
    SemanticResultLineage,
)
from full_view_agent.semantic.authorization import SubjectAuthorization
from full_view_agent.semantic.catalog import (
    RequiredFilter,
    SemanticCatalog,
    SubjectDefinition,
)
from full_view_agent.semantic.compiler import SemanticCompiler, SemanticPlan
from full_view_agent.semantic.errors import SemanticQueryRejected
from full_view_agent.semantic.execution_guard import ExecutionGuard, ExecutionRecheck
from full_view_agent.semantic.query_spec import (
    SEMANTIC_SPEC_VERSION,
    SemanticQuerySpec,
)
from full_view_agent.semantic.validator import (
    SemanticValidator,
    Violation,
    ViolationCode,
)

SEMANTIC_QUERY_TOOL_ID = "governance.semantic_query"
SEMANTIC_QUERY_TOOL_VERSION = "1.0.0"

# S1-A 只绑定真实已闭环的独居老人（population）纵向切片；housing/event
# 仅有 Catalog 声明，解析层禁止绑定（见《16》S1 与《19》并行计划）。
S1A_BINDABLE_SUBJECTS: frozenset[str] = frozenset({"population"})

# 授权类违规码：拒绝结果按 denied 归类并进入拒绝审计语义；
# 其余语义错误按 failed 归类，模型可修正 spec 后重试。
AUTHORIZATION_VIOLATION_CODES: frozenset[str] = frozenset(
    {
        ViolationCode.SUBJECT_NOT_ENTITLED.value,
        ViolationCode.DATASET_NOT_AUTHORIZED.value,
        ViolationCode.FIELD_POLICY_NOT_AUTHORIZED.value,
        ViolationCode.AREA_OUT_OF_SCOPE.value,
    }
)


class SemanticQueryInput(ContractModel):
    """模型调用 semantic_query 的输入契约：``{"spec": ...}``。"""

    spec: SemanticQuerySpec


@dataclass(frozen=True)
class ResolvedSemanticAction:
    """解析成功：规范 ToolAction + 语义计划 + 血缘 + 生产复核结果。"""

    spec: SemanticQuerySpec
    plan: SemanticPlan
    lineage: SemanticResultLineage
    recheck: ExecutionRecheck | None = None

    @property
    def canonical_action(self) -> ToolAction:
        step = self.plan.steps[0]
        return ToolAction(tool_id=step.capability_id, arguments=step.arguments)


@dataclass(frozen=True)
class RejectedSemanticAction:
    """语义层拒绝：结构非法、能力不支持或授权不足。"""

    codes: tuple[str, ...]
    user_message: str
    violations: tuple[Violation, ...] = field(default_factory=tuple)

    @property
    def is_authorization_denial(self) -> bool:
        # 全部违规都是授权类时才记为权限拒绝；混合语义错误按 failed，
        # 让模型修正 spec 而不是误记越权。
        return bool(self.codes) and all(
            code in AUTHORIZATION_VIOLATION_CODES for code in self.codes
        )


@dataclass(frozen=True)
class DeniedSemanticAction:
    """生产 Policy / 计划完整性复核拒绝（ExecutionGuard）。"""

    codes: tuple[str, ...]
    user_message: str
    decisions: tuple[PolicyDecision, ...] = ()


SemanticResolution = (
    ResolvedSemanticAction | RejectedSemanticAction | DeniedSemanticAction
)


class SemanticActionResolver:
    """把 semantic_query 原始参数解析为规范 ToolAction（无状态）。"""

    def __init__(
        self,
        *,
        catalog: SemanticCatalog,
        registry: ToolRegistry,
        policy: PolicyEvaluator,
        validator: SemanticValidator | None = None,
        compiler: SemanticCompiler | None = None,
        guard: ExecutionGuard | None = None,
        bindable_subjects: frozenset[str] = S1A_BINDABLE_SUBJECTS,
    ) -> None:
        self._catalog = catalog
        self._validator = validator or SemanticValidator(catalog)
        self._compiler = compiler or SemanticCompiler(catalog, self._validator)
        self._guard = guard or ExecutionGuard(
            catalog=catalog, registry=registry, policy=policy
        )
        self._bindable_subjects = frozenset(bindable_subjects)

    @property
    def catalog(self) -> SemanticCatalog:
        return self._catalog

    @property
    def bindable_subjects(self) -> frozenset[str]:
        return frozenset(self._bindable_subjects)

    def compile_action(
        self,
        raw_arguments: dict[str, object],
        *,
        auth_context: AuthContext,
    ) -> ResolvedSemanticAction | RejectedSemanticAction:
        """parse → 白名单 → 必填筛选 → Validator → Compiler（不含 Policy）。

        供执行链路与循环指纹共用：指纹只需要规范动作的确定性，
        不评估 Policy（Policy 决策含时间边界，与指纹稳定性无关）。
        """
        try:
            parsed = SemanticQueryInput.model_validate(raw_arguments)
        except ValidationError as exc:
            return self._input_invalid(exc)
        spec = parsed.spec

        subject = self._catalog.subject(spec.subject)
        if subject is not None and spec.subject not in self._bindable_subjects:
            return RejectedSemanticAction(
                codes=("SUBJECT_NOT_BINDABLE",),
                user_message=(
                    f"业务主题 {spec.subject} 已在语义目录声明，但当前阶段"
                    f"语义查询入口仅绑定 {sorted(self._bindable_subjects)}；"
                    "该主题请继续使用其专用 Tool。"
                ),
            )

        authorization = SubjectAuthorization.from_auth_context(auth_context)
        if subject is not None:
            missing = self._missing_required_filters(subject, spec)
            if missing:
                clauses = "、".join(
                    f"{item.field} {item.operator} {item.value}" for item in missing
                )
                return RejectedSemanticAction(
                    codes=("REQUIRED_FILTER_MISSING",),
                    user_message=(
                        f"主题 {spec.subject} 的真实数据源仅支持携带必填筛选"
                        f" {clauses} 的查询；请补充该筛选或改为对应专用 Tool。"
                    ),
                )

        try:
            plan = self._compiler.compile(spec, authorization=authorization)
        except SemanticQueryRejected as exc:
            return RejectedSemanticAction(
                codes=tuple(violation.code.value for violation in exc.violations),
                user_message="；".join(
                    violation.message for violation in exc.violations
                ),
                violations=exc.violations,
            )
        lineage = self._build_lineage(spec=spec, plan=plan)
        return ResolvedSemanticAction(spec=spec, plan=plan, lineage=lineage)

    def resolve(
        self,
        raw_arguments: dict[str, object],
        *,
        auth_context: AuthContext,
    ) -> SemanticResolution:
        """compile_action + ExecutionGuard 生产 Policy 复核。"""
        compiled = self.compile_action(raw_arguments, auth_context=auth_context)
        if isinstance(compiled, RejectedSemanticAction):
            return compiled
        recheck = self._guard.recheck(compiled.plan, auth_context=auth_context)
        if not recheck.allowed:
            return DeniedSemanticAction(
                codes=recheck.denial_codes,
                user_message=recheck.user_message or "语义计划未通过执行前复核。",
                decisions=recheck.decisions,
            )
        return ResolvedSemanticAction(
            spec=compiled.spec,
            plan=compiled.plan,
            lineage=compiled.lineage,
            recheck=recheck,
        )

    @staticmethod
    def _input_invalid(exc: ValidationError) -> RejectedSemanticAction:
        invalid_fields = sorted(
            {
                ".".join(str(part) for part in error["loc"])
                for error in exc.errors(include_url=False, include_input=False)
            }
        )
        return RejectedSemanticAction(
            codes=("SEMANTIC_INPUT_INVALID",),
            user_message=(
                "semantic_query 输入不符合受控语义契约，错误字段："
                f"{', '.join(invalid_fields) or 'unknown'}。"
                "只能表达业务语义（subject/metrics/scope/group_by/filters/"
                "output），不得包含物理表名、列名或 URL。"
            ),
        )

    @staticmethod
    def _missing_required_filters(
        subject: SubjectDefinition,
        spec: SemanticQuerySpec,
    ) -> tuple[RequiredFilter, ...]:
        required = subject.required_filters
        if not required:
            return ()
        provided = {
            (query_filter.field, query_filter.operator, query_filter.value)
            for query_filter in spec.filters
        }
        return tuple(
            item
            for item in required
            if (item.field, item.operator, item.value) not in provided
        )

    def _build_lineage(
        self,
        *,
        spec: SemanticQuerySpec,
        plan: SemanticPlan,
    ) -> SemanticResultLineage:
        step = plan.steps[0]
        return SemanticResultLineage(
            virtual_tool_id=SEMANTIC_QUERY_TOOL_ID,
            virtual_tool_version=SEMANTIC_QUERY_TOOL_VERSION,
            spec_version=SEMANTIC_SPEC_VERSION,
            catalog_version=plan.catalog_version,
            subject=plan.subject,
            logical_dataset_id=plan.logical_dataset_id,
            canonical_tool_id=step.capability_id,
            canonical_tool_version=step.capability_version,
            spec_fingerprint=canonical_fingerprint(
                domain=f"semantic-query-spec:{SEMANTIC_SPEC_VERSION}",
                value=spec,
            ),
            plan_fingerprint=canonical_fingerprint(
                domain=f"semantic-plan:{SEMANTIC_SPEC_VERSION}",
                value=plan,
            ),
            area_code=spec.scope.area_code,
            output=_output_label(spec.output),
            metric_definitions=[
                EvidenceMetricDefinition(
                    metric_id=ref.metric_id,
                    definition_version=ref.definition_version,
                )
                for ref in plan.evidence.metric_definitions
            ],
        )


def _output_label(output: Literal["table", "choropleth", "metric_card"]) -> str:
    return str(output)
