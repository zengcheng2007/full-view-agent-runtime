import asyncio
import os
from base64 import urlsafe_b64encode
from datetime import UTC, datetime, timedelta
from secrets import token_bytes
from uuid import uuid4

import psycopg
import pytest
from pydantic import SecretStr

from full_view_agent.application.errors import ResourceNotFound, RunStateConflict
from full_view_agent.application.session_run_service import SessionRunService
from full_view_agent.domain.models import (
    AgentMessage,
    FrontendCommand,
    FrontendCommandPreconditions,
    FrontendCommandReceipt,
    PanelShowTablePayload,
    PopulationMetricTable,
    TableDataResult,
    TextContent,
)
from full_view_agent.infrastructure.postgres_persistence import (
    PostgresAgentPersistence,
)

from .test_policy import population_auth_context
from .test_session_run_service import run_request


def postgres_test_dsn() -> str:
    dsn = os.getenv("FULL_VIEW_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("FULL_VIEW_TEST_DATABASE_URL is not configured")
    return dsn


def test_postgres_event_store_exposes_retention_configuration() -> None:
    import inspect

    parameters = inspect.signature(PostgresAgentPersistence).parameters

    assert "event_retention_seconds" in parameters
    assert "event_notifier" in parameters


@pytest.mark.asyncio
async def test_postgres_result_identity_cannot_cross_runs_or_users() -> None:
    schema = f"fva_test_{uuid4().hex[:12]}"
    store = PostgresAgentPersistence(dsn=postgres_test_dsn(), schema=schema)
    await store.initialize()
    try:
        service = SessionRunService(store)
        alice_session = await service.create_session(user_id="alice", title="alice")
        bob_session = await service.create_session(user_id="bob", title="bob")
        alice_run = await service.create_run(
            user_id="alice", session_id=alice_session.session_id, request=run_request()
        )
        bob_run = await service.create_run(
            user_id="bob", session_id=bob_session.session_id, request=run_request()
        )
        await service.start_run(user_id="alice", run_id=alice_run.run_id)
        await service.start_run(user_id="bob", run_id=bob_run.run_id)
        alice_result = TableDataResult(
            result_id="res-global-collision",
            data_schema_ref="schema://data/population-metric-table/1.0.0",
            result_fingerprint="sha256:alice",
            data=PopulationMetricTable(rows=[]),
            row_count=0,
        )
        bob_result = alice_result.model_copy(
            update={"result_fingerprint": "sha256:bob"}
        )

        assert await store.save_result(
            user_id="alice", run_id=alice_run.run_id, result=alice_result
        ) == alice_result
        assert await store.save_result(
            user_id="alice", run_id=alice_run.run_id, result=alice_result
        ) == alice_result
        with pytest.raises(RunStateConflict):
            await store.save_result(
                user_id="bob", run_id=bob_run.run_id, result=bob_result
            )

        assert await store.get_result_for_run(
            user_id="alice",
            run_id=alice_run.run_id,
            result_id=alice_result.result_id,
        ) == alice_result
        with pytest.raises(ResourceNotFound):
            await store.get_result_for_run(
                user_id="bob",
                run_id=bob_run.run_id,
                result_id=alice_result.result_id,
            )
    finally:
        await store.drop_schema()


@pytest.mark.asyncio
async def test_postgres_session_workspace_management_survives_restart() -> None:
    schema = f"fva_test_{uuid4().hex[:12]}"
    store = PostgresAgentPersistence(dsn=postgres_test_dsn(), schema=schema)
    await store.initialize()
    try:
        service = SessionRunService(store)
        first = await service.create_session(user_id="user-01", title="第一个会话")
        second = await service.create_session(user_id="user-01", title="第二个会话")
        await service.create_session(user_id="other-user", title="其他用户会话")
        renamed = await service.update_session(
            user_id="user-01",
            session_id=first.session_id,
            title="已重命名会话",
        )
        archived = await service.update_session(
            user_id="user-01",
            session_id=second.session_id,
            status="archived",
        )

        restarted = PostgresAgentPersistence(
            dsn=postgres_test_dsn(),
            schema=schema,
        )
        sessions = await restarted.list_sessions(user_id="user-01")
        archived_sessions = await restarted.list_sessions(
            user_id="user-01",
            status="archived",
        )
        recovered = await restarted.get_session(
            user_id="user-01",
            session_id=first.session_id,
        )

        assert [session.session_id for session in sessions] == [
            archived.session_id,
            renamed.session_id,
        ]
        assert archived_sessions == [archived]
        assert recovered == renamed
        with pytest.raises(ResourceNotFound):
            await restarted.get_session(
                user_id="other-user",
                session_id=first.session_id,
            )
    finally:
        await store.drop_schema()


@pytest.mark.asyncio
async def test_postgres_persists_the_run_input_message_atomically() -> None:
    schema = f"fva_test_{uuid4().hex[:12]}"
    store = PostgresAgentPersistence(dsn=postgres_test_dsn(), schema=schema)
    await store.initialize()
    try:
        service = SessionRunService(store)
        session = await service.create_session(user_id="user-01", title="消息持久化")

        run = await service.create_run(
            user_id="user-01",
            session_id=session.session_id,
            request=run_request(),
        )
        restarted = PostgresAgentPersistence(
            dsn=postgres_test_dsn(),
            schema=schema,
        )
        messages = await restarted.list_messages(
            user_id="user-01",
            session_id=session.session_id,
        )

        assert [message.message_id for message in messages] == [run.input_message_id]
        assert messages[0].content[0].text == "查询独居老人数量"
    finally:
        await store.drop_schema()


@pytest.mark.asyncio
async def test_postgres_reauthentication_retry_is_idempotent_after_restart() -> None:
    schema = f"fva_test_{uuid4().hex[:12]}"
    store = PostgresAgentPersistence(dsn=postgres_test_dsn(), schema=schema)
    await store.initialize()
    try:
        service = SessionRunService(store)
        session = await service.create_session(user_id="user-01", title="认证断点")
        queued = await service.create_run(
            user_id="user-01",
            session_id=session.session_id,
            request=run_request(),
        )
        await service.start_run(user_id="user-01", run_id=queued.run_id)

        waiting, pending = await service.wait_for_reauthentication(
            user_id="user-01", run_id=queued.run_id
        )
        restarted = PostgresAgentPersistence(dsn=postgres_test_dsn(), schema=schema)
        repeated_waiting, repeated_pending = await restarted.wait_for_reauthentication(
            user_id="user-01", run_id=queued.run_id
        )

        assert repeated_waiting == waiting
        assert repeated_pending == pending

        resumed = await restarted.resume_from_input(
            user_id="user-01",
            run_id=queued.run_id,
            input_request_id=pending.input_request_id,
            run_state_version=pending.run_state_version,
        )
        replayed = await store.resume_from_input(
            user_id="user-01",
            run_id=queued.run_id,
            input_request_id=pending.input_request_id,
            run_state_version=pending.run_state_version,
        )

        assert replayed == resumed
        assert resumed.status == "running"
        assert resumed.state_version == pending.run_state_version + 1

        completed = await service.complete_run(
            user_id="user-01",
            run_id=queued.run_id,
            outcome="success",
            completion_reason_code="goal_completed",
        )
        replayed_after_completion = await restarted.resume_from_input(
            user_id="user-01",
            run_id=queued.run_id,
            input_request_id=pending.input_request_id,
            run_state_version=pending.run_state_version,
        )
        assert replayed_after_completion == completed
    finally:
        await store.drop_schema()


@pytest.mark.asyncio
async def test_postgres_expired_reauthentication_request_is_reissued() -> None:
    schema = f"fva_test_{uuid4().hex[:12]}"
    dsn = postgres_test_dsn()
    store = PostgresAgentPersistence(dsn=dsn, schema=schema)
    await store.initialize()
    try:
        service = SessionRunService(store)
        session = await service.create_session(user_id="user-01", title="认证过期")
        queued = await service.create_run(
            user_id="user-01",
            session_id=session.session_id,
            request=run_request(),
        )
        await service.start_run(user_id="user-01", run_id=queued.run_id)
        waiting, pending = await service.wait_for_reauthentication(
            user_id="user-01", run_id=queued.run_id
        )
        expired = pending.model_copy(
            update={"expires_at": datetime.now(UTC) - timedelta(seconds=1)}
        )
        async with await psycopg.AsyncConnection.connect(dsn) as connection:
            await connection.execute(
                f'UPDATE "{schema}".input_requests SET data_json = %s '
                "WHERE run_id = %s",
                (expired.model_dump_json(), queued.run_id),
            )

        restarted = PostgresAgentPersistence(dsn=dsn, schema=schema)
        renewed_waiting, renewed = await restarted.wait_for_reauthentication(
            user_id="user-01", run_id=queued.run_id
        )

        assert renewed.input_request_id != pending.input_request_id
        assert renewed.kind == "reauth"
        assert renewed.run_state_version == waiting.state_version + 1
        assert renewed_waiting.state_version == renewed.run_state_version
        assert renewed.expires_at > datetime.now(UTC)
    finally:
        await store.drop_schema()


@pytest.mark.asyncio
async def test_postgres_persists_an_assistant_message_for_an_active_run() -> None:
    schema = f"fva_test_{uuid4().hex[:12]}"
    store = PostgresAgentPersistence(dsn=postgres_test_dsn(), schema=schema)
    await store.initialize()
    try:
        service = SessionRunService(store)
        session = await service.create_session(user_id="user-01", title="助手消息")
        run = await service.create_run(
            user_id="user-01",
            session_id=session.session_id,
            request=run_request(),
        )
        await service.start_run(user_id="user-01", run_id=run.run_id)
        assistant = AgentMessage(
            message_id="msg-assistant-postgres",
            session_id=session.session_id,
            run_id=run.run_id,
            role="assistant",
            content=[TextContent(type="text", text="查询已完成")],
        )

        await store.save_message(
            user_id="user-01",
            run_id=run.run_id,
            message=assistant,
        )
        restarted = PostgresAgentPersistence(
            dsn=postgres_test_dsn(),
            schema=schema,
        )
        messages = await restarted.list_messages(
            user_id="user-01",
            session_id=session.session_id,
        )

        assert [message.role for message in messages] == ["user", "assistant"]
    finally:
        await store.drop_schema()


@pytest.mark.asyncio
async def test_postgres_persists_frontend_command_and_accepts_receipt_after_restart() -> None:
    schema = f"fva_test_{uuid4().hex[:12]}"
    store = PostgresAgentPersistence(dsn=postgres_test_dsn(), schema=schema)
    await store.initialize()
    try:
        service = SessionRunService(store)
        session = await service.create_session(user_id="user-01", title="前端命令")
        run = await service.create_run(
            user_id="user-01",
            session_id=session.session_id,
            request=run_request(),
        )
        await service.start_run(user_id="user-01", run_id=run.run_id)
        now = datetime.now(UTC)
        command = FrontendCommand(
            command_id="cmd-postgres-01",
            run_id=run.run_id,
            target_client_instance_id="cli-01",
            type="panel.show_table",
            issued_at=now,
            expires_at=now + timedelta(minutes=5),
            preconditions=FrontendCommandPreconditions(
                session_id=session.session_id,
                area_code="330106",
                required_client_capability="panel.show_table@1.0",
            ),
            payload=PanelShowTablePayload(result_id="res-postgres-command"),
        )
        await store.save_frontend_command(
            user_id="user-01",
            run_id=run.run_id,
            command=command,
        )

        restarted = PostgresAgentPersistence(dsn=postgres_test_dsn(), schema=schema)
        receipt = FrontendCommandReceipt(
            command_id=command.command_id,
            client_instance_id="cli-01",
            status="completed",
            received_at=now,
            completed_at=now + timedelta(seconds=1),
            client_state={"route_id": "agent_workspace"},
        )
        stored = await restarted.put_frontend_command_receipt(
            user_id="user-01",
            run_id=run.run_id,
            command_id=command.command_id,
            receipt=receipt,
        )
        replayed = await restarted.put_frontend_command_receipt(
            user_id="user-01",
            run_id=run.run_id,
            command_id=command.command_id,
            receipt=receipt,
        )

        assert stored == receipt
        assert replayed == receipt
    finally:
        await store.drop_schema()


@pytest.mark.asyncio
async def test_postgres_rejects_expired_event_cursor() -> None:
    from full_view_agent.application.errors import EventHistoryExpired

    schema = f"fva_test_{uuid4().hex[:12]}"
    store = PostgresAgentPersistence(
        dsn=postgres_test_dsn(),
        schema=schema,
        event_retention_seconds=0,
    )
    await store.initialize()
    try:
        event = await store.publish(
            event_type="run.completed",
            session_id="session-expired-event",
            run_id="run-expired-event",
            data={},
        )

        with pytest.raises(EventHistoryExpired):
            await store.validate_cursor(
                run_id="run-expired-event",
                after_event_id=event.event_id,
            )
        assert await store.list_events(run_id="run-expired-event") == []

        async def consume_stream():
            return [
                item
                async for item in store.stream(run_id="run-expired-event")
            ]

        task = asyncio.create_task(consume_stream())
        try:
            assert await asyncio.wait_for(task, timeout=1) == []
        finally:
            task.cancel()
    finally:
        await store.drop_schema()


@pytest.mark.asyncio
async def test_postgres_persists_runtime_authority_state_across_restart() -> None:
    schema = f"fva_test_{uuid4().hex[:12]}"
    key = token_bytes(32)
    first = PostgresAgentPersistence(
        dsn=postgres_test_dsn(),
        schema=schema,
        credential_encryption_key=key,
    )
    await first.initialize()
    try:
        service = SessionRunService(first)
        session = await service.create_session(user_id="user-01", title="持久化恢复")
        run = await service.create_run(
            user_id="user-01",
            session_id=session.session_id,
            request=run_request(),
        )
        running = await service.start_run(user_id="user-01", run_id=run.run_id)
        result = TableDataResult(
            result_id="res-postgres-restart",
            data_schema_ref="schema://data/population-metric-table/1.0.0",
            result_fingerprint="sha256:postgres-restart",
            data=PopulationMetricTable(rows=[]),
            row_count=0,
        )
        await first.save_result(user_id="user-01", run_id=run.run_id, result=result)

        grant = await first.issue(
            raw_token=SecretStr("postgres-secret-token"),
            subject_user_id="user-01",
            app_id="full_information_view",
            run_id=run.run_id,
            source_expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        auth_context = population_auth_context().model_copy(
            update={
                "session_id": session.session_id,
                "run_id": run.run_id,
                "credential_ref": grant.credential_ref,
            }
        )
        await first.put(auth_context)
        started_event = await first.publish(
            event_type="run.started",
            session_id=session.session_id,
            run_id=run.run_id,
            data={"status": "running"},
        )

        operation_calls = 0

        async def original_operation():
            nonlocal operation_calls
            operation_calls += 1
            return running

        first_result, replayed = await first.execute(
            user_id="user-01",
            scope="test:restart",
            key="idem-postgres-restart",
            request_fingerprint="sha256:same",
            operation=original_operation,
        )
        assert first_result.run_id == run.run_id
        assert replayed is False

        restarted = PostgresAgentPersistence(
            dsn=postgres_test_dsn(),
            schema=schema,
            credential_encryption_key=key,
        )

        async def must_not_execute():
            raise AssertionError("persisted idempotency record was not replayed")

        recovered_run = await restarted.get_run(user_id="user-01", run_id=run.run_id)
        recovered_result = await restarted.get_result(
            user_id="user-01", result_id=result.result_id
        )
        recovered_context = await restarted.get(user_id="user-01", run_id=run.run_id)
        recovered_events = await restarted.list_events(run_id=run.run_id)
        recovered_token = await restarted.resolve(
            credential_ref=grant.credential_ref,
            subject_user_id="user-01",
            app_id="full_information_view",
            run_id=run.run_id,
        )
        replay_result, replayed = await restarted.execute(
            user_id="user-01",
            scope="test:restart",
            key="idem-postgres-restart",
            request_fingerprint="sha256:same",
            operation=must_not_execute,
        )

        assert recovered_run.status == "running"
        assert recovered_result.result_id == result.result_id
        assert recovered_context.credential_ref == grant.credential_ref
        assert recovered_events == [started_event]
        assert recovered_token.get_secret_value() == "postgres-secret-token"
        assert replay_result.run_id == run.run_id
        assert replayed is True
        assert operation_calls == 1
    finally:
        await first.drop_schema()


@pytest.mark.asyncio
async def test_postgres_event_sequence_is_atomic_across_instances() -> None:
    schema = f"fva_test_{uuid4().hex[:12]}"
    dsn = postgres_test_dsn()
    first = PostgresAgentPersistence(dsn=dsn, schema=schema)
    second = PostgresAgentPersistence(dsn=dsn, schema=schema)
    await first.initialize()
    try:
        events = await asyncio.gather(
            *[
                (first if index % 2 else second).publish(
                    event_type="tool.progress",
                    session_id="session-atomic",
                    run_id="run-atomic",
                    data={"index": index},
                )
                for index in range(20)
            ]
        )

        assert sorted(event.sequence for event in events) == list(range(1, 21))
        assert [event.sequence for event in await first.list_events(run_id="run-atomic")] == list(
            range(1, 21)
        )
    finally:
        await first.drop_schema()


@pytest.mark.asyncio
async def test_redis_wakes_cross_instance_postgres_event_stream() -> None:
    from full_view_agent.infrastructure.redis_event_notifier import (
        RedisEventNotifier,
    )

    redis_url = os.getenv("FULL_VIEW_TEST_REDIS_URL")
    if not redis_url:
        pytest.skip("FULL_VIEW_TEST_REDIS_URL is not configured")
    schema = f"fva_test_{uuid4().hex[:12]}"
    prefix = f"fva-test:{uuid4().hex}"
    publisher_notifier = RedisEventNotifier(url=redis_url, channel_prefix=prefix)
    subscriber_notifier = RedisEventNotifier(url=redis_url, channel_prefix=prefix)
    publisher = PostgresAgentPersistence(
        dsn=postgres_test_dsn(),
        schema=schema,
        event_poll_interval_seconds=5,
        event_notifier=publisher_notifier,
    )
    subscriber = PostgresAgentPersistence(
        dsn=postgres_test_dsn(),
        schema=schema,
        event_poll_interval_seconds=5,
        event_notifier=subscriber_notifier,
    )
    await publisher.initialize()
    try:
        async def consume_stream():
            return [
                event
                async for event in subscriber.stream(run_id="run-redis-wakeup")
            ]

        stream_task = asyncio.create_task(consume_stream())
        await asyncio.sleep(0.1)
        await publisher.publish(
            event_type="run.completed",
            session_id="session-redis-wakeup",
            run_id="run-redis-wakeup",
            data={"status": "completed"},
        )
        await asyncio.sleep(0.3)
        try:
            assert stream_task.done()
            assert [event.type for event in stream_task.result()] == ["run.completed"]
        finally:
            stream_task.cancel()
    finally:
        await publisher.drop_schema()
        await publisher_notifier.close()
        await subscriber_notifier.close()


@pytest.mark.asyncio
async def test_event_persistence_succeeds_when_redis_notification_fails() -> None:
    class FailingNotifier:
        async def publish(self, **_kwargs) -> None:
            raise RuntimeError("redis unavailable")

        async def wait(self, **_kwargs) -> None:
            raise RuntimeError("redis unavailable")

    schema = f"fva_test_{uuid4().hex[:12]}"
    store = PostgresAgentPersistence(
        dsn=postgres_test_dsn(),
        schema=schema,
        event_notifier=FailingNotifier(),
    )
    await store.initialize()
    try:
        raised = False
        try:
            event = await store.publish(
                event_type="run.started",
                session_id="session-redis-down",
                run_id="run-redis-down",
                data={},
            )
        except RuntimeError:
            raised = True

        assert raised is False
        assert [event.event_id for event in await store.list_events(run_id="run-redis-down")] == [
            event.event_id
        ]
    finally:
        await store.drop_schema()


@pytest.mark.asyncio
async def test_event_stream_falls_back_to_polling_when_redis_wait_fails() -> None:
    class FailingNotifier:
        async def publish(self, **_kwargs) -> None:
            raise RuntimeError("redis unavailable")

        async def wait(self, **_kwargs) -> None:
            raise RuntimeError("redis unavailable")

    schema = f"fva_test_{uuid4().hex[:12]}"
    publisher = PostgresAgentPersistence(dsn=postgres_test_dsn(), schema=schema)
    subscriber = PostgresAgentPersistence(
        dsn=postgres_test_dsn(),
        schema=schema,
        event_notifier=FailingNotifier(),
        event_poll_interval_seconds=0.05,
    )
    await publisher.initialize()
    try:
        async def consume_stream():
            return [event async for event in subscriber.stream(run_id="run-poll-fallback")]

        stream_task = asyncio.create_task(consume_stream())
        await asyncio.sleep(0.1)
        await publisher.publish(
            event_type="run.completed",
            session_id="session-poll-fallback",
            run_id="run-poll-fallback",
            data={},
        )
        await asyncio.sleep(0.2)
        try:
            assert stream_task.done()
            assert stream_task.exception() is None
            assert [event.type for event in stream_task.result()] == ["run.completed"]
        finally:
            stream_task.cancel()
    finally:
        await publisher.drop_schema()


@pytest.mark.asyncio
async def test_runtime_recovers_active_run_after_process_restart(monkeypatch) -> None:
    from full_view_agent.api.app import RuntimeContainer
    from full_view_agent.infrastructure.legacy_identity import HashedLegacyIdentityAdapter

    schema = f"fva_test_{uuid4().hex[:12]}"
    key = token_bytes(32)
    monkeypatch.setenv("FULL_VIEW_DATABASE_URL", postgres_test_dsn())
    monkeypatch.setenv("FULL_VIEW_POSTGRES_SCHEMA", schema)
    monkeypatch.setenv(
        "FULL_VIEW_CREDENTIAL_KEY",
        urlsafe_b64encode(key).decode("ascii"),
    )
    identity_port = HashedLegacyIdentityAdapter()
    first_runtime = RuntimeContainer(identity_port=identity_port)
    assert first_runtime.persistence is not None
    try:
        session = await first_runtime.service.create_session(
            user_id="user-recovery",
            title="重启恢复",
        )
        run = await first_runtime.service.create_run(
            user_id="user-recovery",
            session_id=session.session_id,
            request=run_request(),
        )
        await first_runtime.service.start_run(user_id="user-recovery", run_id=run.run_id)
        identity = await identity_port.resolve(SecretStr("recovery-token"))
        identity = identity.model_copy(
            update={
                "principal": identity.principal.model_copy(
                    update={"user_id": "user-recovery"}
                )
            }
        )
        await first_runtime.admission.admit(
            identity=identity,
            raw_token=SecretStr("recovery-token"),
            session_id=session.session_id,
            run_id=run.run_id,
        )

        restarted = RuntimeContainer(identity_port=identity_port)
        recovered_count = await restarted.recover_runs()
        for _ in range(100):
            recovered = await restarted.store.get_run(
                user_id="user-recovery",
                run_id=run.run_id,
            )
            if recovered.status == "completed":
                break
            await asyncio.sleep(0.01)

        assert recovered_count == 1
        assert recovered.status == "completed"
        assert recovered.outcome == "success"
        assert restarted.executor.task_failures == []
        events = await restarted.events.list_events(run_id=run.run_id)
        assert events[0].type == "run.resumed"
    finally:
        await first_runtime.persistence.drop_schema()


def test_runtime_container_uses_one_postgres_authority_store(monkeypatch) -> None:
    from full_view_agent.api.app import RuntimeContainer
    from full_view_agent.infrastructure.legacy_identity import HashedLegacyIdentityAdapter

    schema = f"fva_test_{uuid4().hex[:12]}"
    monkeypatch.setenv("FULL_VIEW_DATABASE_URL", postgres_test_dsn())
    monkeypatch.setenv("FULL_VIEW_POSTGRES_SCHEMA", schema)
    monkeypatch.setenv(
        "FULL_VIEW_CREDENTIAL_KEY",
        urlsafe_b64encode(token_bytes(32)).decode("ascii"),
    )

    runtime = RuntimeContainer(identity_port=HashedLegacyIdentityAdapter())

    assert runtime.persistence is not None
    assert runtime.store is runtime.persistence
    assert runtime.events is runtime.persistence
    assert runtime.idempotency is runtime.persistence
    assert runtime.credentials is runtime.persistence
    assert runtime.auth_contexts is runtime.persistence


def test_runtime_container_wires_redis_notifier_when_configured(monkeypatch) -> None:
    from full_view_agent.api.app import RuntimeContainer
    from full_view_agent.infrastructure.legacy_identity import HashedLegacyIdentityAdapter
    from full_view_agent.infrastructure.redis_event_notifier import RedisEventNotifier

    monkeypatch.setenv("FULL_VIEW_DATABASE_URL", postgres_test_dsn())
    monkeypatch.setenv("FULL_VIEW_POSTGRES_SCHEMA", f"fva_test_{uuid4().hex[:12]}")
    monkeypatch.setenv("FULL_VIEW_REDIS_URL", "redis://127.0.0.1:16379/0")
    monkeypatch.setenv(
        "FULL_VIEW_CREDENTIAL_KEY",
        urlsafe_b64encode(token_bytes(32)).decode("ascii"),
    )

    runtime = RuntimeContainer(identity_port=HashedLegacyIdentityAdapter())

    assert isinstance(getattr(runtime, "event_notifier", None), RedisEventNotifier)
