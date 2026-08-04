from datetime import UTC, datetime
from typing import Literal, Protocol

from full_view_agent.application.answer_grounding import assess_answer_grounding
from full_view_agent.application.capability_service import ToolAdapter
from full_view_agent.application.context_builder import AgentContextBuilder
from full_view_agent.application.model_planner import ModelPlannerFactory
from full_view_agent.application.model_provider import ModelProvider
from full_view_agent.application.native_orchestrator import NativeOrchestrator
from full_view_agent.application.semantic_wiring import (
    build_semantic_capability_stack,
)
from full_view_agent.application.session_run_service import SessionRunService, new_id
from full_view_agent.application.tool_registry import ToolRegistry
from full_view_agent.domain.models import AuthContext, RunCreateRequest, ToolResult
from full_view_agent.evaluation.contracts import (
    EvalCase,
    EvalExpected,
    EvalFinishStep,
    EvalGrade,
    EvalMessageRecord,
    EvalModelRequestRecord,
    EvalOutboundRequestSummary,
    EvalTrace,
    EvalTurnTrace,
    GradeValue,
)
from full_view_agent.evaluation.faults import FaultInjectingEvalAdapter
from full_view_agent.evaluation.recording_provider import RecordingModelProvider
from full_view_agent.evaluation.scripted_provider import ScriptedModelProvider
from full_view_agent.infrastructure.event_broker import InMemoryEventBroker
from full_view_agent.infrastructure.governance_adapter import InMemoryGovernanceAdapter
from full_view_agent.infrastructure.langgraph_orchestrator import (
    LangGraphOrchestrator,
)
from full_view_agent.infrastructure.memory_store import InMemoryAgentStore


class StaticAuthContextProvider:
    def __init__(self, auth_context: AuthContext) -> None:
        self._auth_context = auth_context

    async def get(self, *, user_id: str, run_id: str) -> AuthContext:
        del user_id, run_id
        return self._auth_context


class EvalEnvironment(Protocol):
    @property
    def tool_registry(self) -> ToolRegistry: ...

    @property
    def adapter(self) -> ToolAdapter: ...

    @property
    def environment_kind(self) -> str: ...

    @property
    def evidence_source_system(self) -> str: ...

    @property
    def outbound_requests(self) -> list[EvalOutboundRequestSummary]: ...

    async def resolve_user_id(self, case: EvalCase) -> str: ...

    async def build_auth_context(
        self,
        *,
        case: EvalCase,
        user_id: str,
        session_id: str,
        run_id: str,
    ) -> AuthContext: ...

    async def aclose(self) -> None: ...


class StaticEvalEnvironment:
    def __init__(self) -> None:
        self._adapter = InMemoryGovernanceAdapter()

    @property
    def adapter(self) -> ToolAdapter:
        return self._adapter

    @property
    def tool_registry(self) -> ToolRegistry:
        return ToolRegistry.default()

    @property
    def environment_kind(self) -> str:
        return "static"

    @property
    def evidence_source_system(self) -> str:
        return "eval_fixture"

    @property
    def outbound_requests(self) -> list[EvalOutboundRequestSummary]:
        return []

    async def resolve_user_id(self, case: EvalCase) -> str:
        return f"eval-user-{case.case_id}"

    async def build_auth_context(
        self,
        *,
        case: EvalCase,
        user_id: str,
        session_id: str,
        run_id: str,
    ) -> AuthContext:
        return _auth_context(case, user_id, session_id, run_id)

    async def aclose(self) -> None:
        return None


class EvalRunner:
    def __init__(
        self,
        *,
        provider: ModelProvider | None = None,
        model_provider: str = "scripted",
        model_name: str = "scripted",
        max_total_tokens: int = 32_000,
        environment: EvalEnvironment | None = None,
        orchestrator: Literal["native", "langgraph"] = "native",
        runtime_version: str = "unknown",
    ) -> None:
        self._provider = provider
        self._model_provider = model_provider
        self._model_name = model_name
        self._max_total_tokens = max_total_tokens
        self._environment = environment
        self._orchestrator = orchestrator
        self._runtime_version = runtime_version

    async def run(self, case: EvalCase) -> EvalTrace:
        return await self._run(case)

    async def replay(self, case: EvalCase, trace: EvalTrace) -> EvalTrace:
        if case.case_id != trace.case_id:
            raise ValueError("eval case_id does not match replay trace case_id")
        if trace.turns:
            expected_turn_count = 1 + len(case.follow_up_turns)
            if len(trace.turns) > expected_turn_count:
                raise ValueError("eval trace turn count does not match eval case")
            # 失败轮会提前终止会话，trace 记录的轮数可能少于用例声明；
            # 只回放实际发生过的轮次，不补跑从未执行的后续追问。
            replay_case = case.model_copy(
                update={
                    "model_steps": trace.turns[0].model_steps,
                    "follow_up_turns": [
                        follow_up.model_copy(
                            update={"model_steps": trace.turns[index].model_steps}
                        )
                        for index, follow_up in enumerate(
                            case.follow_up_turns[: len(trace.turns) - 1],
                            start=1,
                        )
                    ],
                }
            )
        else:
            replay_case = case.model_copy(update={"model_steps": trace.model_steps})
        replayed = await self._run(replay_case, force_scripted=True)
        return replayed.model_copy(
            update={
                "replayed_from_eval_run_id": trace.eval_run_id,
                "model_provider": trace.model_provider,
                "model_name": trace.model_name,
                "prompt_version": trace.prompt_version,
            }
        )

    async def _run(self, case: EvalCase, *, force_scripted: bool = False) -> EvalTrace:
        environment = self._environment or StaticEvalEnvironment()
        try:
            return await self._run_in_environment(
                case,
                environment=environment,
                force_scripted=force_scripted,
            )
        finally:
            await environment.aclose()

    async def _run_in_environment(
        self,
        case: EvalCase,
        *,
        environment: EvalEnvironment,
        force_scripted: bool,
    ) -> EvalTrace:
        started_at = datetime.now(UTC)
        user_id = await environment.resolve_user_id(case)
        store = InMemoryAgentStore()
        events = InMemoryEventBroker()
        service = SessionRunService(store)
        registry = environment.tool_registry
        use_live_provider = self._provider is not None and not force_scripted
        scripted_steps = [
            *case.model_steps,
            *(
                step
                for follow_up in case.follow_up_turns
                for step in follow_up.model_steps
            ),
        ]
        if use_live_provider:
            assert self._provider is not None
            provider = RecordingModelProvider(self._provider)
        elif scripted_steps:
            provider = ScriptedModelProvider(scripted_steps)
        else:
            raise ValueError(
                f"case {case.case_id}: model_steps is empty and no live "
                "model provider configured. Either provide model_steps "
                "or configure a live provider via EvalRunner(provider=...)."
            )

        session = await service.create_session(
            user_id=user_id,
            title=case.description[:200],
        )
        adapter = environment.adapter
        if case.fault is not None:
            adapter = FaultInjectingEvalAdapter(
                delegate=adapter,
                fault=case.fault,
            )
        # S1-A：评测与生产共用同一语义接线；scripted 用例直接调用规范
        # Tool 时执行器透明直通，语义入口用例走解析—复核—规范执行链路。
        stack = build_semantic_capability_stack(
            registry=registry,
            adapter=adapter,
            auth_context_refresher=_NoopRefresher(),
        )
        orchestrator_type = (
            NativeOrchestrator
            if self._orchestrator == "native"
            else LangGraphOrchestrator
        )
        turn_specs = [
            (case.user_message, case.expected),
            *(
                (follow_up.user_message, follow_up.expected)
                for follow_up in case.follow_up_turns
            ),
        ]
        turn_traces: list[EvalTurnTrace] = []
        aggregate_event_types: list[str] = []
        aggregate_tool_ids: list[str] = []
        for turn_index, (user_message, expected) in enumerate(turn_specs, start=1):
            run = await service.create_run(
                user_id=user_id,
                session_id=session.session_id,
                request=_run_request(
                    case,
                    user_message=user_message,
                    turn_index=turn_index,
                ),
            )
            auth_context = await environment.build_auth_context(
                case=case,
                user_id=user_id,
                session_id=session.session_id,
                run_id=run.run_id,
            )
            executor = orchestrator_type(
                service=service,
                store=store,
                events=events,
                auth_context_provider=StaticAuthContextProvider(auth_context),
                capability=stack.capability,
                harness=stack.build_harness(),
                registry=registry,
                planner_factory=ModelPlannerFactory(
                    provider=provider,
                    context_builder=AgentContextBuilder(
                        store=store,
                        registry=registry,
                        semantic_presenter=stack.presenter,
                    ),
                    max_total_tokens=self._max_total_tokens,
                    allow_legacy_finish=not use_live_provider,
                    initial_total_tokens=sum(
                        getattr(step, "total_tokens", 0)
                        for step in provider.consumed_steps
                    ),
                ),
                evidence_source_system=environment.evidence_source_system,
            )
            evidence_before = set(store.evidence)
            results_before = set(store.results)
            model_step_start = len(provider.consumed_steps)
            await executor.execute(user_id=user_id, run_id=run.run_id)

            terminal = await store.get_run(user_id=user_id, run_id=run.run_id)
            published = await events.list_events(run_id=run.run_id)
            event_types = [event.type for event in published]
            tool_ids = [
                str(event.data["tool_id"])
                for event in published
                if event.type == "tool.started"
            ]
            evidence_ids = sorted(set(store.evidence) - evidence_before)
            grounding_results = tuple(
                ToolResult(
                    tool_call_id=f"eval-{result_id}",
                    tool_id="eval.result",
                    tool_version="1.0",
                    status="success",
                    summary="Eval persisted result",
                    data_result=store.results[result_id],
                )
                for result_id in sorted(set(store.results) - results_before)
            )
            turn_model_steps = provider.consumed_steps[model_step_start:]
            final_answer = _assistant_answer(published) or _final_answer(
                turn_model_steps
            )
            grades = _grade(
                expected,
                terminal_status=terminal.status,
                outcome=terminal.outcome,
                completion_reason_code=terminal.completion_reason_code,
                tool_ids=tool_ids,
                evidence_count=len(evidence_ids),
                event_types=event_types,
                final_answer=final_answer,
                grounding_results=grounding_results,
            )
            turn_traces.append(
                EvalTurnTrace(
                    turn_index=turn_index,
                    user_message=user_message,
                    model_steps=turn_model_steps,
                    event_types=event_types,
                    terminal_status=terminal.status,
                    outcome=terminal.outcome,
                    completion_reason_code=terminal.completion_reason_code,
                    tool_ids=tool_ids,
                    evidence_ids=evidence_ids,
                    grades=grades,
                    passed=all(grade.passed for grade in grades),
                )
            )
            aggregate_event_types.extend(event_types)
            aggregate_tool_ids.extend(tool_ids)
            if terminal.status != "completed" or terminal.outcome not in {
                "success",
                "partial",
            }:
                break
        final_turn = turn_traces[-1]
        all_grades = [
            grade.model_copy(
                update={
                    "name": (
                        f"turn_{turn.turn_index}.{grade.name}"
                        if len(turn_traces) > 1
                        else grade.name
                    )
                }
            )
            for turn in turn_traces
            for grade in turn.grades
        ]
        turn_count_passed = len(turn_traces) == len(turn_specs)
        all_grades.append(
            EvalGrade(
                name="turn_count",
                passed=turn_count_passed,
                expected=len(turn_specs),
                actual=len(turn_traces),
            )
        )
        return EvalTrace(
            eval_run_id=new_id("evl"),
            case_id=case.case_id,
            started_at=started_at,
            completed_at=datetime.now(UTC),
            environment_kind=environment.environment_kind,
            evidence_source_system=environment.evidence_source_system,
            runtime_version=self._runtime_version,
            outbound_requests=environment.outbound_requests,
            model_provider=(self._model_provider if use_live_provider else "scripted"),
            model_name=(self._model_name if use_live_provider else "scripted"),
            prompt_version=(
                provider.requests[0].prompt_version
                if provider.requests and provider.requests[0].prompt_version
                else "unknown"
            ),
            model_requests=[
                EvalModelRequestRecord(
                    messages=[
                        EvalMessageRecord(
                            role=message.role,
                            content=message.content,
                            tool_call_id=message.tool_call_id,
                        )
                        for message in request.messages
                    ],
                    tool_ids=[tool.tool_id for tool in request.tools],
                    prompt_version=request.prompt_version,
                )
                for request in provider.requests
            ],
            model_steps=provider.consumed_steps,
            total_tokens=sum(
                getattr(step, "total_tokens", 0) for step in provider.consumed_steps
            ),
            event_types=aggregate_event_types,
            terminal_status=final_turn.terminal_status,
            outcome=final_turn.outcome,
            completion_reason_code=final_turn.completion_reason_code,
            tool_ids=aggregate_tool_ids,
            evidence_ids=sorted(store.evidence),
            grades=all_grades,
            turns=turn_traces,
            passed=turn_count_passed and all(turn.passed for turn in turn_traces),
        )


def _run_request(
    case: EvalCase,
    *,
    user_message: str | None = None,
    turn_index: int = 1,
) -> RunCreateRequest:
    return RunCreateRequest.model_validate(
        {
            "input": {
                "client_message_id": (
                    f"eval-message-{case.case_id}-turn-{turn_index}"
                ),
                "content": [
                    {
                        "type": "text",
                        "text": user_message or case.user_message,
                    }
                ],
            },
            "client": {
                "client_instance_id": "eval-runner",
                "frontend_command_schema_versions": ["1.1"],
                "supported_commands": ["panel.show_table"],
            },
            "mode": "agent",
        }
    )


def _auth_context(
    case: EvalCase,
    user_id: str,
    session_id: str,
    run_id: str,
) -> AuthContext:
    return AuthContext.model_validate(
        {
            "auth_context_id": f"auth-{case.case_id}",
            "auth_context_fingerprint": f"sha256:eval-{case.case_id}",
            "principal": {
                "tenant_id": "eval-tenant",
                "user_id": user_id,
                "org_id": "eval-org",
                "roles": ["governance_analyst"],
            },
            "application": {
                "app_id": "full_information_view",
                "agent_id": "governance_general_agent",
            },
            "entitlements": case.auth.entitlements,
            "data_scopes": {
                "areas": [
                    {"area_code": code, "include_descendants": True}
                    for code in case.auth.area_codes
                ],
                "datasets": case.auth.datasets,
                "field_policy_set": case.auth.field_policy_set,
            },
            "purpose": "interactive_analysis",
            "session_id": session_id,
            "run_id": run_id,
            "credential_ref": "cred-eval-do-not-record",
            "issued_at": "2099-01-01T00:00:00Z",
            "expires_at": "2099-01-01T00:05:00Z",
            "policy_version": "eval-v1",
        }
    )


def _grade(
    expected: EvalExpected,
    *,
    terminal_status: str,
    outcome: str | None,
    completion_reason_code: str | None,
    tool_ids: list[str],
    evidence_count: int,
    event_types: list[str],
    final_answer: str,
    grounding_results: tuple[ToolResult, ...] = (),
) -> list[EvalGrade]:
    forbidden_matches = [
        substring
        for substring in expected.forbidden_answer_substrings
        if substring in final_answer
    ]
    required_matches = [
        substring
        for substring in expected.required_answer_substrings
        if substring in final_answer
    ]
    required_any_matches = [
        substring
        for substring in expected.required_answer_any_substrings
        if substring in final_answer
    ]
    missing_required_tools = [
        tool_id for tool_id in expected.required_tool_ids if tool_id not in tool_ids
    ]
    forbidden_tool_matches = [
        tool_id for tool_id in expected.forbidden_tool_ids if tool_id in tool_ids
    ]
    checks: list[tuple[str, GradeValue, GradeValue, bool]] = [
        (
            "terminal_status",
            expected.terminal_status,
            terminal_status,
            terminal_status == expected.terminal_status,
        ),
    ]
    if expected.acceptable_terminal_variants:
        actual_signature = _terminal_signature(
            outcome=outcome,
            completion_reason_code=completion_reason_code,
            tool_ids=tool_ids,
        )
        acceptable_signatures = [
            _terminal_signature(
                outcome=variant.outcome,
                completion_reason_code=variant.completion_reason_code,
                tool_ids=variant.tool_ids,
            )
            for variant in expected.acceptable_terminal_variants
        ]
        checks.append(
            (
                "terminal_variant",
                " OR ".join(acceptable_signatures),
                actual_signature,
                actual_signature in acceptable_signatures,
            )
        )
    else:
        checks.extend(
            [
                ("outcome", expected.outcome, outcome, outcome == expected.outcome),
                (
                    "completion_reason_code",
                    expected.completion_reason_code,
                    completion_reason_code,
                    completion_reason_code == expected.completion_reason_code,
                ),
                (
                    "tool_ids",
                    expected.tool_ids,
                    tool_ids,
                    (not expected.tool_ids) or tool_ids == expected.tool_ids,
                ),
            ]
        )
    checks.extend(
        [
            (
            "required_tool_ids",
            expected.required_tool_ids,
            missing_required_tools,
            not missing_required_tools,
            ),
            (
            "forbidden_tool_ids",
            expected.forbidden_tool_ids,
            forbidden_tool_matches,
            not forbidden_tool_matches,
            ),
            *(
                [
                    (
                        "max_tool_calls",
                        expected.max_tool_calls,
                        len(tool_ids),
                        len(tool_ids) <= expected.max_tool_calls,
                    )
                ]
                if expected.max_tool_calls is not None
                else []
            ),
            (
            "min_evidence_count",
            expected.min_evidence_count,
            evidence_count,
            evidence_count >= expected.min_evidence_count,
            ),
            *(
                [
                    (
                        "max_evidence_count",
                        expected.max_evidence_count,
                        evidence_count,
                        evidence_count <= expected.max_evidence_count,
                    )
                ]
                if expected.max_evidence_count is not None
                else []
            ),
            (
            "tool_lifecycle_terminal_count",
            len(tool_ids),
            sum(
                event_type in {"tool.completed", "tool.failed"}
                for event_type in event_types
            ),
            len(tool_ids)
            == sum(
                event_type in {"tool.completed", "tool.failed"}
                for event_type in event_types
            ),
            ),
            (
            "required_event_types",
            expected.required_event_types,
            event_types,
            set(expected.required_event_types).issubset(event_types),
            ),
            (
            "required_answer_substrings",
            expected.required_answer_substrings,
            required_matches,
            len(required_matches) == len(expected.required_answer_substrings),
            ),
            (
            "required_answer_any_substrings",
            expected.required_answer_any_substrings,
            required_any_matches,
            (not expected.required_answer_any_substrings)
            or bool(required_any_matches),
            ),
            (
            "forbidden_answer_substrings",
            expected.forbidden_answer_substrings,
            forbidden_matches,
            not forbidden_matches,
            ),
        ]
    )
    if expected.grounding_reason_code is not None:
        actual_grounding = assess_answer_grounding(
            final_answer, grounding_results
        ).reason_code
        checks.append(
            (
                "grounding",
                expected.grounding_reason_code,
                actual_grounding,
                actual_grounding == expected.grounding_reason_code,
            )
        )
    return [
        EvalGrade(name=name, expected=expected_value, actual=actual, passed=passed)
        for name, expected_value, actual, passed in checks
    ]


def _final_answer(steps) -> str:
    for step in reversed(steps):
        if isinstance(step, EvalFinishStep):
            return step.content
    return ""


def _assistant_answer(events) -> str:
    for event in reversed(events):
        if event.type != "assistant.message.completed":
            continue
        message = event.data.get("message")
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        return "\n".join(
            str(item["text"])
            for item in content
            if isinstance(item, dict)
            and item.get("type") == "text"
            and isinstance(item.get("text"), str)
        )
    return ""


def _terminal_signature(
    *,
    outcome: str | None,
    completion_reason_code: str | None,
    tool_ids: list[str],
) -> str:
    return f"{outcome}|{completion_reason_code}|{','.join(tool_ids)}"


class _NoopRefresher:
    async def refresh(self, auth_context: AuthContext) -> AuthContext:
        return auth_context
