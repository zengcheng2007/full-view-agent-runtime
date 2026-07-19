from secrets import token_hex
from typing import Literal

from full_view_agent.application.ports import AgentStore
from full_view_agent.domain.models import (
    AgentMessage,
    AgentRun,
    AgentSession,
    MessageContent,
    RunCreateRequest,
    Steer,
)


def new_id(prefix: str) -> str:
    return f"{prefix}_{token_hex(13)}"


class SessionRunService:
    def __init__(self, store: AgentStore) -> None:
        self._store = store

    async def create_session(self, *, user_id: str, title: str) -> AgentSession:
        session = AgentSession(
            session_id=new_id("ses"),
            owner_user_id=user_id,
            title=title,
        )
        return await self._store.create_session(session)

    async def get_session(
        self, *, user_id: str, session_id: str
    ) -> AgentSession:
        return await self._store.get_session(user_id=user_id, session_id=session_id)

    async def list_sessions(
        self,
        *,
        user_id: str,
        status: Literal["active", "archived"] | None = None,
    ) -> list[AgentSession]:
        return await self._store.list_sessions(user_id=user_id, status=status)

    async def update_session(
        self,
        *,
        user_id: str,
        session_id: str,
        title: str | None = None,
        status: Literal["archived"] | None = None,
    ) -> AgentSession:
        if title is None and status is None:
            raise ValueError("at least one session field must be updated")
        return await self._store.update_session(
            user_id=user_id,
            session_id=session_id,
            title=title,
            status=status,
        )

    async def create_run(
        self,
        *,
        user_id: str,
        session_id: str,
        request: RunCreateRequest,
    ) -> AgentRun:
        message_id = new_id("msg")
        run = AgentRun(
            run_id=new_id("run"),
            session_id=session_id,
            origin_client_instance_id=request.client.client_instance_id,
            client_capabilities=request.client,
            status="queued",
            mode=request.mode,
            workflow_ref=request.workflow_ref,
            input_message_id=message_id,
            base_context_version=1,
        )
        message_content: list[MessageContent] = []
        message_content.extend(request.input.content)
        input_message = AgentMessage(
            message_id=message_id,
            session_id=session_id,
            run_id=run.run_id,
            role="user",
            content=message_content,
        )
        return await self._store.create_run_if_session_idle(
            user_id=user_id,
            session_id=session_id,
            run=run,
            input_message=input_message,
        )

    async def start_run(self, *, user_id: str, run_id: str) -> AgentRun:
        return await self._store.start_run(user_id=user_id, run_id=run_id)

    async def cancel_run(self, *, user_id: str, run_id: str) -> AgentRun:
        return await self._store.cancel_run(user_id=user_id, run_id=run_id)

    async def wait_for_reauthentication(self, *, user_id: str, run_id: str):
        return await self._store.wait_for_reauthentication(
            user_id=user_id,
            run_id=run_id,
        )

    async def resume_from_input(
        self,
        *,
        user_id: str,
        run_id: str,
        input_request_id: str,
        run_state_version: int,
    ) -> AgentRun:
        return await self._store.resume_from_input(
            user_id=user_id,
            run_id=run_id,
            input_request_id=input_request_id,
            run_state_version=run_state_version,
        )

    async def steer_run(
        self,
        *,
        user_id: str,
        run_id: str,
        client_instance_id: str,
        content: str,
    ) -> Steer:
        steer = Steer(
            steer_id=new_id("str"),
            run_id=run_id,
            client_instance_id=client_instance_id,
            content=content,
        )
        return await self._store.add_steer(user_id=user_id, steer=steer)

    async def complete_run(
        self,
        *,
        user_id: str,
        run_id: str,
        outcome: Literal["success", "partial", "denied"],
        completion_reason_code: str,
    ) -> AgentRun:
        return await self._store.complete_run(
            user_id=user_id,
            run_id=run_id,
            outcome=outcome,
            completion_reason_code=completion_reason_code,
        )

    async def fail_run(
        self, *, user_id: str, run_id: str, completion_reason_code: str
    ) -> AgentRun:
        return await self._store.fail_run(
            user_id=user_id,
            run_id=run_id,
            completion_reason_code=completion_reason_code,
        )
