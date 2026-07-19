import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from full_view_agent.application.errors import EventHistoryExpired
from full_view_agent.application.session_run_service import new_id
from full_view_agent.domain.models import AgentEvent


class InMemoryEventBroker:
    def __init__(
        self,
        *,
        retention_seconds: int = 3600,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._condition = asyncio.Condition()
        self._events: dict[str, list[AgentEvent]] = {}
        self._expires_at: dict[str, datetime] = {}
        self._retention_seconds = retention_seconds
        self._now = now or (lambda: datetime.now(UTC))

    async def publish(
        self,
        *,
        event_type: str,
        session_id: str,
        run_id: str,
        data: dict[str, object],
    ) -> AgentEvent:
        async with self._condition:
            run_events = self._events.setdefault(run_id, [])
            event = AgentEvent(
                event_id=new_id("evt"),
                sequence=len(run_events) + 1,
                type=event_type,
                session_id=session_id,
                run_id=run_id,
                trace_id=f"trc_{run_id}",
                data=data,
            )
            run_events.append(event)
            self._expires_at[event.event_id] = self._now() + timedelta(
                seconds=self._retention_seconds
            )
            self._condition.notify_all()
            return event

    async def list_events(self, *, run_id: str) -> list[AgentEvent]:
        async with self._condition:
            now = self._now()
            return [
                event
                for event in self._events.get(run_id, [])
                if self._expires_at.get(event.event_id, now) > now
            ]

    async def validate_cursor(
        self, *, run_id: str, after_event_id: str | None
    ) -> None:
        if after_event_id is None:
            return
        async with self._condition:
            matching = next(
                (
                    event
                    for event in self._events.get(run_id, [])
                    if event.event_id == after_event_id
                ),
                None,
            )
            if (
                matching is None
                or self._expires_at.get(matching.event_id, self._now()) <= self._now()
            ):
                raise EventHistoryExpired("event history is no longer available")

    async def stream(self, *, run_id: str, after_event_id: str | None = None):
        terminal_types = {"run.completed", "run.failed", "run.cancelled"}
        cursor = 0
        async with self._condition:
            now = self._now()
            existing = [
                event
                for event in self._events.get(run_id, [])
                if self._expires_at.get(event.event_id, now) > now
            ]
            if after_event_id is not None:
                for index, event in enumerate(existing):
                    if event.event_id == after_event_id:
                        cursor = index + 1
                        break

        while True:
            async with self._condition:
                all_run_events = self._events.get(run_id, [])
                now = self._now()
                run_events = [
                    event
                    for event in all_run_events
                    if self._expires_at.get(event.event_id, now) > now
                ]
                if cursor >= len(run_events):
                    if all_run_events and all_run_events[-1].type in terminal_types:
                        return
                    await self._condition.wait()
                    continue
                pending = list(run_events[cursor:])

            for event in pending:
                cursor += 1
                yield event
                if event.type in terminal_types:
                    return

    async def health_check(self) -> None:
        return None
