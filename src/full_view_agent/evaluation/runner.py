from datetime import UTC, datetime
from typing import Protocol

from full_view_agent.application.capability_service import CapabilityService, ToolAdapter
from full_view_agent.application.context_builder import AgentContextBuilder
from full_view_agent.application.mock_executor import MockRunExecutor
from full_view_agent.application.model_planner import ModelPlannerFactory
from full_view_agent.application.model_provider import ModelProvider
from full_view_agent.application.policy import MinimalPolicyAdapter
from full_view_agent.application.session_run_service import SessionRunService, new_id
from full_view_agent.application.tool_registry import ToolRegistry
from full_view_agent.domain.models import AuthContext, RunCreateRequest
from full_view_agent.evaluation.contracts import (
    EvalCase,
    EvalFinishStep,
    EvalGrade,
    EvalMessageRecord,
    EvalModelRequestRecord,
    EvalTrace,
    GradeValue,
)
from full_view_agent.evaluation.faults import FaultInjectingEvalAdapter
from full_view_agent.evaluation.recording_provider import RecordingModelProvider
from full_view_agent.evaluation.scripted_provider import ScriptedModelProvider
from full_view_agent.infrastructure.event_broker import InMemoryEventBroker
from full_view_agent.infrastructure.governance_adapter import InMemoryGovernanceAdapter
from full_view_agent.infrastructure.memory_store import InMemoryAgentStore


class StaticAuthContextProvider:
    def __init__(self, auth_context: AuthContext) -> None:
        self._auth_context = auth_context

    async def get(self, *, user_id: str, run_id: str) -> AuthContext:
        del user_id, run_id
        return self._auth_context


class EvalEnvironment(Protocol):
    @property
    def adapter(self) -> ToolAdapter: ...

    @property
    def evidence_source_system(self) -> str: ...

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
    def evidence_source_system(self) -> str:
        return "eval_fixture"

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
    ) -> None:
        self._provider = provider
        self._model_provider = model_provider
        self._model_name = model_name
        self._max_total_tokens = max_total_tokens
        self._environment = environment

    async def run(self, case: EvalCase) -> EvalTrace:
        return await self._run(case)

    async def replay(self, case: EvalCase, trace: EvalTrace) -> EvalTrace:
        if case.case_id != trace.case_id:
            raise ValueError("eval case_id does not match replay trace case_id")
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
        registry = ToolRegistry.default()
        use_live_provider = self._provider is not None and not force_scripted
        if use_live_provider:
            assert self._provider is not None
            provider = RecordingModelProvider(self._provider)
        elif case.model_steps:
            provider = ScriptedModelProvider(case.model_steps)
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
        run = await service.create_run(
            user_id=user_id,
            session_id=session.session_id,
            request=_run_request(case),
        )
        auth_context = await environment.build_auth_context(
            case=case,
            user_id=user_id,
            session_id=session.session_id,
            run_id=run.run_id,
        )
        adapter = environment.adapter
        if case.fault is not None:
            adapter = FaultInjectingEvalAdapter(
                delegate=adapter,
                fault=case.fault,
            )
        capability = CapabilityService(
            registry=registry,
            policy=MinimalPolicyAdapter(),
            adapter=adapter,
            auth_context_refresher=_NoopRefresher(),
        )
        executor = MockRunExecutor(
            service=service,
            store=store,
            events=events,
            auth_context_provider=StaticAuthContextProvider(auth_context),
            capability=capability,
            registry=registry,
            planner_factory=ModelPlannerFactory(
                provider=provider,
                context_builder=AgentContextBuilder(store=store, registry=registry),
                max_total_tokens=self._max_total_tokens,
            ),
            evidence_source_system=environment.evidence_source_system,
        )

        await executor.execute(user_id=user_id, run_id=run.run_id)

        terminal = await store.get_run(user_id=user_id, run_id=run.run_id)
        published = await events.list_events(run_id=run.run_id)
        event_types = [event.type for event in published]
        tool_ids = [
            str(event.data["tool_id"])
            for event in published
            if event.type == "tool.started"
        ]
        evidence_ids = sorted(store.evidence)
        grades = _grade(
            case,
            terminal_status=terminal.status,
            outcome=terminal.outcome,
            completion_reason_code=terminal.completion_reason_code,
            tool_ids=tool_ids,
            evidence_count=len(evidence_ids),
            event_types=event_types,
            final_answer=_final_answer(provider.consumed_steps),
        )
        return EvalTrace(
            eval_run_id=new_id("evl"),
            case_id=case.case_id,
            started_at=started_at,
            completed_at=datetime.now(UTC),
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
                        EvalMessageRecord(role=message.role, content=message.content)
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
            event_types=event_types,
            terminal_status=terminal.status,
            outcome=terminal.outcome,
            completion_reason_code=terminal.completion_reason_code,
            tool_ids=tool_ids,
            evidence_ids=evidence_ids,
            grades=grades,
            passed=all(grade.passed for grade in grades),
        )


def _run_request(case: EvalCase) -> RunCreateRequest:
    return RunCreateRequest.model_validate(
        {
            "input": {
                "client_message_id": f"eval-message-{case.case_id}",
                "content": [{"type": "text", "text": case.user_message}],
            },
            "client": {
                "client_instance_id": "eval-runner",
                "frontend_command_schema_versions": ["1.0"],
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
                "field_policy_set": "eval_policy_v1",
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
    case: EvalCase,
    *,
    terminal_status: str,
    outcome: str | None,
    completion_reason_code: str | None,
    tool_ids: list[str],
    evidence_count: int,
    event_types: list[str],
    final_answer: str,
) -> list[EvalGrade]:
    expected = case.expected
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
            "min_evidence_count",
            expected.min_evidence_count,
            evidence_count,
            evidence_count >= expected.min_evidence_count,
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
            "forbidden_answer_substrings",
            expected.forbidden_answer_substrings,
            forbidden_matches,
            not forbidden_matches,
            ),
        ]
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
