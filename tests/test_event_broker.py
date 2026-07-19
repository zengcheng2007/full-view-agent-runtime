import pytest

from full_view_agent.application.errors import EventHistoryExpired
from full_view_agent.infrastructure.event_broker import InMemoryEventBroker


def test_event_broker_exposes_retention_and_clock_configuration() -> None:
    import inspect

    parameters = inspect.signature(InMemoryEventBroker).parameters

    assert "retention_seconds" in parameters
    assert "now" in parameters


@pytest.mark.asyncio
async def test_event_cursor_expires_after_retention_window() -> None:
    from datetime import UTC, datetime, timedelta

    current = [datetime(2026, 7, 18, tzinfo=UTC)]
    broker = InMemoryEventBroker(
        retention_seconds=10,
        now=lambda: current[0],
    )
    event = await broker.publish(
        event_type="run.started",
        session_id="session-expiry",
        run_id="run-expiry",
        data={},
    )
    current[0] += timedelta(seconds=11)

    with pytest.raises(EventHistoryExpired):
        await broker.validate_cursor(
            run_id="run-expiry",
            after_event_id=event.event_id,
        )
    assert await broker.list_events(run_id="run-expiry") == []


@pytest.mark.asyncio
async def test_event_stream_never_delivers_expired_payloads() -> None:
    broker = InMemoryEventBroker(retention_seconds=0)
    await broker.publish(
        event_type="run.completed",
        session_id="session-expired-stream",
        run_id="run-expired-stream",
        data={"status": "completed"},
    )

    delivered = [event async for event in broker.stream(run_id="run-expired-stream")]

    assert delivered == []


@pytest.mark.asyncio
async def test_event_stream_resumes_after_last_event_id_and_closes_on_terminal_event() -> None:
    broker = InMemoryEventBroker()
    first = await broker.publish(
        event_type="run.started",
        session_id="session-01",
        run_id="run-01",
        data={"status": "running"},
    )
    await broker.publish(
        event_type="tool.completed",
        session_id="session-01",
        run_id="run-01",
        data={},
    )
    await broker.publish(
        event_type="run.completed",
        session_id="session-01",
        run_id="run-01",
        data={"status": "completed", "outcome": "success"},
    )

    resumed = [
        event
        async for event in broker.stream(run_id="run-01", after_event_id=first.event_id)
    ]

    assert [event.type for event in resumed] == ["tool.completed", "run.completed"]
