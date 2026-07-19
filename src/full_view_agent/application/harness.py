from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from time import monotonic
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


@dataclass(frozen=True)
class HarnessResult:
    summary: str
    state: HarnessState


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

        success_results = [
            r for r in state.tool_results if r.status in ("success", "partial")
        ]
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

        # Case 5: no tool results at all → explicit allowlist only
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
        clock: Callable[[], float] = monotonic,
        tool_call_id_factory: Callable[[], str] | None = None,
    ) -> None:
        self._tool_executor = tool_executor
        self._limits = limits or HarnessLimits()
        self._validator = validator or NonEmptyCompletionValidator()
        self._clock = clock
        self._tool_call_id_factory = tool_call_id_factory or (lambda: new_id("tcl"))

    async def run(
        self,
        *,
        planner: Planner,
        auth_context: AuthContext,
        before_tool_call: BeforeToolCall | None = None,
        after_tool_call: AfterToolCall | None = None,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> HarnessResult:
        state = HarnessState()
        started_at = self._clock()
        previous_call_fingerprint: str | None = None
        repeated_calls = 0
        previous_observation_fingerprint: str | None = None

        while True:
            self._guard_time(started_at)
            if is_cancelled is not None and is_cancelled():
                raise BudgetExceeded("run was cancelled before the next safe checkpoint")
            if state.model_turns >= self._limits.max_model_turns:
                raise BudgetExceeded("maximum model turns exceeded")

            action = await planner.decide(state)
            state = replace(state, model_turns=state.model_turns + 1)
            if isinstance(action, FinishAction):
                if await self._validator.validate(state, action):
                    return HarnessResult(summary=action.summary, state=state)
                state = replace(state, no_progress_count=state.no_progress_count + 1)
                self._guard_no_progress(state)
                continue

            if state.tool_calls >= self._limits.max_tool_calls:
                raise BudgetExceeded("maximum tool calls exceeded")
            call_fingerprint = canonical_fingerprint(
                domain="harness-tool-call",
                value={"tool_id": action.tool_id, "arguments": action.arguments},
            )
            if call_fingerprint == previous_call_fingerprint:
                repeated_calls += 1
            else:
                repeated_calls = 1
            if repeated_calls > self._limits.repeated_call_limit:
                raise LoopDetected("same tool and arguments repeated without progress")
            previous_call_fingerprint = call_fingerprint

            self._guard_time(started_at)
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
            failures = (
                state.consecutive_failures + 1 if result.status == "failed" else 0
            )
            observation_fingerprint = canonical_fingerprint(
                domain="harness-tool-observation",
                value=result.model_dump(mode="json", exclude={"tool_call_id"}),
            )
            no_progress = (
                state.no_progress_count + 1
                if observation_fingerprint == previous_observation_fingerprint
                else 0
            )
            previous_observation_fingerprint = observation_fingerprint
            state = replace(
                state,
                tool_calls=state.tool_calls + 1,
                consecutive_failures=failures,
                no_progress_count=no_progress,
                tool_results=(*state.tool_results, result),
            )
            if failures >= self._limits.max_consecutive_failures:
                raise BudgetExceeded(
                    "maximum consecutive tool failures exceeded",
                    root_cause_code=(
                        result.warnings[0] if result.warnings else "tool_failed"
                    ),
                    root_cause_message=result.summary,
                )
            self._guard_no_progress(state)

    def _guard_time(self, started_at: float) -> None:
        if self._clock() - started_at >= self._limits.max_elapsed_seconds:
            raise BudgetExceeded("maximum elapsed time exceeded")

    def _guard_no_progress(self, state: HarnessState) -> None:
        if state.no_progress_count >= self._limits.max_no_progress:
            raise LoopDetected("maximum no-progress steps exceeded")
