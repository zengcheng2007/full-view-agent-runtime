import pytest

from full_view_agent.application.errors import (
    ResourceNotFound,
    RunStateConflict,
    SessionActiveRunConflict,
)
from full_view_agent.application.session_run_service import SessionRunService
from full_view_agent.domain.models import PopulationMetricTable, RunCreateRequest, TableDataResult
from full_view_agent.infrastructure.memory_store import InMemoryAgentStore


def run_request(message_id: str = "web-msg-01") -> RunCreateRequest:
    return RunCreateRequest.model_validate(
        {
            "input": {
                "client_message_id": message_id,
                "content": [{"type": "text", "text": "查询独居老人数量"}],
            },
            "client": {
                "client_instance_id": "cli-01",
                "frontend_command_schema_versions": ["1.0"],
                "supported_commands": ["panel.show_table"],
            },
            "mode": "agent",
        }
    )


@pytest.mark.asyncio
async def test_sessions_are_owned_sorted_and_filterable_for_workspace_recovery() -> None:
    service = SessionRunService(InMemoryAgentStore())
    first = await service.create_session(user_id="user-01", title="第一个会话")
    second = await service.create_session(user_id="user-01", title="第二个会话")
    await service.create_session(user_id="other-user", title="其他用户会话")
    updated_first = await service.update_session(
        user_id="user-01",
        session_id=first.session_id,
        title="第一个会话（已更新）",
    )
    archived_second = await service.update_session(
        user_id="user-01",
        session_id=second.session_id,
        status="archived",
    )

    all_sessions = await service.list_sessions(user_id="user-01")
    active_sessions = await service.list_sessions(
        user_id="user-01",
        status="active",
    )
    archived_sessions = await service.list_sessions(
        user_id="user-01",
        status="archived",
    )

    assert [session.session_id for session in all_sessions] == [
        archived_second.session_id,
        updated_first.session_id,
    ]
    assert active_sessions == [updated_first]
    assert archived_sessions == [archived_second]


@pytest.mark.asyncio
async def test_session_detail_and_update_hide_other_users_resources() -> None:
    service = SessionRunService(InMemoryAgentStore())
    session = await service.create_session(user_id="user-01", title="私有会话")

    with pytest.raises(ResourceNotFound):
        await service.get_session(
            user_id="other-user",
            session_id=session.session_id,
        )
    with pytest.raises(ResourceNotFound):
        await service.update_session(
            user_id="other-user",
            session_id=session.session_id,
            title="越权修改",
        )


@pytest.mark.asyncio
async def test_renaming_a_session_preserves_active_run_for_refresh_recovery() -> None:
    service = SessionRunService(InMemoryAgentStore())
    session = await service.create_session(user_id="user-01", title="旧标题")
    run = await service.create_run(
        user_id="user-01",
        session_id=session.session_id,
        request=run_request(),
    )

    renamed = await service.update_session(
        user_id="user-01",
        session_id=session.session_id,
        title="新标题",
    )
    recovered = await service.get_session(
        user_id="user-01",
        session_id=session.session_id,
    )

    assert renamed.title == "新标题"
    assert renamed.active_run_id == run.run_id
    assert recovered == renamed
    assert renamed.version == session.version + 2


@pytest.mark.asyncio
async def test_active_session_cannot_be_archived_and_archived_session_cannot_run() -> None:
    service = SessionRunService(InMemoryAgentStore())
    active = await service.create_session(user_id="user-01", title="活动会话")
    run = await service.create_run(
        user_id="user-01",
        session_id=active.session_id,
        request=run_request(),
    )

    with pytest.raises(SessionActiveRunConflict) as exc_info:
        await service.update_session(
            user_id="user-01",
            session_id=active.session_id,
            status="archived",
        )

    assert exc_info.value.active_run_id == run.run_id

    idle = await service.create_session(user_id="user-01", title="待归档会话")
    archived = await service.update_session(
        user_id="user-01",
        session_id=idle.session_id,
        status="archived",
    )
    assert archived.status == "archived"
    with pytest.raises(RunStateConflict, match="archived"):
        await service.create_run(
            user_id="user-01",
            session_id=idle.session_id,
            request=run_request("web-msg-archived"),
        )


@pytest.mark.asyncio
async def test_creating_a_run_atomically_persists_its_user_message() -> None:
    store = InMemoryAgentStore()
    service = SessionRunService(store)
    session = await service.create_session(user_id="user-01", title="独居老人分析")

    run = await service.create_run(
        user_id="user-01",
        session_id=session.session_id,
        request=run_request(),
    )
    messages = await store.list_messages(
        user_id="user-01",
        session_id=session.session_id,
    )

    assert len(messages) == 1
    assert messages[0].message_id == run.input_message_id
    assert messages[0].run_id == run.run_id
    assert messages[0].role == "user"
    assert messages[0].content[0].text == "查询独居老人数量"


@pytest.mark.asyncio
async def test_session_rejects_a_second_active_run() -> None:
    service = SessionRunService(InMemoryAgentStore())
    session = await service.create_session(user_id="user-01", title="独居老人分析")
    first_run = await service.create_run(
        user_id="user-01", session_id=session.session_id, request=run_request()
    )

    with pytest.raises(SessionActiveRunConflict) as exc_info:
        await service.create_run(
            user_id="user-01",
            session_id=session.session_id,
            request=run_request("web-msg-02"),
        )

    assert exc_info.value.active_run_id == first_run.run_id


@pytest.mark.asyncio
async def test_completing_a_run_releases_the_session_for_the_next_run() -> None:
    service = SessionRunService(InMemoryAgentStore())
    session = await service.create_session(user_id="user-01", title="独居老人分析")
    first_run = await service.create_run(
        user_id="user-01", session_id=session.session_id, request=run_request()
    )
    await service.start_run(user_id="user-01", run_id=first_run.run_id)

    completed = await service.complete_run(
        user_id="user-01",
        run_id=first_run.run_id,
        outcome="success",
        completion_reason_code="goal_completed",
    )
    second_run = await service.create_run(
        user_id="user-01",
        session_id=session.session_id,
        request=run_request("web-msg-02"),
    )

    assert completed.status == "completed"
    assert completed.outcome == "success"
    assert second_run.run_id != first_run.run_id


@pytest.mark.asyncio
async def test_starting_a_queued_run_moves_it_to_running() -> None:
    service = SessionRunService(InMemoryAgentStore())
    session = await service.create_session(user_id="user-01", title="独居老人分析")
    queued = await service.create_run(
        user_id="user-01", session_id=session.session_id, request=run_request()
    )

    running = await service.start_run(user_id="user-01", run_id=queued.run_id)

    assert running.status == "running"
    assert running.current_phase == "planning"
    assert running.started_at is not None


@pytest.mark.asyncio
async def test_reauthentication_ledger_is_idempotent_across_crash_retries() -> None:
    service = SessionRunService(InMemoryAgentStore())
    session = await service.create_session(user_id="user-01", title="断点恢复")
    queued = await service.create_run(
        user_id="user-01", session_id=session.session_id, request=run_request()
    )
    await service.start_run(user_id="user-01", run_id=queued.run_id)

    waiting, pending = await service.wait_for_reauthentication(
        user_id="user-01", run_id=queued.run_id
    )
    repeated_waiting, repeated_pending = await service.wait_for_reauthentication(
        user_id="user-01", run_id=queued.run_id
    )

    assert repeated_waiting == waiting
    assert repeated_pending == pending

    resumed = await service.resume_from_input(
        user_id="user-01",
        run_id=queued.run_id,
        input_request_id=pending.input_request_id,
        run_state_version=pending.run_state_version,
    )
    replayed = await service.resume_from_input(
        user_id="user-01",
        run_id=queued.run_id,
        input_request_id=pending.input_request_id,
        run_state_version=pending.run_state_version,
    )

    assert replayed == resumed
    assert resumed.status == "running"
    assert resumed.state_version == pending.run_state_version + 1


@pytest.mark.asyncio
async def test_queued_run_cannot_skip_running_and_complete_directly() -> None:
    service = SessionRunService(InMemoryAgentStore())
    session = await service.create_session(user_id="user-01", title="独居老人分析")
    queued = await service.create_run(
        user_id="user-01", session_id=session.session_id, request=run_request()
    )

    with pytest.raises(RunStateConflict):
        await service.complete_run(
            user_id="user-01",
            run_id=queued.run_id,
            outcome="success",
            completion_reason_code="goal_completed",
        )


@pytest.mark.asyncio
async def test_cancelling_a_running_run_releases_the_session() -> None:
    service = SessionRunService(InMemoryAgentStore())
    session = await service.create_session(user_id="user-01", title="独居老人分析")
    run = await service.create_run(
        user_id="user-01", session_id=session.session_id, request=run_request()
    )
    await service.start_run(user_id="user-01", run_id=run.run_id)

    cancelled = await service.cancel_run(user_id="user-01", run_id=run.run_id)
    next_run = await service.create_run(
        user_id="user-01",
        session_id=session.session_id,
        request=run_request("web-msg-after-cancel"),
    )

    assert cancelled.status == "cancelled"
    assert cancelled.outcome == "cancelled"
    assert next_run.run_id != run.run_id


@pytest.mark.asyncio
async def test_cancelled_run_cannot_persist_a_late_tool_result() -> None:
    store = InMemoryAgentStore()
    service = SessionRunService(store)
    session = await service.create_session(user_id="user-01", title="取消后结果防线")
    run = await service.create_run(
        user_id="user-01",
        session_id=session.session_id,
        request=run_request(),
    )
    await service.start_run(user_id="user-01", run_id=run.run_id)
    await service.cancel_run(user_id="user-01", run_id=run.run_id)
    late_result = TableDataResult(
        result_id="res-late-after-cancel",
        data_schema_ref="schema://data/population-metric-table/1.0.0",
        result_fingerprint="sha256:late-after-cancel",
        data=PopulationMetricTable(rows=[]),
        row_count=0,
    )

    with pytest.raises(RunStateConflict):
        await store.save_result(
            user_id="user-01",
            run_id=run.run_id,
            result=late_result,
        )

    assert "res-late-after-cancel" not in store.results


@pytest.mark.asyncio
async def test_steer_is_accepted_for_the_next_safe_checkpoint() -> None:
    store = InMemoryAgentStore()
    service = SessionRunService(store)
    session = await service.create_session(user_id="user-01", title="独居老人分析")
    run = await service.create_run(
        user_id="user-01", session_id=session.session_id, request=run_request()
    )
    await service.start_run(user_id="user-01", run_id=run.run_id)

    steer = await service.steer_run(
        user_id="user-01",
        run_id=run.run_id,
        client_instance_id="cli-01",
        content="结果出来后再筛选 80 岁以上。",
    )

    assert steer.status == "accepted"
    assert steer.delivery == "next_safe_checkpoint"
