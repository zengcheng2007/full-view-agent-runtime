import asyncio
from datetime import UTC, datetime, timedelta
from secrets import token_hex
from typing import Literal

from full_view_agent.application.errors import (
    CommandClientMismatch,
    InputRequestClosed,
    ResourceNotFound,
    RunStateConflict,
    SessionActiveRunConflict,
)
from full_view_agent.application.frontend_commands import merge_receipt
from full_view_agent.domain.models import (
    AgentMessage,
    AgentRun,
    AgentSession,
    DataResult,
    Evidence,
    FrontendCommand,
    FrontendCommandReceipt,
    PendingInputRequest,
    Steer,
)


def _same_observation_result(left: DataResult, right: DataResult) -> bool:
    return left.model_dump(exclude={"created_at", "payload_expires_at"}) == right.model_dump(
        exclude={"created_at", "payload_expires_at"}
    )


def _same_observation_evidence(left: Evidence, right: Evidence) -> bool:
    return left.model_dump(exclude={"retrieved_at"}) == right.model_dump(
        exclude={"retrieved_at"}
    )


def _same_observation_command(
    left: FrontendCommand, right: FrontendCommand
) -> bool:
    excluded = {"issued_at", "expires_at"}
    return left.model_dump(exclude=excluded) == right.model_dump(exclude=excluded)


def _same_result_identity(left: DataResult, right: DataResult) -> bool:
    if left == right:
        return True
    if left.kind != "analysis_report" or right.kind != "analysis_report":
        return False
    excluded = {"created_at", "payload_expires_at"}
    return left.model_dump(exclude=excluded) == right.model_dump(exclude=excluded)


def _owns_resource(
    session: AgentSession | None,
    *,
    user_id: str,
    tenant_id: str | None,
    app_id: str | None,
) -> bool:
    return bool(
        session is not None
        and session.owner_user_id == user_id
        and (tenant_id is None or session.owner_tenant_id == tenant_id)
        and (app_id is None or session.app_id == app_id)
    )


class InMemoryAgentStore:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self.sessions: dict[str, AgentSession] = {}
        self.runs: dict[str, AgentRun] = {}
        self.messages: dict[str, AgentMessage] = {}
        self.steers: dict[str, list[Steer]] = {}
        self.results: dict[str, DataResult] = {}
        self.result_run_ids: dict[str, str] = {}
        self.evidence: dict[str, Evidence] = {}
        self.frontend_commands: dict[str, FrontendCommand] = {}
        self.frontend_command_receipts: dict[
            tuple[str, str], FrontendCommandReceipt
        ] = {}
        self.input_requests: dict[str, PendingInputRequest] = {}

    async def create_session(self, session: AgentSession) -> AgentSession:
        async with self._lock:
            self.sessions[session.session_id] = session
            return session

    async def get_session(
        self,
        *,
        tenant_id: str = "legacy",
        app_id: str = "full_information_view",
        user_id: str,
        session_id: str,
    ) -> AgentSession:
        async with self._lock:
            session = self.sessions.get(session_id)
            if (
                session is None
                or session.owner_tenant_id != tenant_id
                or session.app_id != app_id
                or session.owner_user_id != user_id
            ):
                raise ResourceNotFound("session not found")
            return session

    async def list_sessions(
        self,
        *,
        tenant_id: str = "legacy",
        app_id: str = "full_information_view",
        user_id: str,
        status: Literal["active", "archived"] | None = None,
    ) -> list[AgentSession]:
        async with self._lock:
            return sorted(
                (
                    session
                    for session in self.sessions.values()
                    if session.owner_tenant_id == tenant_id
                    and session.app_id == app_id
                    and session.owner_user_id == user_id
                    and (status is None or session.status == status)
                ),
                key=lambda session: (session.updated_at, session.session_id),
                reverse=True,
            )

    async def update_session(
        self,
        *,
        tenant_id: str = "legacy",
        app_id: str = "full_information_view",
        user_id: str,
        session_id: str,
        title: str | None = None,
        status: Literal["archived"] | None = None,
    ) -> AgentSession:
        async with self._lock:
            session = self.sessions.get(session_id)
            if (
                session is None
                or session.owner_tenant_id != tenant_id
                or session.app_id != app_id
                or session.owner_user_id != user_id
            ):
                raise ResourceNotFound("session not found")
            if status == "archived" and session.active_run_id is not None:
                raise SessionActiveRunConflict(session.active_run_id)
            changes: dict[str, object] = {}
            if title is not None and title != session.title:
                changes["title"] = title
            if status is not None and status != session.status:
                changes["status"] = status
            if not changes:
                return session
            changes.update(
                updated_at=datetime.now(UTC),
                version=session.version + 1,
            )
            updated = session.model_copy(update=changes)
            self.sessions[session_id] = updated
            return updated

    async def create_run_if_session_idle(
        self,
        *,
        user_id: str,
        session_id: str,
        run: AgentRun,
        input_message: AgentMessage,
    ) -> AgentRun:
        async with self._lock:
            session = self.sessions.get(session_id)
            if session is None or session.owner_user_id != user_id:
                raise ResourceNotFound("session not found")
            if session.status == "archived":
                raise RunStateConflict("archived session cannot create runs")
            if session.active_run_id is not None:
                raise SessionActiveRunConflict(session.active_run_id)
            self.runs[run.run_id] = run
            self.messages[input_message.message_id] = input_message
            self.sessions[session_id] = session.model_copy(
                update={
                    "active_run_id": run.run_id,
                    "updated_at": datetime.now(UTC),
                    "version": session.version + 1,
                }
            )
            return run

    async def list_messages(
        self,
        *,
        user_id: str,
        session_id: str,
        tenant_id: str | None = None,
        app_id: str | None = None,
    ) -> list[AgentMessage]:
        async with self._lock:
            session = self.sessions.get(session_id)
            if not _owns_resource(
                session, user_id=user_id, tenant_id=tenant_id, app_id=app_id
            ):
                raise ResourceNotFound("session not found")
            return sorted(
                (
                    message
                    for message in self.messages.values()
                    if message.session_id == session_id
                ),
                key=lambda message: (message.created_at, message.message_id),
            )

    async def save_message(
        self, *, user_id: str, run_id: str, message: AgentMessage
    ) -> AgentMessage:
        async with self._lock:
            run = self.runs.get(run_id)
            session = self.sessions.get(run.session_id) if run is not None else None
            if run is None or session is None or session.owner_user_id != user_id:
                raise ResourceNotFound("run not found")
            existing = self.messages.get(message.message_id)
            if existing is not None:
                if existing != message:
                    raise RunStateConflict(
                        "message identity is already bound differently"
                    )
                return existing
            if (
                run.status != "running"
                or session.active_run_id != run_id
                or message.run_id != run_id
                or message.session_id != run.session_id
            ):
                raise RunStateConflict("messages can only be saved for an active run")
            self.messages[message.message_id] = message
            return message

    async def start_run(self, *, user_id: str, run_id: str) -> AgentRun:
        async with self._lock:
            run = self.runs.get(run_id)
            if run is None:
                raise ResourceNotFound("run not found")
            session = self.sessions.get(run.session_id)
            if session is None or session.owner_user_id != user_id:
                raise ResourceNotFound("run not found")
            if run.status != "queued":
                raise RunStateConflict("only a queued run can be started")
            running = run.model_copy(
                update={
                    "status": "running",
                    "current_phase": "planning",
                    "started_at": datetime.now(UTC),
                }
            )
            self.runs[run_id] = running
            return running

    async def get_run(
        self,
        *,
        user_id: str,
        run_id: str,
        tenant_id: str | None = None,
        app_id: str | None = None,
    ) -> AgentRun:
        async with self._lock:
            run = self.runs.get(run_id)
            if run is None:
                raise ResourceNotFound("run not found")
            session = self.sessions.get(run.session_id)
            if not _owns_resource(
                session, user_id=user_id, tenant_id=tenant_id, app_id=app_id
            ):
                raise ResourceNotFound("run not found")
            return run

    async def list_recoverable_runs(self) -> list[tuple[str, AgentRun]]:
        async with self._lock:
            recoverable: list[tuple[str, AgentRun]] = []
            for run in self.runs.values():
                if run.status not in {"queued", "running", "waiting_input"}:
                    continue
                session = self.sessions.get(run.session_id)
                if session is not None and session.active_run_id == run.run_id:
                    recoverable.append((session.owner_user_id, run))
            return recoverable

    async def save_result(
        self,
        *,
        user_id: str,
        run_id: str,
        result: DataResult,
    ) -> DataResult:
        async with self._lock:
            run = self.runs.get(run_id)
            if run is None:
                raise ResourceNotFound("run not found")
            session = self.sessions.get(run.session_id)
            if session is None or session.owner_user_id != user_id:
                raise ResourceNotFound("run not found")
            if run.status != "running" or session.active_run_id != run_id:
                raise RunStateConflict("results can only be saved for an active run")
            existing = self.results.get(result.result_id)
            existing_run_id = self.result_run_ids.get(result.result_id)
            if existing is not None:
                if existing_run_id != run_id or not _same_result_identity(existing, result):
                    raise RunStateConflict("result identity is already bound differently")
                return existing
            self.results[result.result_id] = result
            self.result_run_ids[result.result_id] = run_id
            return result

    async def save_tool_observation(
        self,
        *,
        user_id: str,
        run_id: str,
        result: DataResult,
        evidence: Evidence,
        commands: tuple[FrontendCommand, ...],
    ) -> tuple[DataResult, Evidence, tuple[FrontendCommand, ...]]:
        """Atomically persist one complete Tool observation in memory."""

        async with self._lock:
            run = self.runs.get(run_id)
            session = self.sessions.get(run.session_id) if run is not None else None
            if session is None or session.owner_user_id != user_id:
                raise ResourceNotFound("run not found")
            if run is None or run.status != "running" or session.active_run_id != run_id:
                raise RunStateConflict("tool observations require an active run")
            if evidence.result_id != result.result_id:
                raise RunStateConflict("evidence result does not match observation result")
            if result.evidence_ids != [evidence.evidence_id]:
                raise RunStateConflict("result evidence reference is inconsistent")
            for command in commands:
                if command.run_id != run_id:
                    raise RunStateConflict("frontend command run does not match observation")
                if command.target_client_instance_id != run.origin_client_instance_id:
                    raise CommandClientMismatch(
                        "frontend command target does not match run client"
                    )

            existing_result = self.results.get(result.result_id)
            existing_run_id = self.result_run_ids.get(result.result_id)
            existing_evidence = self.evidence.get(evidence.evidence_id)
            existing_commands = tuple(
                self.frontend_commands.get(command.command_id) for command in commands
            )
            if existing_result is not None:
                if existing_run_id != run_id or not _same_observation_result(
                    existing_result, result
                ):
                    raise RunStateConflict("result identity is already bound differently")
                if (
                    existing_evidence is None
                    or not _same_observation_evidence(existing_evidence, evidence)
                    or any(command is None for command in existing_commands)
                    or not all(
                        _same_observation_command(stored, requested)
                        for stored, requested in zip(
                            existing_commands, commands, strict=True
                        )
                        if stored is not None
                    )
                ):
                    raise RunStateConflict("observation identity is already bound differently")
                return existing_result, existing_evidence, tuple(
                    command for command in existing_commands if command is not None
                )
            if existing_evidence is not None or any(
                command is not None for command in existing_commands
            ):
                raise RunStateConflict("observation identity is partially occupied")

            self.results[result.result_id] = result
            self.result_run_ids[result.result_id] = run_id
            self.evidence[evidence.evidence_id] = evidence
            for command in commands:
                self.frontend_commands[command.command_id] = command
            return result, evidence, commands

    async def get_result_for_run(
        self,
        *,
        user_id: str,
        run_id: str,
        result_id: str,
        tenant_id: str | None = None,
        app_id: str | None = None,
    ) -> DataResult:
        result = await self.get_result(
            user_id=user_id,
            result_id=result_id,
            tenant_id=tenant_id,
            app_id=app_id,
        )
        async with self._lock:
            if self.result_run_ids.get(result_id) != run_id:
                raise ResourceNotFound("result not found")
        return result

    async def get_result(
        self,
        *,
        user_id: str,
        result_id: str,
        tenant_id: str | None = None,
        app_id: str | None = None,
    ) -> DataResult:
        async with self._lock:
            result = self.results.get(result_id)
            run_id = self.result_run_ids.get(result_id)
            if result is None or run_id is None:
                raise ResourceNotFound("result not found")
            run = self.runs.get(run_id)
            session = self.sessions.get(run.session_id) if run is not None else None
            if not _owns_resource(
                session, user_id=user_id, tenant_id=tenant_id, app_id=app_id
            ):
                raise ResourceNotFound("result not found")
            return result

    async def save_evidence(
        self, *, user_id: str, run_id: str, evidence: Evidence
    ) -> Evidence:
        async with self._lock:
            run = self.runs.get(run_id)
            session = self.sessions.get(run.session_id) if run is not None else None
            if (
                session is None
                or session.owner_user_id != user_id
                or self.result_run_ids.get(evidence.result_id) != run_id
            ):
                raise ResourceNotFound("result not found")
            existing = self.evidence.get(evidence.evidence_id)
            if existing is not None:
                if existing != evidence:
                    raise RunStateConflict(
                        "evidence identity is already bound differently"
                    )
                return existing
            self.evidence[evidence.evidence_id] = evidence
            return evidence

    async def get_evidence(
        self,
        *,
        user_id: str,
        evidence_id: str,
        tenant_id: str | None = None,
        app_id: str | None = None,
    ) -> Evidence:
        async with self._lock:
            evidence = self.evidence.get(evidence_id)
            run_id = (
                self.result_run_ids.get(evidence.result_id)
                if evidence is not None
                else None
            )
            run = self.runs.get(run_id) if run_id is not None else None
            session = self.sessions.get(run.session_id) if run is not None else None
            if evidence is None or not _owns_resource(
                session, user_id=user_id, tenant_id=tenant_id, app_id=app_id
            ):
                raise ResourceNotFound("evidence not found")
            return evidence

    async def get_evidence_for_run(
        self,
        *,
        user_id: str,
        run_id: str,
        evidence_id: str,
        tenant_id: str | None = None,
        app_id: str | None = None,
    ) -> Evidence:
        evidence = await self.get_evidence(
            user_id=user_id,
            evidence_id=evidence_id,
            tenant_id=tenant_id,
            app_id=app_id,
        )
        async with self._lock:
            if self.result_run_ids.get(evidence.result_id) != run_id:
                raise ResourceNotFound("evidence not found")
        return evidence

    async def save_frontend_command(
        self, *, user_id: str, run_id: str, command: FrontendCommand
    ) -> FrontendCommand:
        async with self._lock:
            run = self.runs.get(run_id)
            session = self.sessions.get(run.session_id) if run is not None else None
            if run is None or session is None or session.owner_user_id != user_id:
                raise ResourceNotFound("run not found")
            if run.status != "running" or session.active_run_id != run_id:
                raise RunStateConflict("frontend commands require an active run")
            if command.run_id != run_id:
                raise RunStateConflict("frontend command run does not match request path")
            if command.target_client_instance_id != run.origin_client_instance_id:
                raise CommandClientMismatch("frontend command target does not match run client")
            self.frontend_commands[command.command_id] = command
            return command

    async def put_frontend_command_receipt(
        self,
        *,
        user_id: str,
        run_id: str,
        command_id: str,
        receipt: FrontendCommandReceipt,
    ) -> FrontendCommandReceipt:
        async with self._lock:
            command = self.frontend_commands.get(command_id)
            run = self.runs.get(run_id)
            session = self.sessions.get(run.session_id) if run is not None else None
            if (
                command is None
                or run is None
                or session is None
                or session.owner_user_id != user_id
                or command.run_id != run_id
            ):
                raise ResourceNotFound("frontend command not found")
            if receipt.command_id != command_id:
                raise RunStateConflict("receipt command does not match request path")
            if receipt.client_instance_id != command.target_client_instance_id:
                raise CommandClientMismatch("receipt client does not match command target")
            key = (command_id, receipt.client_instance_id)
            stored = merge_receipt(self.frontend_command_receipts.get(key), receipt)
            self.frontend_command_receipts[key] = stored
            return stored

    async def cancel_run(self, *, user_id: str, run_id: str) -> AgentRun:
        async with self._lock:
            run = self.runs.get(run_id)
            if run is None:
                raise ResourceNotFound("run not found")
            session = self.sessions.get(run.session_id)
            if session is None or session.owner_user_id != user_id:
                raise ResourceNotFound("run not found")
            if run.status == "cancelled":
                return run
            if session.active_run_id != run_id:
                raise RunStateConflict("run is not active")
            if run.status not in {
                "queued",
                "running",
                "waiting_input",
                "waiting_approval",
                "cancelling",
            }:
                raise RunStateConflict("run cannot be cancelled from its current state")
            now = datetime.now(UTC)
            cancelled = run.model_copy(
                update={
                    "status": "cancelled",
                    "outcome": "cancelled",
                    "completion_reason_code": "user_cancelled",
                    "current_phase": "cancelled",
                    "completed_at": now,
                }
            )
            self.runs[run_id] = cancelled
            self.sessions[session.session_id] = session.model_copy(
                update={
                    "active_run_id": None,
                    "updated_at": now,
                    "version": session.version + 1,
                }
            )
            return cancelled

    async def wait_for_reauthentication(
        self,
        *,
        user_id: str,
        run_id: str,
        analysis_plan_id: str | None = None,
        analysis_request_id: str | None = None,
    ) -> tuple[AgentRun, PendingInputRequest]:
        if (analysis_plan_id is None) != (analysis_request_id is None):
            raise RunStateConflict("analysis reauthentication references are incomplete")
        async with self._lock:
            run = self.runs.get(run_id)
            if run is None:
                raise ResourceNotFound("run not found")
            session = self.sessions.get(run.session_id)
            if session is None or session.owner_user_id != user_id:
                raise ResourceNotFound("run not found")
            existing = self.input_requests.get(run_id)
            now = datetime.now(UTC)
            if (
                run.status == "waiting_input"
                and run.waiting_for == "reauth"
                and session.active_run_id == run_id
                and existing is not None
                and existing.kind == "reauth"
                and existing.closed_at is None
                and existing.expires_at > now
                and existing.run_state_version == run.state_version
            ):
                if analysis_plan_id is not None:
                    if existing.analysis_plan_id is None:
                        existing = existing.model_copy(
                            update={
                                "analysis_plan_id": analysis_plan_id,
                                "analysis_request_id": analysis_request_id,
                            }
                        )
                        self.input_requests[run_id] = existing
                    elif (
                        existing.analysis_plan_id != analysis_plan_id
                        or existing.analysis_request_id != analysis_request_id
                    ):
                        raise RunStateConflict(
                            "analysis reauthentication references changed"
                        )
                return run, existing
            renew_expired = (
                run.status == "waiting_input"
                and run.waiting_for == "reauth"
                and existing is not None
                and existing.kind == "reauth"
                and existing.closed_at is None
                and existing.expires_at <= now
                and existing.run_state_version == run.state_version
            )
            if (
                session.active_run_id != run_id
                or (run.status != "running" and not renew_expired)
            ):
                raise RunStateConflict("only an active running run can wait for reauthentication")
            waiting = run.model_copy(
                update={
                    "status": "waiting_input",
                    "current_phase": "waiting_input",
                    "waiting_for": "reauth",
                    "state_version": run.state_version + 1,
                }
            )
            pending = PendingInputRequest(
                input_request_id=f"inreq_{token_hex(13)}",
                run_id=run_id,
                kind="reauth",
                prompt="登录凭据已失效，请重新认证后继续。",
                run_state_version=waiting.state_version,
                expires_at=now + timedelta(minutes=10),
                analysis_plan_id=analysis_plan_id,
                analysis_request_id=analysis_request_id,
            )
            self.runs[run_id] = waiting
            self.input_requests[run_id] = pending
            return waiting, pending

    async def get_pending_input(
        self, *, user_id: str, run_id: str
    ) -> PendingInputRequest:
        async with self._lock:
            run = self.runs.get(run_id)
            session = self.sessions.get(run.session_id) if run is not None else None
            if run is None or session is None or session.owner_user_id != user_id:
                raise ResourceNotFound("run not found")
            pending = self.input_requests.get(run_id)
            if pending is None:
                raise ResourceNotFound("pending input request not found")
            return pending

    async def resume_from_input(
        self,
        *,
        user_id: str,
        run_id: str,
        input_request_id: str,
        run_state_version: int,
    ) -> AgentRun:
        async with self._lock:
            run = self.runs.get(run_id)
            if run is None:
                raise ResourceNotFound("run not found")
            session = self.sessions.get(run.session_id)
            if session is None or session.owner_user_id != user_id:
                raise ResourceNotFound("run not found")
            pending = self.input_requests.get(run_id)
            now = datetime.now(UTC)
            if (
                run.status in {"running", "completed", "failed"}
                and run.waiting_for is None
                and pending is not None
                and pending.kind == "reauth"
                and pending.closed_at is not None
                and pending.input_request_id == input_request_id
                and pending.run_state_version == run_state_version
                and run.state_version in {run_state_version + 1, run_state_version + 2}
            ):
                return run
            if (
                run.status != "waiting_input"
                or run.waiting_for != "reauth"
                or pending is None
                or pending.kind != "reauth"
                or pending.closed_at is not None
                or pending.input_request_id != input_request_id
                or pending.run_state_version != run_state_version
                or run.state_version != run_state_version
                or pending.expires_at <= now
            ):
                raise InputRequestClosed("input request is closed or stale")
            resumed = run.model_copy(
                update={
                    "status": "running",
                    "current_phase": "planning",
                    "waiting_for": None,
                    "state_version": run.state_version + 1,
                }
            )
            self.runs[run_id] = resumed
            self.input_requests[run_id] = pending.model_copy(update={"closed_at": now})
            return resumed

    async def add_steer(self, *, user_id: str, steer: Steer) -> Steer:
        async with self._lock:
            run = self.runs.get(steer.run_id)
            if run is None:
                raise ResourceNotFound("run not found")
            session = self.sessions.get(run.session_id)
            if session is None or session.owner_user_id != user_id:
                raise ResourceNotFound("run not found")
            if run.status not in {"running", "waiting_input", "waiting_approval"}:
                raise RunStateConflict("run cannot accept steer in its current state")
            self.steers.setdefault(run.run_id, []).append(steer)
            return steer

    async def apply_pending_steers(self, *, user_id: str, run_id: str) -> list[Steer]:
        async with self._lock:
            run = self.runs.get(run_id)
            if run is None:
                raise ResourceNotFound("run not found")
            session = self.sessions.get(run.session_id)
            if session is None or session.owner_user_id != user_id:
                raise ResourceNotFound("run not found")
            now = datetime.now(UTC)
            applied = [
                steer.model_copy(update={"status": "applied", "applied_at": now})
                for steer in self.steers.get(run_id, [])
                if steer.status == "accepted"
            ]
            if applied:
                existing = self.steers.get(run_id, [])
                applied_by_id = {steer.steer_id: steer for steer in applied}
                self.steers[run_id] = [
                    applied_by_id.get(steer.steer_id, steer) for steer in existing
                ]
            return applied

    async def complete_run(
        self,
        *,
        user_id: str,
        run_id: str,
        outcome: str,
        completion_reason_code: str,
    ) -> AgentRun:
        async with self._lock:
            run = self.runs.get(run_id)
            if run is None:
                raise ResourceNotFound("run not found")
            session = self.sessions.get(run.session_id)
            if session is None or session.owner_user_id != user_id:
                raise ResourceNotFound("run not found")
            if session.active_run_id != run_id:
                raise RunStateConflict("run is not active")
            if run.status != "running":
                raise RunStateConflict("only a running run can be completed")
            now = datetime.now(UTC)
            completed = run.model_copy(
                update={
                    "status": "completed",
                    "outcome": outcome,
                    "completion_reason_code": completion_reason_code,
                    "current_phase": "completed",
                    "completed_at": now,
                }
            )
            self.runs[run_id] = completed
            self.sessions[session.session_id] = session.model_copy(
                update={
                    "active_run_id": None,
                    "updated_at": now,
                    "version": session.version + 1,
                }
            )
            return completed

    async def fail_run(
        self, *, user_id: str, run_id: str, completion_reason_code: str
    ) -> AgentRun:
        async with self._lock:
            run = self.runs.get(run_id)
            if run is None:
                raise ResourceNotFound("run not found")
            session = self.sessions.get(run.session_id)
            if session is None or session.owner_user_id != user_id:
                raise ResourceNotFound("run not found")
            if session.active_run_id != run_id or run.status != "running":
                raise RunStateConflict("only an active running run can fail")
            now = datetime.now(UTC)
            failed = run.model_copy(
                update={
                    "status": "failed",
                    "outcome": "failed",
                    "completion_reason_code": completion_reason_code,
                    "current_phase": "failed",
                    "completed_at": now,
                    "state_version": run.state_version + 1,
                }
            )
            self.runs[run_id] = failed
            self.sessions[session.session_id] = session.model_copy(
                update={
                    "active_run_id": None,
                    "updated_at": now,
                    "version": session.version + 1,
                }
            )
            return failed

    async def health_check(self) -> None:
        return None
