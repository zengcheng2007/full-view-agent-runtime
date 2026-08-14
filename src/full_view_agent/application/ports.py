from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import datetime
from typing import Literal, Protocol, TypeVar, runtime_checkable

from pydantic import SecretStr

from full_view_agent.application.analysis_graph import AnalysisRunOutcome
from full_view_agent.domain.models import (
    AgentEvent,
    AgentMessage,
    AgentRun,
    AgentSession,
    AuthContext,
    CredentialGrant,
    DataResult,
    Evidence,
    FrontendCommand,
    FrontendCommandReceipt,
    LegacyIdentitySnapshot,
    PendingInputRequest,
    Steer,
)

T = TypeVar("T")


class LegacyIdentityPort(Protocol):
    async def resolve(self, raw_token: SecretStr) -> LegacyIdentitySnapshot: ...


class CredentialBroker(Protocol):
    async def issue(
        self,
        *,
        raw_token: SecretStr,
        subject_user_id: str,
        app_id: str,
        run_id: str,
        source_expires_at: datetime,
    ) -> CredentialGrant: ...

    async def resolve(
        self,
        *,
        credential_ref: str,
        subject_user_id: str,
        app_id: str,
        run_id: str,
    ) -> SecretStr: ...

    async def revoke(self, *, credential_ref: str) -> None: ...

    async def revoke_subject(self, *, subject_user_id: str) -> None: ...


class AgentStore(Protocol):
    async def create_session(self, session: AgentSession) -> AgentSession: ...

    async def get_session(
        self,
        *,
        tenant_id: str = "legacy",
        app_id: str = "full_information_view",
        user_id: str,
        session_id: str,
    ) -> AgentSession: ...

    async def list_sessions(
        self,
        *,
        tenant_id: str = "legacy",
        app_id: str = "full_information_view",
        user_id: str,
        status: Literal["active", "archived"] | None = None,
    ) -> list[AgentSession]: ...

    async def update_session(
        self,
        *,
        tenant_id: str = "legacy",
        app_id: str = "full_information_view",
        user_id: str,
        session_id: str,
        title: str | None = None,
        status: Literal["archived"] | None = None,
    ) -> AgentSession: ...

    async def create_run_if_session_idle(
        self,
        *,
        user_id: str,
        session_id: str,
        run: AgentRun,
        input_message: AgentMessage,
    ) -> AgentRun: ...

    async def list_messages(
        self,
        *,
        user_id: str,
        session_id: str,
        tenant_id: str | None = None,
        app_id: str | None = None,
    ) -> list[AgentMessage]: ...

    async def save_message(
        self, *, user_id: str, run_id: str, message: AgentMessage
    ) -> AgentMessage: ...

    async def start_run(self, *, user_id: str, run_id: str) -> AgentRun: ...

    async def get_run(
        self,
        *,
        user_id: str,
        run_id: str,
        tenant_id: str | None = None,
        app_id: str | None = None,
    ) -> AgentRun: ...

    async def list_recoverable_runs(self) -> list[tuple[str, AgentRun]]: ...

    async def save_result(
        self, *, user_id: str, run_id: str, result: DataResult
    ) -> DataResult: ...

    async def save_tool_observation(
        self,
        *,
        user_id: str,
        run_id: str,
        result: DataResult,
        evidence: Evidence,
        commands: tuple[FrontendCommand, ...],
    ) -> tuple[DataResult, Evidence, tuple[FrontendCommand, ...]]: ...

    async def get_result(
        self,
        *,
        user_id: str,
        result_id: str,
        tenant_id: str | None = None,
        app_id: str | None = None,
    ) -> DataResult: ...

    async def get_result_for_run(
        self,
        *,
        user_id: str,
        run_id: str,
        result_id: str,
        tenant_id: str | None = None,
        app_id: str | None = None,
    ) -> DataResult: ...

    async def save_evidence(
        self, *, user_id: str, run_id: str, evidence: Evidence
    ) -> Evidence: ...

    async def get_evidence(
        self,
        *,
        user_id: str,
        evidence_id: str,
        tenant_id: str | None = None,
        app_id: str | None = None,
    ) -> Evidence: ...

    async def get_evidence_for_run(
        self,
        *,
        user_id: str,
        run_id: str,
        evidence_id: str,
        tenant_id: str | None = None,
        app_id: str | None = None,
    ) -> Evidence: ...

    async def save_frontend_command(
        self, *, user_id: str, run_id: str, command: FrontendCommand
    ) -> FrontendCommand: ...

    async def put_frontend_command_receipt(
        self,
        *,
        user_id: str,
        run_id: str,
        command_id: str,
        receipt: FrontendCommandReceipt,
    ) -> FrontendCommandReceipt: ...

    async def cancel_run(self, *, user_id: str, run_id: str) -> AgentRun: ...

    async def wait_for_reauthentication(
        self,
        *,
        user_id: str,
        run_id: str,
        analysis_plan_id: str | None = None,
        analysis_request_id: str | None = None,
    ) -> tuple[AgentRun, PendingInputRequest]: ...

    async def get_pending_input(
        self, *, user_id: str, run_id: str
    ) -> PendingInputRequest: ...

    async def resume_from_input(
        self,
        *,
        user_id: str,
        run_id: str,
        input_request_id: str,
        run_state_version: int,
    ) -> AgentRun: ...

    async def add_steer(self, *, user_id: str, steer: Steer) -> Steer: ...

    async def apply_pending_steers(self, *, user_id: str, run_id: str) -> list[Steer]: ...

    async def complete_run(
        self,
        *,
        user_id: str,
        run_id: str,
        outcome: str,
        completion_reason_code: str,
    ) -> AgentRun: ...

    async def fail_run(
        self, *, user_id: str, run_id: str, completion_reason_code: str
    ) -> AgentRun: ...


class EventPublisher(Protocol):
    async def publish(
        self,
        *,
        event_type: str,
        session_id: str,
        run_id: str,
        data: dict[str, object],
        idempotency_key: str | None = None,
    ) -> AgentEvent: ...


class EventStore(EventPublisher, Protocol):
    async def validate_cursor(
        self, *, run_id: str, after_event_id: str | None
    ) -> None: ...

    def stream(
        self, *, run_id: str, after_event_id: str | None = None
    ) -> AsyncIterator[AgentEvent]: ...


class IdempotencyStore(Protocol):
    async def execute(
        self,
        *,
        user_id: str,
        scope: str,
        key: str,
        request_fingerprint: str,
        operation: Callable[[], Awaitable[T]],
    ) -> tuple[T, bool]: ...


class RunAuthContextStore(Protocol):
    async def put(self, auth_context: AuthContext) -> AuthContext: ...

    async def get(self, *, user_id: str, run_id: str) -> AuthContext: ...
    async def delete(self, *, run_id: str) -> None: ...


class AnalysisOrchestratorPort(Protocol):
    """Executes a trusted server-side analysis plan for an admitted Run.

    The API layer verifies identity, tenant ownership and the Run/Plan
    trusted boundary before calling ``run``; the implementation owns the
    crash-safe graph execution and must never fall back to ad-hoc
    executors. A missing composition is fail-closed at the API layer.
    """

    async def run(
        self,
        *,
        user_id: str,
        session_id: str,
        analysis_run_id: str,
        plan_id: str,
        request_id: str,
        auth_context: AuthContext,
    ) -> AnalysisRunOutcome: ...

    async def resume(
        self,
        *,
        user_id: str,
        session_id: str,
        analysis_run_id: str,
        plan_id: str,
        request_id: str,
        input_request_id: str,
        run_state_version: int,
        auth_context: AuthContext,
    ) -> AnalysisRunOutcome: ...


@runtime_checkable
class OrchestrationPort(Protocol):
    """Framework-neutral orchestration contract.

    Implementations (Native, LangGraph, etc.) drive the full Run
    lifecycle: start/continue execution, handle user input and
    reauthentication, accept steer instructions, and cancel runs.

    Standard events and terminal states are written to the product
    ledger (AgentStore / EventPublisher) – never through framework-
    private channels.  The API layer depends only on this port.
    """

    async def execute(self, *, user_id: str, run_id: str) -> None:
        """Start or continue a Run through the harness loop."""
        ...

    async def cancel(self, *, user_id: str, run_id: str) -> None:
        """Cancel a running or queued Run.

        Must be a no-op (no events, no tool calls) when the Run is
        already in a terminal state (completed / failed / cancelled).
        """
        ...

    async def resume(
        self,
        *,
        user_id: str,
        run_id: str,
        input_request_id: str,
        run_state_version: int,
    ) -> None:
        """Resume a Run that is waiting for user input or reauth.

        Caller (API) must have already verified admission / credentials
        before invoking this method.
        """
        ...

    async def steer(
        self,
        *,
        user_id: str,
        run_id: str,
        client_instance_id: str,
        content: str,
    ) -> Steer:
        """Record a steer instruction and publish event.  Returns Steer."""
        ...

    def schedule(self, *, user_id: str, run_id: str) -> None:
        """Fire-and-forget: schedule execute() as an asyncio task."""
        ...

    async def shutdown(self) -> None:
        """Cancel all running tasks.  Called during app shutdown."""
        ...
