from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from time import time
from typing import Literal, Protocol

from full_view_agent.application.answer_claims import (
    CAPABILITY_SUMMARY,
    CLARIFICATION_SUMMARY,
    DENIAL_SUMMARY,
    FAILURE_SUMMARY,
    UNSUPPORTED_CONSTRAINT_SUMMARY,
    StructuredFinish,
    assess_structured_finish,
)
from full_view_agent.application.answer_grounding import (
    assess_answer_grounding,
    remove_lines_with_numbers,
    remove_lines_with_values,
)
from full_view_agent.application.errors import (
    BudgetExceeded,
    LoopDetected,
    ModelContractError,
    ModelProviderTimeout,
    ModelProviderUnavailable,
)
from full_view_agent.application.fingerprints import canonical_fingerprint
from full_view_agent.application.session_run_service import new_id
from full_view_agent.domain.analysis_intent import AnalysisIntentV1
from full_view_agent.domain.models import AuthContext, ToolResult

ANALYSIS_INTENT_TOOL_ID = "agent.request_regional_analysis"
MODEL_TIMEOUT_WITH_RESULTS_SUMMARY = (
    "查询工具已经执行完成，但模型生成综合说明超时。已保留可查看的验证结果。"
)
MODEL_TOKEN_BUDGET_WITH_RESULTS_SUMMARY = (
    "查询工具已经执行完成，但模型本轮可用 Token 已耗尽。"
    "已保留可查看的验证结果。"
)
MODEL_PROVIDER_UNAVAILABLE_WITH_RESULTS_SUMMARY = (
    "查询工具已经执行完成，但模型服务暂时不可用。已保留可查看的验证结果。"
)


@dataclass(frozen=True)
class HarnessLimits:
    max_model_turns: int = 8
    max_tool_calls: int = 12
    max_consecutive_failures: int = 3
    max_no_progress: int = 3
    max_elapsed_seconds: float = 120.0
    repeated_call_limit: int = 2

    def __post_init__(self) -> None:
        values = (
            self.max_model_turns,
            self.max_tool_calls,
            self.max_consecutive_failures,
            self.max_no_progress,
            self.max_elapsed_seconds,
            self.repeated_call_limit,
        )
        if any(value <= 0 for value in values):
            raise ValueError("all harness limits must be positive")


@dataclass(frozen=True)
class ToolAction:
    tool_id: str
    arguments: dict[str, object]


@dataclass(frozen=True)
class FinishAction:
    summary: str
    structured_finish: StructuredFinish | None = None
    structured_finish_error: str | None = None
    legacy: bool = False
    # Only deterministic server logic may set this. It permits a more precise
    # capability-boundary explanation while model-authored text remains
    # replaced by the fixed safe template.
    server_authored: bool = False
    degraded_reason_code: Literal[
        "model_timeout_with_results",
        "model_token_budget_with_results",
        "model_provider_unavailable_with_results",
    ] | None = None


@dataclass(frozen=True)
class AnalysisIntentAction:
    """Validated model intent that must be handled by a dedicated bridge.

    It is deliberately not a ``ToolAction``: the model may express analysis
    goals and scope, but it may not smuggle an executable capability call into
    the regular Tool pipeline.
    """

    intent: AnalysisIntentV1


HarnessAction = ToolAction | FinishAction | AnalysisIntentAction


@dataclass(frozen=True)
class HarnessState:
    model_turns: int = 0
    tool_calls: int = 0
    consecutive_failures: int = 0
    no_progress_count: int = 0
    tool_results: tuple[ToolResult, ...] = ()
    tool_call_ids: tuple[str, ...] = ()
    tool_actions: tuple[ToolAction, ...] = ()
    inherited_result_ids: tuple[str, ...] = ()
    inherited_evidence_ids: tuple[str, ...] = ()
    completion_feedback: str | None = None
    completion_feedback_code: str | None = None
    completion_revision_count: int = 0


@dataclass(frozen=True)
class HarnessResult:
    summary: str
    state: HarnessState
    # Historical Result cards are an explicit completion decision, never an
    # automatic side effect of merely having inherited grounding in context.
    use_inherited_references: bool = False
    outcome: Literal["success", "partial"] = "success"
    completion_reason_code: str = "goal_completed"


@dataclass(frozen=True)
class HarnessControl:
    """Framework-neutral control state for one bounded agent iteration."""

    state: HarnessState
    started_at: float
    previous_call_fingerprint: str | None = None
    repeated_calls: int = 0
    previous_observation_fingerprint: str | None = None


@dataclass(frozen=True)
class HarnessToolExecution:
    """A Tool result awaiting observation and validation by the Harness."""

    action: ToolAction
    tool_call_id: str
    result: ToolResult
    call_fingerprint: str
    repeated_calls: int


def default_tool_call_fingerprint(action: ToolAction) -> str:
    """Canonical loop-detection fingerprint of a raw tool call."""
    return canonical_fingerprint(
        domain="harness-tool-call",
        value={"tool_id": action.tool_id, "arguments": action.arguments},
    )


class ToolCallFingerprinter(Protocol):
    """Strategy for computing the loop-detection fingerprint of a call.

    The default keeps the historical raw-action fingerprint. The S1-A
    semantic wiring resolves ``governance.semantic_query`` to its canonical
    action first, so synonymous phrasings converge and direct/semantic
    duplicates share one repeated-call counter.
    """

    def fingerprint(self, action: ToolAction, *, auth_context: AuthContext) -> str: ...


class DefaultToolCallFingerprinter:
    def fingerprint(self, action: ToolAction, *, auth_context: AuthContext) -> str:
        del auth_context
        return default_tool_call_fingerprint(action)


class Planner(Protocol):
    async def decide(self, state: HarnessState) -> HarnessAction: ...


class HarnessToolExecutor(Protocol):
    async def execute(
        self,
        *,
        tool_call_id: str,
        tool_id: str,
        raw_arguments: dict[str, object],
        auth_context: AuthContext,
    ) -> ToolResult: ...


class CompletionValidator(Protocol):
    async def validate(self, state: HarnessState, action: FinishAction) -> bool: ...


@dataclass(frozen=True)
class CompletionAssessment:
    status: Literal["accept", "revise", "reject"]
    reason_code: str | None = None
    feedback: str | None = None
    safe_summary: str | None = None


class NonEmptyCompletionValidator:
    async def validate(self, state: HarnessState, action: FinishAction) -> bool:
        del state
        return bool(action.summary.strip())


class DeterministicCompletionValidator:
    """Validate completion by checking structural conditions, not just non-empty text.

    Rules (evaluated in order):
    1. Empty summary → reject.
    2. At least one success/partial tool result → accept (model summarises real data).
    3. Only denied tool results → summary must acknowledge denial, not fabricate data.
       Reject if the summary contains hallucinated numbers or success claims.
    4. Only failed tool results → summary must acknowledge failure.
       Reject if the summary contains hallucinated numbers or success claims.
    5. No tool results → accept only if the summary matches an explicit allowlist of
       non-data-response types (capability description, clarification request).

    Hallucination detection: any digit sequence of 2+ characters in the summary
    is treated as a fabricated metric when there is no success/partial result.
    """

    import re as _re

    _NUMBER_RE = _re.compile(r"\d{2,}")
    _UNSUPPORTED_INFERENCE_PATTERNS = (
        "分布均匀",
        "较为均匀",
        "可能反映",
        "可能说明",
        "可以推测",
        "由此推测",
        "原因可能",
        "这表明",
        "这说明",
        "导致",
    )
    _UNSUPPORTED_INFERENCE_FEEDBACK = (
        "回答包含无证据推断；请仅保留已验证事实和可复算计算。"
    )
    _UNSUPPORTED_NUMBER_FEEDBACK = (
        "回答包含无法回指 Result 的数字；请只引用原始事实或可复算计算。"
    )
    _GROUNDING_FEEDBACK = {
        "unsupported_number": _UNSUPPORTED_NUMBER_FEEDBACK,
        "unsupported_area": "回答包含无法回指 Result 的区域；请仅引用查询结果中的区域。",
        "unsupported_object": "回答包含无法回指 Result 的对象；请仅引用查询结果中的对象。",
        "unsupported_judgement": (
            "回答中的最大、最小、并列或相同判断无法由 Result 复算；请修正判断。"
        ),
    }
    _SAFE_STOP_SUMMARY = (
        "抱歉，当前回答仍包含无法由查询结果核验的内容，已停止生成结论。"
    )

    ALLOWED_NO_RESULT_PATTERNS = (
        "我可以",
        "我能够",
        "当前支持",
        "当前能力",
        "请提供",
        "请补充",
        "请告诉我",
        "需要确认",
        "需要选择",
        "请选择",
        "无法",
        "没有找到",
        "不存在",
        "不在授权",
        "不在",
        "不支持",
        "抱歉",
        "不能",
    )

    async def validate(self, state: HarnessState, action: FinishAction) -> bool:
        return (await self.assess(state, action)).status == "accept"

    async def assess(
        self, state: HarnessState, action: FinishAction
    ) -> CompletionAssessment:
        summary = action.summary.strip()
        if not summary:
            return CompletionAssessment(status="reject", reason_code="empty_summary")

        success_results = [r for r in state.tool_results if r.status in ("success", "partial")]
        denied_results = [r for r in state.tool_results if r.status == "denied"]
        failed_results = [r for r in state.tool_results if r.status == "failed"]

        if action.degraded_reason_code is not None:
            expected_summary = {
                "model_timeout_with_results": MODEL_TIMEOUT_WITH_RESULTS_SUMMARY,
                "model_token_budget_with_results": (
                    MODEL_TOKEN_BUDGET_WITH_RESULTS_SUMMARY
                ),
                "model_provider_unavailable_with_results": (
                    MODEL_PROVIDER_UNAVAILABLE_WITH_RESULTS_SUMMARY
                ),
            }.get(action.degraded_reason_code)
            accepted = bool(
                action.server_authored
                and expected_summary is not None
                and summary == expected_summary
                and success_results
            )
            return CompletionAssessment(
                status="accept" if accepted else "reject",
                reason_code=(
                    "model_timeout_with_results"
                    if accepted
                    else "invalid_degraded_completion"
                ),
            )
        if action.structured_finish_error is not None:
            return CompletionAssessment(
                status="revise",
                reason_code=action.structured_finish_error,
                feedback=(
                    "结构化完成参数无效；请按完成工具 schema 补齐 Result 绑定、"
                    "行、字段、运算和值。"
                ),
            )

        # Successful data does not authorize unsupported causal or
        # source-quality inferences.
        if success_results:
            if action.structured_finish is not None:
                claim_assessment = assess_structured_finish(
                    action.structured_finish, tuple(success_results)
                )
                return CompletionAssessment(
                    status="accept" if claim_assessment.accepted else "revise",
                    reason_code=claim_assessment.reason_code,
                    feedback=(
                        None
                        if claim_assessment.accepted
                        else "结构化事实声明无法回指查询结果，请修正来源、行、字段、运算和值。"
                    ),
                    safe_summary=claim_assessment.rendered_summary,
                )
            if not action.legacy:
                return CompletionAssessment(
                    status="revise",
                    reason_code="structured_claims_required",
                    feedback=(
                        "已有成功查询结果；最终回答必须调用结构化完成工具，"
                        "提交可核验 claims，或使用 reference_only。"
                    ),
                )
            grounding = assess_answer_grounding(summary, tuple(success_results))
            if grounding.reason_code != "grounded":
                return CompletionAssessment(
                    status="revise",
                    reason_code=grounding.reason_code,
                    feedback=self._GROUNDING_FEEDBACK[grounding.reason_code],
                    safe_summary=(
                        (
                            remove_lines_with_numbers(
                                summary, set(grounding.unsupported_values)
                            )
                            if grounding.reason_code == "unsupported_number"
                            else remove_lines_with_values(
                                summary, set(grounding.unsupported_values)
                            )
                        )
                        or None
                    ),
                )
            safe_summary = self._remove_unsupported_inference_lines(summary)
            if safe_summary != summary:
                return CompletionAssessment(
                    status="revise",
                    reason_code="unsupported_inference",
                    feedback=self._UNSUPPORTED_INFERENCE_FEEDBACK,
                    safe_summary=safe_summary or None,
                )
            return CompletionAssessment(status="accept")

        if action.structured_finish is not None:
            finish_kind = action.structured_finish.kind
            has_inherited_reference = bool(
                state.inherited_result_ids and state.inherited_evidence_ids
            )
            non_data_summary = {
                "capability": CAPABILITY_SUMMARY,
                "clarification": CLARIFICATION_SUMMARY,
                "denial": DENIAL_SUMMARY,
                "failure": FAILURE_SUMMARY,
            }.get(finish_kind)
            if finish_kind in {"capability", "clarification"}:
                # Historical grounding belongs to earlier runs. It must not
                # force an unrelated unsupported/clarification response to
                # masquerade as a reference-only follow-up.
                accepted = not state.tool_results
                return CompletionAssessment(
                    status="accept" if accepted else "revise",
                    reason_code=(
                        finish_kind if accepted else f"structured_{finish_kind}_state_mismatch"
                    ),
                    safe_summary=(
                        action.summary
                        if accepted
                        and finish_kind == "capability"
                        and action.structured_finish.limitations
                        == ["unsupported_requested_constraint"]
                        and action.server_authored
                        else UNSUPPORTED_CONSTRAINT_SUMMARY
                        if accepted
                        and finish_kind == "capability"
                        and action.structured_finish.limitations
                        == ["unsupported_requested_constraint"]
                        else non_data_summary if accepted else None
                    ),
                    feedback=(
                        None
                        if accepted
                        else "当前已有查询状态，不能用能力说明或参数澄清替代结果处理。"
                    ),
                )
            if finish_kind == "denial":
                accepted = bool(denied_results) and not failed_results
                return CompletionAssessment(
                    status="accept" if accepted else "revise",
                    reason_code=(
                        "denial" if accepted else "structured_denial_state_mismatch"
                    ),
                    safe_summary=DENIAL_SUMMARY if accepted else None,
                    feedback=None if accepted else "当前没有匹配的权限拒绝结果。",
                )
            if finish_kind == "failure":
                accepted = bool(failed_results)
                return CompletionAssessment(
                    status="accept" if accepted else "revise",
                    reason_code=(
                        "failure" if accepted else "structured_failure_state_mismatch"
                    ),
                    safe_summary=FAILURE_SUMMARY if accepted else None,
                    feedback=None if accepted else "当前没有匹配的执行失败结果。",
                )
            claim_assessment = assess_structured_finish(
                action.structured_finish,
                (),
                has_reference=has_inherited_reference,
            )
            return CompletionAssessment(
                status="accept" if claim_assessment.accepted else "revise",
                reason_code=claim_assessment.reason_code,
                feedback=(
                    None
                    if claim_assessment.accepted
                    else (
                        "当前没有可用于核验 claims 的已加载结果；"
                        "如仅需引用已有结果，请使用 reference_only。"
                    )
                ),
                safe_summary=claim_assessment.rendered_summary,
            )

        if not action.legacy:
            return CompletionAssessment(
                status="revise",
                reason_code="structured_finish_required",
                feedback=(
                    "最终回答必须调用结构化完成工具；业务事实、能力说明、参数澄清、"
                    "权限拒绝和执行失败均不得使用普通文本完成。"
                ),
            )

        has_fabricated_number = bool(self._NUMBER_RE.search(summary))

        # Case 3: denied results → must acknowledge denial, no fabricated data
        if denied_results and not success_results and not failed_results:
            accepted = not has_fabricated_number
            return CompletionAssessment(
                status="accept" if accepted else "reject",
                reason_code=None if accepted else "fabricated_number",
            )

        # Case 4: only failures → must acknowledge failure, no fabricated data
        if failed_results:
            accepted = not has_fabricated_number
            return CompletionAssessment(
                status="accept" if accepted else "reject",
                reason_code=None if accepted else "fabricated_number",
            )

        # A follow-up may transform a result already verified and persisted in
        # the same owned session. The orchestrator only populates these IDs
        # after reloading both the result and its evidence from the store.
        if state.inherited_result_ids and state.inherited_evidence_ids:
            return CompletionAssessment(
                status="revise",
                reason_code="structured_claims_required",
                feedback=(
                    "历史结果尚未加载为可复算数据；不得直接复述事实，"
                    "请使用结构化完成工具的 reference_only。"
                ),
            )

        # Case 5: no current or inherited results → explicit allowlist only
        accepted = any(p in summary for p in self.ALLOWED_NO_RESULT_PATTERNS)
        return CompletionAssessment(
            status="accept" if accepted else "reject",
            reason_code=None if accepted else "ungrounded_no_result",
        )

    def _remove_unsupported_inference_lines(self, summary: str) -> str:
        return "\n".join(
            line
            for line in summary.splitlines()
            if not any(
                pattern in line
                for pattern in self._UNSUPPORTED_INFERENCE_PATTERNS
            )
        ).strip()


BeforeToolCall = Callable[[ToolAction, str], Awaitable[None]]
AfterToolCall = Callable[[ToolResult], Awaitable[None]]


class AgentHarness:
    """Server-side controller for bounded model/tool execution loops."""

    def __init__(
        self,
        *,
        tool_executor: HarnessToolExecutor,
        limits: HarnessLimits | None = None,
        validator: CompletionValidator | None = None,
        clock: Callable[[], float] = time,
        tool_call_id_factory: Callable[[], str] | None = None,
        call_fingerprinter: ToolCallFingerprinter | None = None,
    ) -> None:
        self._tool_executor = tool_executor
        self._limits = limits or HarnessLimits()
        self._validator = validator or NonEmptyCompletionValidator()
        self._clock = clock
        self._tool_call_id_factory = tool_call_id_factory or (lambda: new_id("tcl"))
        self._call_fingerprinter = call_fingerprinter or DefaultToolCallFingerprinter()

    @property
    def model_turn_limit(self) -> int:
        """Expose the framework-neutral action bound to orchestration adapters."""

        return self._limits.max_model_turns

    def begin(
        self,
        *,
        inherited_result_ids: tuple[str, ...] = (),
        inherited_evidence_ids: tuple[str, ...] = (),
    ) -> HarnessControl:
        return HarnessControl(
            state=HarnessState(
                inherited_result_ids=inherited_result_ids,
                inherited_evidence_ids=inherited_evidence_ids,
            ),
            started_at=self._clock(),
        )

    async def plan_action_once(
        self, *, planner: Planner, control: HarnessControl
    ) -> tuple[HarnessControl, HarnessAction]:
        self._guard_time(control.started_at)
        if control.state.model_turns >= self._limits.max_model_turns:
            raise BudgetExceeded("maximum model turns exceeded")
        try:
            action = await planner.decide(control.state)
        except (ModelProviderTimeout, ModelProviderUnavailable, BudgetExceeded) as exc:
            if isinstance(exc, BudgetExceeded) and str(exc) != (
                "model token budget exceeded"
            ):
                raise
            has_verified_result = any(
                result.status in {"success", "partial"}
                for result in control.state.tool_results
            )
            if not has_verified_result:
                raise
            token_budget_exhausted = isinstance(exc, BudgetExceeded)
            provider_unavailable = isinstance(exc, ModelProviderUnavailable)
            action = FinishAction(
                summary=(
                    MODEL_TOKEN_BUDGET_WITH_RESULTS_SUMMARY
                    if token_budget_exhausted
                    else MODEL_PROVIDER_UNAVAILABLE_WITH_RESULTS_SUMMARY
                    if provider_unavailable
                    else MODEL_TIMEOUT_WITH_RESULTS_SUMMARY
                ),
                legacy=True,
                server_authored=True,
                degraded_reason_code=(
                    "model_token_budget_with_results"
                    if token_budget_exhausted
                    else "model_provider_unavailable_with_results"
                    if provider_unavailable
                    else "model_timeout_with_results"
                ),
            )
        if isinstance(action, AnalysisIntentAction):
            # The ordinary Harness has no trusted intent-compilation/execution
            # bridge yet. Reject before checkpoints, hooks, or adapters can
            # observe the action. A later graph node may consume this action
            # explicitly, but it must never fall through to Tool execution.
            raise ModelContractError(
                "analysis intent execution bridge is not configured"
            )
        state = replace(control.state, model_turns=control.state.model_turns + 1)
        return replace(control, state=state), action

    async def validate_once(
        self,
        *,
        action: HarnessAction,
        control: HarnessControl,
    ) -> tuple[HarnessControl, str | None]:
        if isinstance(action, FinishAction):
            assess = getattr(self._validator, "assess", None)
            assessment = (
                await assess(control.state, action)
                if assess is not None
                else CompletionAssessment(
                    status=(
                        "accept"
                        if await self._validator.validate(control.state, action)
                        else "reject"
                    )
                )
            )
            if assessment.status == "accept":
                return control, assessment.safe_summary or action.summary
            if control.state.completion_revision_count > 0:
                if assessment.safe_summary:
                    # This text was produced by deterministic server logic, not by the
                    # model. Re-evaluate it through the retired text gate explicitly.
                    safe_assessment = await self._assess_completion(
                        control.state,
                        FinishAction(summary=assessment.safe_summary, legacy=True),
                    )
                    if safe_assessment.status == "accept":
                        return control, assessment.safe_summary
                return control, DeterministicCompletionValidator._SAFE_STOP_SUMMARY
            state = replace(
                control.state,
                no_progress_count=(
                    control.state.no_progress_count
                    if assessment.status == "revise"
                    else control.state.no_progress_count + 1
                ),
                completion_feedback=assessment.feedback,
                completion_feedback_code=assessment.reason_code,
                completion_revision_count=(
                    control.state.completion_revision_count + 1
                    if assessment.status == "revise"
                    else control.state.completion_revision_count
                ),
            )
            self._guard_no_progress(state)
            return replace(control, state=state), None

        state = control.state
        if state.consecutive_failures >= self._limits.max_consecutive_failures:
            result = state.tool_results[-1]
            raise BudgetExceeded(
                "maximum consecutive tool failures exceeded",
                root_cause_code=result.warnings[0] if result.warnings else "tool_failed",
                root_cause_message=result.summary,
            )
        self._guard_no_progress(state)
        return control, None

    async def _assess_completion(
        self, state: HarnessState, action: FinishAction
    ) -> CompletionAssessment:
        assess = getattr(self._validator, "assess", None)
        if assess is not None:
            return await assess(state, action)
        return CompletionAssessment(
            status=(
                "accept"
                if await self._validator.validate(state, action)
                else "reject"
            )
        )

    async def plan_once(
        self, *, planner: Planner, control: HarnessControl
    ) -> tuple[HarnessControl, ToolAction | None, str | None]:
        control, action = await self.plan_action_once(planner=planner, control=control)
        if isinstance(action, ToolAction):
            return control, action, None
        if not isinstance(action, FinishAction):
            raise ModelContractError("unsupported Harness action")
        control, summary = await self.validate_once(action=action, control=control)
        return control, None, summary

    async def authorize_and_execute_once(
        self,
        *,
        action: ToolAction,
        auth_context: AuthContext,
        control: HarnessControl,
        before_tool_call: BeforeToolCall | None = None,
        after_tool_call: AfterToolCall | None = None,
    ) -> HarnessToolExecution:
        if control.state.tool_calls >= self._limits.max_tool_calls:
            raise BudgetExceeded("maximum tool calls exceeded")
        fingerprint = self._call_fingerprinter.fingerprint(
            action, auth_context=auth_context
        )
        repeated = (
            control.repeated_calls + 1 if fingerprint == control.previous_call_fingerprint else 1
        )
        if repeated > self._limits.repeated_call_limit:
            raise LoopDetected("same tool and arguments repeated without progress")
        self._guard_time(control.started_at)
        tool_call_id = self._tool_call_id_factory()
        if before_tool_call is not None:
            await before_tool_call(action, tool_call_id)
        result = await self._tool_executor.execute(
            tool_call_id=tool_call_id,
            tool_id=action.tool_id,
            raw_arguments=action.arguments,
            auth_context=auth_context,
        )
        if after_tool_call is not None:
            await after_tool_call(result)
        return HarnessToolExecution(
            action=action,
            tool_call_id=tool_call_id,
            result=result,
            call_fingerprint=fingerprint,
            repeated_calls=repeated,
        )

    def observe_once(
        self,
        *,
        execution: HarnessToolExecution,
        control: HarnessControl,
    ) -> HarnessControl:
        result = execution.result
        observation = canonical_fingerprint(
            domain="harness-tool-observation",
            value=result.model_dump(mode="json", exclude={"tool_call_id"}),
        )
        no_progress = (
            control.state.no_progress_count + 1
            if observation == control.previous_observation_fingerprint
            else 0
        )
        state = replace(
            control.state,
            tool_calls=control.state.tool_calls + 1,
            consecutive_failures=control.state.consecutive_failures + 1
            if result.status == "failed"
            else 0,
            no_progress_count=no_progress,
            tool_results=(*control.state.tool_results, result),
            tool_call_ids=(*control.state.tool_call_ids, execution.tool_call_id),
            tool_actions=(*control.state.tool_actions, execution.action),
        )
        return HarnessControl(
            state=state,
            started_at=control.started_at,
            previous_call_fingerprint=execution.call_fingerprint,
            repeated_calls=execution.repeated_calls,
            previous_observation_fingerprint=observation,
        )

    async def execute_once(
        self,
        *,
        action: ToolAction,
        auth_context: AuthContext,
        control: HarnessControl,
        before_tool_call: BeforeToolCall | None = None,
        after_tool_call: AfterToolCall | None = None,
    ) -> HarnessControl:
        execution = await self.authorize_and_execute_once(
            action=action,
            auth_context=auth_context,
            control=control,
            before_tool_call=before_tool_call,
            after_tool_call=after_tool_call,
        )
        control = self.observe_once(execution=execution, control=control)
        control, _summary = await self.validate_once(action=action, control=control)
        return control

    async def run(
        self,
        *,
        planner: Planner,
        auth_context: AuthContext,
        before_tool_call: BeforeToolCall | None = None,
        after_tool_call: AfterToolCall | None = None,
        is_cancelled: Callable[[], bool] | None = None,
        inherited_result_ids: tuple[str, ...] = (),
        inherited_evidence_ids: tuple[str, ...] = (),
    ) -> HarnessResult:
        control = self.begin(
            inherited_result_ids=inherited_result_ids,
            inherited_evidence_ids=inherited_evidence_ids,
        )
        while True:
            if is_cancelled is not None and is_cancelled():
                raise BudgetExceeded("run was cancelled before the next safe checkpoint")
            control, action = await self.plan_action_once(
                planner=planner,
                control=control,
            )
            if isinstance(action, FinishAction):
                control, summary = await self.validate_once(
                    action=action,
                    control=control,
                )
                if summary is not None:
                    return HarnessResult(
                        summary=summary,
                        state=control.state,
                        use_inherited_references=(
                            action.structured_finish is not None
                            and action.structured_finish.kind == "reference_only"
                        ),
                        outcome=(
                            "partial" if action.degraded_reason_code else "success"
                        ),
                        completion_reason_code=(
                            action.degraded_reason_code or "goal_completed"
                        ),
                    )
                continue
            if not isinstance(action, ToolAction):
                raise ModelContractError("unsupported Harness action")
            execution = await self.authorize_and_execute_once(
                action=action,
                auth_context=auth_context,
                control=control,
                before_tool_call=before_tool_call,
                after_tool_call=after_tool_call,
            )
            control = self.observe_once(execution=execution, control=control)
            control, _summary = await self.validate_once(
                action=action,
                control=control,
            )

    def _guard_time(self, started_at: float) -> None:
        elapsed = self._clock() - started_at
        if elapsed < 0:
            raise BudgetExceeded("clock moved backwards during run")
        if elapsed >= self._limits.max_elapsed_seconds:
            raise BudgetExceeded("maximum elapsed time exceeded")

    def _guard_no_progress(self, state: HarnessState) -> None:
        if state.no_progress_count >= self._limits.max_no_progress:
            raise LoopDetected("maximum no-progress steps exceeded")
