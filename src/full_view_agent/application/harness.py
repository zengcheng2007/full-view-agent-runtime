from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from time import time
from typing import Protocol

from full_view_agent.application.errors import BudgetExceeded, LoopDetected
from full_view_agent.application.fingerprints import canonical_fingerprint
from full_view_agent.application.session_run_service import new_id
from full_view_agent.domain.models import AuthContext, ToolResult


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


HarnessAction = ToolAction | FinishAction


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


@dataclass(frozen=True)
class HarnessResult:
    summary: str
    state: HarnessState


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
        summary = action.summary.strip()
        if not summary:
            return False

        success_results = [r for r in state.tool_results if r.status in ("success", "partial")]
        denied_results = [r for r in state.tool_results if r.status == "denied"]
        failed_results = [r for r in state.tool_results if r.status == "failed"]

        # Case 2: has real data → accept
        if success_results:
            return True

        has_fabricated_number = bool(self._NUMBER_RE.search(summary))

        # Case 3: denied results → must acknowledge denial, no fabricated data
        if denied_results and not success_results and not failed_results:
            return not has_fabricated_number

        # Case 4: only failures → must acknowledge failure, no fabricated data
        if failed_results:
            return not has_fabricated_number

        # A follow-up may transform a result already verified and persisted in
        # the same owned session. The orchestrator only populates these IDs
        # after reloading both the result and its evidence from the store.
        if state.inherited_result_ids and state.inherited_evidence_ids:
            return True

        # Case 5: no current or inherited results → explicit allowlist only
        return any(p in summary for p in self.ALLOWED_NO_RESULT_PATTERNS)


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
    ) -> None:
        self._tool_executor = tool_executor
        self._limits = limits or HarnessLimits()
        self._validator = validator or NonEmptyCompletionValidator()
        self._clock = clock
        self._tool_call_id_factory = tool_call_id_factory or (lambda: new_id("tcl"))

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
        action = await planner.decide(control.state)
        state = replace(control.state, model_turns=control.state.model_turns + 1)
        return replace(control, state=state), action

    async def validate_once(
        self,
        *,
        action: HarnessAction,
        control: HarnessControl,
    ) -> tuple[HarnessControl, str | None]:
        if isinstance(action, FinishAction):
            if await self._validator.validate(control.state, action):
                return control, action.summary
            state = replace(
                control.state,
                no_progress_count=control.state.no_progress_count + 1,
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

    async def plan_once(
        self, *, planner: Planner, control: HarnessControl
    ) -> tuple[HarnessControl, ToolAction | None, str | None]:
        control, action = await self.plan_action_once(planner=planner, control=control)
        if not isinstance(action, FinishAction):
            return control, action, None
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
        fingerprint = canonical_fingerprint(
            domain="harness-tool-call",
            value={"tool_id": action.tool_id, "arguments": action.arguments},
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
                    return HarnessResult(summary=summary, state=control.state)
                continue
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
