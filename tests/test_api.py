import asyncio
import base64
import json

import httpx
import pytest

from full_view_agent.api.app import (
    RuntimeContainer,
    configured_p0_allowed_user_ids,
    create_app,
)
from full_view_agent.application.answer_claims import FINISH_TOOL_ID
from full_view_agent.application.model_provider import (
    ModelResponse,
    ModelToolCall,
)
from full_view_agent.domain.models import ObjectProfileResult, RunCreateRequest
from full_view_agent.infrastructure.credential_broker import (
    InMemoryCredentialBroker,
    UnconfiguredCredentialBroker,
)
from full_view_agent.infrastructure.legacy_identity import HashedLegacyIdentityAdapter


def runtime_fixture() -> RuntimeContainer:
    return RuntimeContainer(
        identity_port=HashedLegacyIdentityAdapter(),
        credentials=InMemoryCredentialBroker(),
    )


def test_runtime_selects_http_governance_adapter_only_when_explicitly_enabled(
    monkeypatch,
) -> None:
    from full_view_agent.infrastructure.governance_adapter import (
        HttpGovernanceAdapter,
    )

    monkeypatch.setenv("FULL_VIEW_GOVERNANCE_ADAPTER", "http")
    monkeypatch.setenv(
        "FULL_VIEW_GOVERNANCE_BASE_URL",
        "http://127.0.0.1:9666/geo-qxst",
    )

    runtime = runtime_fixture()

    assert isinstance(runtime.governance_adapter, HttpGovernanceAdapter)
    assert runtime.tool_registry.list_tool_ids() == [
        "governance.query_event_metrics",
        "governance.query_housing_metrics",
        "governance.query_population_metrics",
        "governance.resolve_area",
    ]


@pytest.mark.asyncio
async def test_app_lifespan_closes_owned_http_governance_client(monkeypatch) -> None:
    monkeypatch.setenv("FULL_VIEW_GOVERNANCE_ADAPTER", "http")
    runtime = runtime_fixture()
    app = create_app(runtime)
    client = runtime.governance_adapter._client

    async with app.router.lifespan_context(app):
        assert client.is_closed is False

    assert client.is_closed is True


def test_p0_allowed_user_ids_are_loaded_as_an_exact_trimmed_allowlist(monkeypatch) -> None:
    monkeypatch.setenv(
        "FULL_VIEW_P0_ALLOWED_USER_IDS",
        " legacy-user-01,legacy-user-02,legacy-user-01 ",
    )

    assert configured_p0_allowed_user_ids() == {
        "legacy-user-01",
        "legacy-user-02",
    }


def test_production_runtime_fails_closed_when_authority_store_is_missing(
    monkeypatch,
) -> None:
    monkeypatch.setenv("FULL_VIEW_RUNTIME_PROFILE", "production")
    monkeypatch.delenv("FULL_VIEW_DATABASE_URL", raising=False)

    with pytest.raises(RuntimeError, match="FULL_VIEW_DATABASE_URL"):
        RuntimeContainer()


def test_production_runtime_requires_credential_encryption_key(monkeypatch) -> None:
    monkeypatch.setenv("FULL_VIEW_RUNTIME_PROFILE", "production")
    monkeypatch.setenv(
        "FULL_VIEW_DATABASE_URL",
        "postgresql://agent:password@database.invalid/agent",
    )
    monkeypatch.setenv("FULL_VIEW_GOVERNANCE_ADAPTER", "http")
    monkeypatch.delenv("FULL_VIEW_CREDENTIAL_KEY", raising=False)

    with pytest.raises(RuntimeError, match="FULL_VIEW_CREDENTIAL_KEY"):
        RuntimeContainer()


def test_production_runtime_requires_stable_cursor_signing_key(monkeypatch) -> None:
    monkeypatch.setenv("FULL_VIEW_RUNTIME_PROFILE", "production")
    monkeypatch.setenv(
        "FULL_VIEW_DATABASE_URL",
        "postgresql://agent:password@database.invalid/agent",
    )
    monkeypatch.setenv("FULL_VIEW_GOVERNANCE_ADAPTER", "http")
    monkeypatch.setenv(
        "FULL_VIEW_CREDENTIAL_KEY",
        base64.urlsafe_b64encode(b"c" * 32).decode(),
    )
    monkeypatch.delenv("FULL_VIEW_CURSOR_KEY", raising=False)

    with pytest.raises(RuntimeError, match="FULL_VIEW_CURSOR_KEY"):
        RuntimeContainer()


def test_production_runtime_rejects_in_memory_business_adapter(monkeypatch) -> None:
    monkeypatch.setenv("FULL_VIEW_RUNTIME_PROFILE", "production")
    monkeypatch.setenv(
        "FULL_VIEW_DATABASE_URL",
        "postgresql://agent:password@database.invalid/agent",
    )
    monkeypatch.setenv(
        "FULL_VIEW_CREDENTIAL_KEY",
        base64.urlsafe_b64encode(b"c" * 32).decode(),
    )
    monkeypatch.setenv(
        "FULL_VIEW_CURSOR_KEY",
        base64.urlsafe_b64encode(b"k" * 32).decode(),
    )
    monkeypatch.setenv("FULL_VIEW_GOVERNANCE_ADAPTER", "memory")

    with pytest.raises(RuntimeError, match="FULL_VIEW_GOVERNANCE_ADAPTER=http"):
        RuntimeContainer()


def test_production_runtime_requires_openai_compatible_model_provider(
    monkeypatch,
) -> None:
    monkeypatch.setenv("FULL_VIEW_RUNTIME_PROFILE", "production")
    monkeypatch.setenv(
        "FULL_VIEW_DATABASE_URL",
        "postgresql://agent:password@database.invalid/agent",
    )
    monkeypatch.setenv(
        "FULL_VIEW_CREDENTIAL_KEY",
        base64.urlsafe_b64encode(b"c" * 32).decode(),
    )
    monkeypatch.setenv(
        "FULL_VIEW_CURSOR_KEY",
        base64.urlsafe_b64encode(b"k" * 32).decode(),
    )
    monkeypatch.setenv("FULL_VIEW_GOVERNANCE_ADAPTER", "http")
    monkeypatch.delenv("FULL_VIEW_MODEL_PROVIDER", raising=False)

    with pytest.raises(
        RuntimeError,
        match="FULL_VIEW_MODEL_PROVIDER=openai_compatible",
    ):
        RuntimeContainer()


class QueueModelProvider:
    def __init__(self) -> None:
        self.requests = []
        self.responses = [
            ModelResponse(
                content=None,
                tool_calls=(
                    # S1-B：生产准入下人口规范 Tool 被语义入口遮蔽，模型
                    # 只能调用 semantic_query；catalog_version/fingerprint
                    # 由服务端注入，模型参数不得携带（携带即契约错误）。
                    ModelToolCall(
                        tool_id="governance.semantic_query",
                        arguments={
                            "spec": {
                                "subject": "population",
                                "metrics": ["person_count"],
                                "scope": {"area_code": "330106"},
                                "filters": [
                                    {
                                        "field": "person_category",
                                        "operator": "eq",
                                        "value": "solitary_elderly",
                                    }
                                ],
                                "group_by": ["street"],
                            }
                        },
                    ),
                ),
                finish_reason="tool_calls",
            ),
        ]

    async def complete(self, request):
        self.requests.append(request)
        if self.responses:
            return self.responses.pop(0)
        observation = json.loads(
            next(
                message.content
                for message in reversed(request.messages)
                if message.role == "tool"
            )
        )
        data_result = observation["data_result"]
        row = data_result["sample_rows"][0]
        return ModelResponse(
            content=None,
            tool_calls=(
                ModelToolCall(
                    tool_id=FINISH_TOOL_ID,
                    arguments={
                        "kind": "claims",
                        "summary": "根据已验证结果，人口指标查询已完成。",
                        "claims": [
                            {
                                "claim_id": "claim-1",
                                "result_id": data_result["result_id"],
                                "result_fingerprint": data_result["result_fingerprint"],
                                "collection": "rows",
                                "row_locator": {"area_code": row["area_code"]},
                                "field": "person_count",
                                "operation": "value",
                                "value": row["person_count"],
                            }
                        ],
                    },
                ),
            ),
            finish_reason="tool_calls",
        )


def run_request(*, message_id: str, client_instance_id: str) -> RunCreateRequest:
    return RunCreateRequest.model_validate(
        {
            "input": {
                "client_message_id": message_id,
                "content": [{"type": "text", "text": "查询独居老人数量"}],
            },
            "client": {
                "client_instance_id": client_instance_id,
                "frontend_command_schema_versions": ["1.1"],
                "supported_commands": ["panel.show_table"],
            },
            "mode": "agent",
        }
    )


@pytest.mark.asyncio
async def test_create_session_requires_geotoken_header() -> None:
    app = create_app(runtime_fixture())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/agent-api/v1/sessions",
            headers={"Idempotency-Key": "idem-unauth-session"},
            json={"title": "独居老人分析"},
        )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthenticated"


@pytest.mark.asyncio
async def test_request_validation_uses_unified_error_envelope() -> None:
    app = create_app(runtime_fixture())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/agent-api/v1/sessions",
            headers={
                "geoToken": "test-token-user-validation",
                "Idempotency-Key": "idem-validation-error",
            },
            json={"title": ""},
        )

    assert response.status_code == 422
    payload = response.json()
    assert payload["error"]["code"] == "validation_error"
    assert payload["error"]["retryable"] is False
    assert payload["error"]["details"] == [
        {
            "field": "title",
            "code": "string_too_short",
            "message": "String should have at least 1 character",
        }
    ]
    assert payload["meta"]["request_id"].startswith("req_")
    assert payload["meta"]["trace_id"].startswith("trc_")


@pytest.mark.asyncio
async def test_api_rejects_geotoken_in_url_even_when_header_is_valid() -> None:
    app = create_app(runtime_fixture())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/agent-api/v1/sessions?geoToken=url-token-must-not-be-accepted",
            headers={
                "geoToken": "header-token",
                "Idempotency-Key": "idem-url-token-rejected",
            },
            json={"title": "URL Token 拒绝测试"},
        )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_authentication_transport"


@pytest.mark.asyncio
async def test_api_rejects_geotoken_and_bearer_on_the_same_request() -> None:
    app = create_app(runtime_fixture())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/agent-api/v1/sessions",
            headers={
                "geoToken": "header-token",
                "Authorization": "Bearer bearer-token",
                "Idempotency-Key": "idem-double-auth-rejected",
            },
            json={"title": "双身份拒绝测试"},
        )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_authentication_transport"


@pytest.mark.asyncio
async def test_create_session_returns_owned_session_without_echoing_token() -> None:
    app = create_app(runtime_fixture())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/agent-api/v1/sessions",
            headers={
                "geoToken": "test-token-user-01",
                "Idempotency-Key": "idem-session-01",
            },
            json={"title": "独居老人分析"},
        )

    assert response.status_code == 201
    payload = response.json()
    assert payload["data"]["title"] == "独居老人分析"
    assert payload["data"]["active_run_id"] is None
    assert "test-token-user-01" not in response.text


@pytest.mark.asyncio
async def test_session_workspace_api_supports_detail_rename_archive_and_ownership() -> None:
    runtime = runtime_fixture()
    app = create_app(runtime)
    owner = {"geoToken": "session-workspace-owner"}
    other = {"geoToken": "session-workspace-other"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        created = await client.post(
            "/agent-api/v1/sessions",
            headers={**owner, "Idempotency-Key": "idem-workspace-create"},
            json={"title": "原会话标题"},
        )
        session_id = created.json()["data"]["session_id"]
        renamed = await client.patch(
            f"/agent-api/v1/sessions/{session_id}",
            headers={**owner, "Idempotency-Key": "idem-workspace-rename"},
            json={"title": "新会话标题"},
        )
        rename_replay = await client.patch(
            f"/agent-api/v1/sessions/{session_id}",
            headers={**owner, "Idempotency-Key": "idem-workspace-rename"},
            json={"title": "新会话标题"},
        )
        detail = await client.get(
            f"/agent-api/v1/sessions/{session_id}",
            headers=owner,
        )
        hidden = await client.get(
            f"/agent-api/v1/sessions/{session_id}",
            headers=other,
        )
        archived = await client.patch(
            f"/agent-api/v1/sessions/{session_id}",
            headers={**owner, "Idempotency-Key": "idem-workspace-archive"},
            json={"status": "archived"},
        )

    assert renamed.status_code == 200
    assert renamed.json()["data"]["title"] == "新会话标题"
    assert rename_replay.json()["meta"]["idempotency_replayed"] is True
    assert detail.json()["data"]["title"] == "新会话标题"
    assert hidden.status_code == 404
    assert archived.json()["data"]["status"] == "archived"


@pytest.mark.asyncio
async def test_session_list_uses_owned_filtered_signed_cursor_and_recovers_active_run() -> None:
    runtime = runtime_fixture()
    app = create_app(runtime)
    auth = {"geoToken": "session-list-owner"}
    other = {"geoToken": "session-list-other"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        session_ids = []
        for index in range(3):
            response = await client.post(
                "/agent-api/v1/sessions",
                headers={
                    **auth,
                    "Idempotency-Key": f"idem-session-list-{index}",
                },
                json={"title": f"会话 {index}"},
            )
            session_ids.append(response.json()["data"]["session_id"])
        await client.post(
            "/agent-api/v1/sessions",
            headers={**other, "Idempotency-Key": "idem-session-list-other"},
            json={"title": "其他用户会话"},
        )
        await client.patch(
            f"/agent-api/v1/sessions/{session_ids[1]}",
            headers={**auth, "Idempotency-Key": "idem-session-list-archive"},
            json={"status": "archived"},
        )
        owner_user_id = runtime.store.sessions[session_ids[0]].owner_user_id
        active_run = await runtime.service.create_run(
            user_id=owner_user_id,
            session_id=session_ids[0],
            request=run_request(
                message_id="web-msg-session-recovery",
                client_instance_id="cli-session-recovery",
            ),
        )

        first_page = await client.get(
            "/agent-api/v1/sessions",
            params={"status": "active", "limit": 1},
            headers=auth,
        )
        cursor = first_page.json()["meta"]["next_cursor"]
        second_page = await client.get(
            "/agent-api/v1/sessions",
            params={"status": "active", "limit": 1, "cursor": cursor},
            headers=auth,
        )
        archived_page = await client.get(
            "/agent-api/v1/sessions",
            params={"status": "archived", "limit": 10},
            headers=auth,
        )
        wrong_filter = await client.get(
            "/agent-api/v1/sessions",
            params={"status": "archived", "limit": 1, "cursor": cursor},
            headers=auth,
        )
        recovery_detail = await client.get(
            f"/agent-api/v1/sessions/{session_ids[0]}",
            headers=auth,
        )

    active_data = first_page.json()["data"] + second_page.json()["data"]
    assert first_page.status_code == 200
    assert cursor
    assert len(active_data) == 2
    assert all(item["status"] == "active" for item in active_data)
    assert archived_page.json()["data"][0]["session_id"] == session_ids[1]
    assert wrong_filter.status_code == 422
    assert wrong_filter.json()["error"]["code"] == "validation_error"
    assert recovery_detail.json()["data"]["active_run_id"] == active_run.run_id


@pytest.mark.asyncio
async def test_failed_run_admission_releases_session_and_terminalizes_created_run() -> None:
    runtime = RuntimeContainer(
        identity_port=HashedLegacyIdentityAdapter(),
        credentials=UnconfiguredCredentialBroker(),
    )
    app = create_app(runtime)
    auth = {"geoToken": "admission-failure-token"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        session_response = await client.post(
            "/agent-api/v1/sessions",
            headers={**auth, "Idempotency-Key": "idem-admission-failure-session"},
            json={"title": "准入失败补偿"},
        )
        session_id = session_response.json()["data"]["session_id"]
        run_response = await client.post(
            f"/agent-api/v1/sessions/{session_id}/runs",
            headers={**auth, "Idempotency-Key": "idem-admission-failure-run"},
            json=run_request(
                message_id="web-msg-admission-failure",
                client_instance_id="cli-admission-failure",
            ).model_dump(mode="json"),
        )

    assert run_response.status_code == 404
    session = runtime.store.sessions[session_id]
    created_run = next(iter(runtime.store.runs.values()))
    assert session.active_run_id is None
    assert created_run.status == "failed"
    assert created_run.completion_reason_code == "run_admission_failed"


@pytest.mark.asyncio
async def test_create_run_executes_mock_tool_and_reaches_success() -> None:
    app = create_app(runtime_fixture())
    headers = {"geoToken": "test-token-user-01", "Idempotency-Key": "idem-run-01"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        session_response = await client.post(
            "/agent-api/v1/sessions",
            headers={
                "geoToken": "test-token-user-01",
                "Idempotency-Key": "idem-session-run-test",
            },
            json={"title": "独居老人分析"},
        )
        session_id = session_response.json()["data"]["session_id"]
        run_response = await client.post(
            f"/agent-api/v1/sessions/{session_id}/runs",
            headers=headers,
            json={
                "input": {
                    "client_message_id": "web-msg-api-01",
                    "content": [{"type": "text", "text": "查询独居老人数量"}],
                },
                "client": {
                    "client_instance_id": "cli-api-01",
                    "frontend_command_schema_versions": ["1.1"],
                    "supported_commands": ["panel.show_table"],
                },
                "mode": "agent",
            },
        )
        assert run_response.status_code == 202
        run_id = run_response.json()["data"]["run_id"]

        for _ in range(100):
            run_get = await client.get(
                f"/agent-api/v1/runs/{run_id}",
                headers={"geoToken": "test-token-user-01"},
            )
            if run_get.status_code == 200 and run_get.json()["data"]["status"] == "completed":
                break
            await asyncio.sleep(0.001)

    assert run_get.status_code == 200
    assert run_get.json()["data"]["outcome"] == "success"


@pytest.mark.asyncio
async def test_frontend_command_receipt_is_idempotent_and_bound_to_target_client() -> None:
    runtime = runtime_fixture()
    app = create_app(runtime)
    auth = {"geoToken": "test-token-command-receipt"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        session_response = await client.post(
            "/agent-api/v1/sessions",
            headers={**auth, "Idempotency-Key": "idem-command-session"},
            json={"title": "前端命令回执"},
        )
        session_id = session_response.json()["data"]["session_id"]
        run_response = await client.post(
            f"/agent-api/v1/sessions/{session_id}/runs",
            headers={**auth, "Idempotency-Key": "idem-command-run"},
            json={
                "input": {
                    "client_message_id": "web-command-01",
                    "content": [{"type": "text", "text": "查询独居老人数量"}],
                },
                "client": {
                    "client_instance_id": "cli-command-01",
                    "frontend_command_schema_versions": ["1.1"],
                    "supported_commands": ["panel.show_table"],
                },
                "mode": "agent",
            },
        )
        run_id = run_response.json()["data"]["run_id"]
        for _ in range(100):
            run_get = await client.get(f"/agent-api/v1/runs/{run_id}", headers=auth)
            if run_get.json()["data"]["status"] == "completed":
                break
            await asyncio.sleep(0.001)

        events = await runtime.events.list_events(run_id=run_id)
        command_event = next(
            event for event in events if event.type == "frontend.command.requested"
        )
        command = command_event.data["command"]
        receipt = {
            "schema_version": "1.1",
            "command_id": command["command_id"],
            "client_instance_id": "cli-command-01",
            "status": "completed",
            "received_at": "2026-07-18T10:00:00Z",
            "completed_at": "2026-07-18T10:00:01Z",
            "client_state": {"route_id": "agent_workspace"},
            "error": None,
        }
        receipt_url = (
            f"/agent-api/v1/runs/{run_id}/frontend-command-receipts/"
            f"{command['command_id']}"
        )
        first = await client.put(receipt_url, headers=auth, json=receipt)
        replay = await client.put(receipt_url, headers=auth, json=receipt)
        mismatch = await client.put(
            receipt_url,
            headers=auth,
            json={**receipt, "client_instance_id": "cli-command-other"},
        )

    assert first.status_code == 200
    assert first.json()["data"] == receipt
    assert replay.status_code == 200
    assert replay.json()["data"] == receipt
    assert mismatch.status_code == 403
    assert mismatch.json()["error"]["code"] == "command_client_mismatch"


@pytest.mark.asyncio
async def test_injected_model_provider_drives_the_runtime_agent_loop() -> None:
    provider = QueueModelProvider()
    runtime = RuntimeContainer(
        identity_port=HashedLegacyIdentityAdapter(),
        credentials=InMemoryCredentialBroker(),
        model_provider=provider,
    )
    app = create_app(runtime)
    auth = {"geoToken": "test-token-dynamic-model"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        session_response = await client.post(
            "/agent-api/v1/sessions",
            headers={**auth, "Idempotency-Key": "idem-dynamic-session"},
            json={"title": "动态模型规划"},
        )
        session_id = session_response.json()["data"]["session_id"]
        run_response = await client.post(
            f"/agent-api/v1/sessions/{session_id}/runs",
            headers={**auth, "Idempotency-Key": "idem-dynamic-run"},
            json=run_request(
                message_id="web-msg-dynamic",
                client_instance_id="cli-dynamic",
            ).model_dump(mode="json"),
        )
        run_id = run_response.json()["data"]["run_id"]
        for _ in range(100):
            run_get = await client.get(
                f"/agent-api/v1/runs/{run_id}",
                headers=auth,
            )
            if run_get.json()["data"]["status"] in {
                "completed",
                "failed",
                "cancelled",
            }:
                break
            await asyncio.sleep(0.001)

    assert run_get.json()["data"]["status"] == "completed"
    assert len(provider.requests) == 2
    assert any(
        "查询独居老人数量" in message.content
        for message in provider.requests[0].messages
    )
    # Second request includes tool results in standard OpenAI format
    last_msgs = provider.requests[1].messages
    tool_msgs = [m for m in last_msgs if m.role == "tool"]
    assert len(tool_msgs) >= 1
    # S1-B：模型侧入口为语义虚拟 Tool，且回放消息中携带服务端注入的
    # 目录版本钉扎；观察仍由规范人口 Tool 产生（语义入口执行同一规范链路）。
    semantic_calls = [
        call
        for message in last_msgs
        if message.role == "assistant"
        for call in message.tool_calls
        if call.tool_id == "governance.semantic_query"
    ]
    assert len(semantic_calls) == 1
    assert "catalog_version" in semantic_calls[0].arguments
    assert "catalog_fingerprint" in semantic_calls[0].arguments
    assert "governance.query_population_metrics" in tool_msgs[-1].content


@pytest.mark.asyncio
async def test_session_messages_api_returns_the_persisted_user_message() -> None:
    app = create_app(runtime_fixture())
    auth = {"geoToken": "test-token-user-01"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        session_response = await client.post(
            "/agent-api/v1/sessions",
            headers={**auth, "Idempotency-Key": "idem-message-session"},
            json={"title": "消息查询"},
        )
        session_id = session_response.json()["data"]["session_id"]
        run_response = await client.post(
            f"/agent-api/v1/sessions/{session_id}/runs",
            headers={**auth, "Idempotency-Key": "idem-message-run"},
            json=run_request(
                message_id="web-msg-list-01",
                client_instance_id="cli-message-list",
            ).model_dump(mode="json"),
        )

        response = await client.get(
            f"/agent-api/v1/sessions/{session_id}/messages",
            headers=auth,
        )

    assert run_response.status_code == 202
    assert response.status_code == 200
    payload = response.json()
    assert len(payload["data"]) >= 1
    assert payload["data"][0]["role"] == "user"
    assert payload["data"][0]["content"] == [
        {"type": "text", "text": "查询独居老人数量"}
    ]
    assert payload["meta"]["has_next"] is False


@pytest.mark.asyncio
async def test_session_messages_api_uses_a_signed_cursor() -> None:
    app = create_app(runtime_fixture())
    auth = {"geoToken": "test-token-message-pagination"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        session_response = await client.post(
            "/agent-api/v1/sessions",
            headers={**auth, "Idempotency-Key": "idem-message-page-session"},
            json={"title": "消息分页"},
        )
        session_id = session_response.json()["data"]["session_id"]
        first_run_response = await client.post(
            f"/agent-api/v1/sessions/{session_id}/runs",
            headers={**auth, "Idempotency-Key": "idem-message-page-run-1"},
            json=run_request(
                message_id="web-msg-page-01",
                client_instance_id="cli-message-page",
            ).model_dump(mode="json"),
        )
        first_run_id = first_run_response.json()["data"]["run_id"]
        for _ in range(100):
            first_run = await client.get(
                f"/agent-api/v1/runs/{first_run_id}",
                headers=auth,
            )
            if first_run.json()["data"]["status"] == "completed":
                break
            await asyncio.sleep(0.001)
        second_request = run_request(
            message_id="web-msg-page-02",
            client_instance_id="cli-message-page",
        ).model_dump(mode="json")
        second_request["input"]["content"][0]["text"] = "第二个问题"
        second_response = await client.post(
            f"/agent-api/v1/sessions/{session_id}/runs",
            headers={**auth, "Idempotency-Key": "idem-message-page-run-2"},
            json=second_request,
        )

        first_page = await client.get(
            f"/agent-api/v1/sessions/{session_id}/messages?limit=1",
            headers=auth,
        )
        cursor = first_page.json()["meta"]["next_cursor"]
        second_page = await client.get(
            f"/agent-api/v1/sessions/{session_id}/messages",
            params={"limit": 1, "cursor": cursor},
            headers=auth,
        )
        third_page = await client.get(
            f"/agent-api/v1/sessions/{session_id}/messages",
            params={
                "limit": 1,
                "cursor": second_page.json()["meta"]["next_cursor"],
            },
            headers=auth,
        )

    assert second_response.status_code == 202
    assert len(first_page.json()["data"]) == 1
    assert first_page.json()["meta"]["has_next"] is True
    assert cursor
    assert len(second_page.json()["data"]) == 1
    assert second_page.json()["data"][0]["role"] == "assistant"
    assert third_page.json()["data"][0]["content"][0]["text"] == "第二个问题"


@pytest.mark.asyncio
async def test_completed_run_persists_an_assistant_message_with_result_evidence() -> None:
    runtime = runtime_fixture()
    app = create_app(runtime)
    auth = {"geoToken": "test-token-assistant-message"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        session_response = await client.post(
            "/agent-api/v1/sessions",
            headers={**auth, "Idempotency-Key": "idem-assistant-message-session"},
            json={"title": "助手消息"},
        )
        session_id = session_response.json()["data"]["session_id"]
        run_response = await client.post(
            f"/agent-api/v1/sessions/{session_id}/runs",
            headers={**auth, "Idempotency-Key": "idem-assistant-message-run"},
            json=run_request(
                message_id="web-msg-assistant-01",
                client_instance_id="cli-assistant-message",
            ).model_dump(mode="json"),
        )
        run_id = run_response.json()["data"]["run_id"]
        for _ in range(100):
            run_get = await client.get(
                f"/agent-api/v1/runs/{run_id}",
                headers=auth,
            )
            if run_get.json()["data"]["status"] == "completed":
                break
            await asyncio.sleep(0.001)
        messages_response = await client.get(
            f"/agent-api/v1/sessions/{session_id}/messages",
            headers=auth,
        )

    messages = messages_response.json()["data"]
    assistant = messages[-1]
    events = await runtime.events.list_events(run_id=run_id)
    completed_event = next(event for event in events if event.type == "run.completed")

    assert [message["role"] for message in messages] == ["user", "assistant"]
    assert assistant["content"][0]["type"] == "text"
    assert assistant["content"][1]["type"] == "result_reference"
    assert assistant["content"][1]["result_id"]
    assert len(assistant["evidence_ids"]) == 1
    assert completed_event.data["result_message_id"] == assistant["message_id"]


@pytest.mark.asyncio
async def test_create_run_admits_auth_context_used_by_executor() -> None:
    runtime = runtime_fixture()
    app = create_app(runtime)
    token = "test-token-admission-user"
    auth = {"geoToken": token}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        session_response = await client.post(
            "/agent-api/v1/sessions",
            headers={**auth, "Idempotency-Key": "idem-admission-session"},
            json={"title": "Admission 集成测试"},
        )
        session_id = session_response.json()["data"]["session_id"]
        owner_user_id = runtime.store.sessions[session_id].owner_user_id
        run_response = await client.post(
            f"/agent-api/v1/sessions/{session_id}/runs",
            headers={**auth, "Idempotency-Key": "idem-admission-run"},
            json=run_request(
                message_id="web-msg-admission-01",
                client_instance_id="cli-admission-01",
            ).model_dump(mode="json"),
        )
        run_id = run_response.json()["data"]["run_id"]
        for _ in range(100):
            run_get = await client.get(f"/agent-api/v1/runs/{run_id}", headers=auth)
            if run_get.json()["data"]["status"] == "completed":
                break
            await asyncio.sleep(0.001)

    auth_context = await runtime.auth_contexts.get(
        user_id=owner_user_id,
        run_id=run_id,
    )
    events = await runtime.events.list_events(run_id=run_id)
    tool_completed = next(event for event in events if event.type == "tool.completed")
    assert auth_context.run_id == run_id
    assert auth_context.session_id == session_id
    assert auth_context.credential_ref.startswith("cred_")
    assert (
        tool_completed.data["tool_result"]["policy"]["auth_context_fingerprint"]
        == auth_context.auth_context_fingerprint
    )
    assert token not in str(tool_completed.model_dump(mode="json"))


@pytest.mark.asyncio
async def test_create_run_rejects_unregistered_workflow_without_occupying_session() -> None:
    runtime = runtime_fixture()
    app = create_app(runtime)
    auth = {"geoToken": "test-token-unregistered-workflow"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        session_response = await client.post(
            "/agent-api/v1/sessions",
            headers={**auth, "Idempotency-Key": "idem-workflow-session"},
            json={"title": "未注册工作流"},
        )
        session_id = session_response.json()["data"]["session_id"]
        response = await client.post(
            f"/agent-api/v1/sessions/{session_id}/runs",
            headers={**auth, "Idempotency-Key": "idem-workflow-run"},
            json={
                **run_request(
                    message_id="web-msg-workflow",
                    client_instance_id="cli-workflow",
                ).model_dump(mode="json"),
                "mode": "workflow",
                "workflow_ref": {
                    "workflow_id": "does-not-exist",
                    "workflow_version": "999.0",
                },
            },
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "workflow_not_available"
    assert runtime.store.sessions[session_id].active_run_id is None


@pytest.mark.asyncio
async def test_sse_endpoint_returns_run_events_without_token_in_payload() -> None:
    app = create_app(runtime_fixture())
    auth = {"geoToken": "test-token-sse-user"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        session_response = await client.post(
            "/agent-api/v1/sessions",
            headers={**auth, "Idempotency-Key": "idem-session-sse"},
            json={"title": "SSE 测试"},
        )
        session_id = session_response.json()["data"]["session_id"]
        run_response = await client.post(
            f"/agent-api/v1/sessions/{session_id}/runs",
            headers={**auth, "Idempotency-Key": "idem-sse-run"},
            json={
                "input": {
                    "client_message_id": "web-msg-sse-01",
                    "content": [{"type": "text", "text": "查询独居老人数量"}],
                },
                "client": {
                    "client_instance_id": "cli-sse-01",
                    "frontend_command_schema_versions": ["1.1"],
                    "supported_commands": ["panel.show_table"],
                },
                "mode": "agent",
            },
        )
        run_id = run_response.json()["data"]["run_id"]
        for _ in range(100):
            run_get = await client.get(f"/agent-api/v1/runs/{run_id}", headers=auth)
            if run_get.json()["data"]["status"] == "completed":
                break
            await asyncio.sleep(0.001)

        events_response = await client.get(
            f"/agent-api/v1/runs/{run_id}/events",
            headers=auth,
        )

    assert events_response.status_code == 200
    assert events_response.headers["content-type"].startswith("text/event-stream")
    assert "event: run.completed" in events_response.text
    assert "test-token-sse-user" not in events_response.text


@pytest.mark.asyncio
async def test_sse_rejects_unknown_last_event_id_before_starting_stream() -> None:
    app = create_app(runtime_fixture())
    auth = {"geoToken": "test-token-expired-sse-cursor"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        session_response = await client.post(
            "/agent-api/v1/sessions",
            headers={**auth, "Idempotency-Key": "idem-expired-sse-session"},
            json={"title": "SSE 过期游标"},
        )
        session_id = session_response.json()["data"]["session_id"]
        run_response = await client.post(
            f"/agent-api/v1/sessions/{session_id}/runs",
            headers={**auth, "Idempotency-Key": "idem-expired-sse-run"},
            json=run_request(
                message_id="web-msg-expired-sse",
                client_instance_id="cli-expired-sse",
            ).model_dump(mode="json"),
        )
        run_id = run_response.json()["data"]["run_id"]
        for _ in range(100):
            run_get = await client.get(f"/agent-api/v1/runs/{run_id}", headers=auth)
            if run_get.json()["data"]["status"] == "completed":
                break
            await asyncio.sleep(0.001)

        response = await client.get(
            f"/agent-api/v1/runs/{run_id}/events",
            headers={**auth, "Last-Event-ID": "evt-no-longer-available"},
        )

    assert response.status_code == 410
    assert response.json()["error"]["code"] == "event_history_expired"


@pytest.mark.asyncio
async def test_cancel_endpoint_cancels_an_active_run_and_emits_event() -> None:
    runtime = runtime_fixture()
    app = create_app(runtime)
    auth = {"geoToken": "test-token-cancel-user"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        session_response = await client.post(
            "/agent-api/v1/sessions",
            headers={**auth, "Idempotency-Key": "idem-session-cancel"},
            json={"title": "取消测试"},
        )
        session_id = session_response.json()["data"]["session_id"]
        owner_user_id = runtime.store.sessions[session_id].owner_user_id
        run = await runtime.service.create_run(
            user_id=owner_user_id,
            session_id=session_id,
            request=run_request(
                message_id="web-msg-cancel-01",
                client_instance_id="cli-cancel-01",
            ),
        )
        await runtime.service.start_run(user_id=owner_user_id, run_id=run.run_id)

        response = await client.post(
            f"/agent-api/v1/runs/{run.run_id}/cancel",
            headers=auth,
        )
        replay = await client.post(
            f"/agent-api/v1/runs/{run.run_id}/cancel",
            headers=auth,
        )

    assert response.status_code == 202
    assert response.json()["data"]["status"] == "cancelled"
    assert replay.status_code == 202
    assert replay.json()["data"]["run_id"] == response.json()["data"]["run_id"]
    events = await runtime.events.list_events(run_id=run.run_id)
    assert [event.type for event in events] == ["run.cancelled"]
    assert "test-token-cancel-user" not in response.text


@pytest.mark.asyncio
async def test_cancel_endpoint_best_effort_cancels_active_tool_task() -> None:
    from full_view_agent.application.mock_executor import MockRunExecutor

    class CancellableCapability:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.cancelled = asyncio.Event()

        async def execute(self, **_kwargs):
            self.started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled.set()
                raise

    runtime = runtime_fixture()
    capability = CancellableCapability()
    runtime.executor = MockRunExecutor(
        service=runtime.service,
        store=runtime.store,
        events=runtime.events,
        capability=capability,
        auth_context_provider=runtime.auth_contexts,
    )
    app = create_app(runtime)
    auth = {"geoToken": "test-token-cancel-active-tool"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        session_response = await client.post(
            "/agent-api/v1/sessions",
            headers={**auth, "Idempotency-Key": "idem-cancel-task-session"},
            json={"title": "活动 Tool 取消测试"},
        )
        session_id = session_response.json()["data"]["session_id"]
        run_response = await client.post(
            f"/agent-api/v1/sessions/{session_id}/runs",
            headers={**auth, "Idempotency-Key": "idem-cancel-task-run"},
            json=run_request(
                message_id="web-msg-cancel-task",
                client_instance_id="cli-cancel-task",
            ).model_dump(mode="json"),
        )
        run_id = run_response.json()["data"]["run_id"]
        await capability.started.wait()
        response = await client.post(
            f"/agent-api/v1/runs/{run_id}/cancel",
            headers=auth,
        )
        await asyncio.wait_for(capability.cancelled.wait(), timeout=1)

    assert response.status_code == 202
    assert response.json()["data"]["status"] == "cancelled"


@pytest.mark.asyncio
async def test_steer_endpoint_accepts_instruction_for_next_safe_checkpoint() -> None:
    runtime = runtime_fixture()
    app = create_app(runtime)
    auth = {"geoToken": "test-token-steer-user"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        session_response = await client.post(
            "/agent-api/v1/sessions",
            headers={**auth, "Idempotency-Key": "idem-session-steer"},
            json={"title": "Steer 测试"},
        )
        session_id = session_response.json()["data"]["session_id"]
        owner_user_id = runtime.store.sessions[session_id].owner_user_id
        run = await runtime.service.create_run(
            user_id=owner_user_id,
            session_id=session_id,
            request=run_request(
                message_id="web-msg-steer-01",
                client_instance_id="cli-steer-01",
            ),
        )
        await runtime.service.start_run(user_id=owner_user_id, run_id=run.run_id)

        steer_payload = {
            "client_instance_id": "cli-steer-01",
            "content": "结果出来后再筛选 80 岁以上。",
        }
        response = await client.post(
            f"/agent-api/v1/runs/{run.run_id}/steers",
            headers={**auth, "Idempotency-Key": "idem-steer-01"},
            json=steer_payload,
        )
        replay = await client.post(
            f"/agent-api/v1/runs/{run.run_id}/steers",
            headers={**auth, "Idempotency-Key": "idem-steer-01"},
            json=steer_payload,
        )

    assert response.status_code == 202
    assert response.json()["data"]["status"] == "accepted"
    assert response.json()["data"]["delivery"] == "next_safe_checkpoint"
    assert replay.status_code == 202
    assert replay.json()["data"]["steer_id"] == response.json()["data"]["steer_id"]
    assert replay.json()["meta"]["idempotency_replayed"] is True
    events = await runtime.events.list_events(run_id=run.run_id)
    assert [event.type for event in events] == ["steer.accepted"]
    assert "test-token-steer-user" not in response.text


@pytest.mark.asyncio
async def test_create_session_idempotency_replays_and_rejects_changed_body() -> None:
    app = create_app(runtime_fixture())
    headers = {
        "geoToken": "test-token-idempotency-user",
        "Idempotency-Key": "idem-session-replay-01",
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        first = await client.post(
            "/agent-api/v1/sessions",
            headers=headers,
            json={"title": "同一请求"},
        )
        replay = await client.post(
            "/agent-api/v1/sessions",
            headers=headers,
            json={"title": "同一请求"},
        )
        conflict = await client.post(
            "/agent-api/v1/sessions",
            headers=headers,
            json={"title": "已改变的请求"},
        )

    assert first.status_code == 201
    assert replay.status_code == 201
    assert replay.json()["data"]["session_id"] == first.json()["data"]["session_id"]
    assert replay.json()["meta"]["idempotency_replayed"] is True
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "idempotency_conflict"


@pytest.mark.asyncio
async def test_create_run_idempotency_schedules_execution_only_once() -> None:
    runtime = runtime_fixture()
    app = create_app(runtime)
    auth = {"geoToken": "test-token-run-idempotency-user"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        session_response = await client.post(
            "/agent-api/v1/sessions",
            headers={**auth, "Idempotency-Key": "idem-session-for-run-replay"},
            json={"title": "Run 幂等测试"},
        )
        session_id = session_response.json()["data"]["session_id"]
        payload = run_request(
            message_id="web-msg-run-replay-01",
            client_instance_id="cli-run-replay-01",
        ).model_dump(mode="json")
        headers = {**auth, "Idempotency-Key": "idem-run-replay-01"}

        first = await client.post(
            f"/agent-api/v1/sessions/{session_id}/runs",
            headers=headers,
            json=payload,
        )
        replay = await client.post(
            f"/agent-api/v1/sessions/{session_id}/runs",
            headers=headers,
            json=payload,
        )
        run_id = first.json()["data"]["run_id"]
        for _ in range(100):
            run_get = await client.get(f"/agent-api/v1/runs/{run_id}", headers=auth)
            if run_get.json()["data"]["status"] == "completed":
                break
            await asyncio.sleep(0.001)

    assert first.status_code == 202
    assert replay.status_code == 202
    assert replay.json()["data"]["run_id"] == run_id
    assert replay.json()["meta"]["idempotency_replayed"] is True
    events = await runtime.events.list_events(run_id=run_id)
    assert [event.type for event in events].count("run.started") == 1


@pytest.mark.asyncio
async def test_client_message_id_replays_same_run_across_different_idempotency_keys() -> None:
    runtime = runtime_fixture()
    app = create_app(runtime)
    auth = {"geoToken": "test-token-client-message-replay"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        session_response = await client.post(
            "/agent-api/v1/sessions",
            headers={**auth, "Idempotency-Key": "idem-client-message-session"},
            json={"title": "消息自然幂等"},
        )
        session_id = session_response.json()["data"]["session_id"]
        payload = run_request(
            message_id="web-msg-natural-replay",
            client_instance_id="cli-natural-replay",
        ).model_dump(mode="json")

        first, second = await asyncio.gather(
            client.post(
                f"/agent-api/v1/sessions/{session_id}/runs",
                headers={**auth, "Idempotency-Key": "idem-natural-run-a"},
                json=payload,
            ),
            client.post(
                f"/agent-api/v1/sessions/{session_id}/runs",
                headers={**auth, "Idempotency-Key": "idem-natural-run-b"},
                json=payload,
            ),
        )

    assert first.status_code == 202
    assert second.status_code == 202
    assert first.json()["data"]["run_id"] == second.json()["data"]["run_id"]
    assert sorted(
        [
            first.json()["meta"]["idempotency_replayed"],
            second.json()["meta"]["idempotency_replayed"],
        ]
    ) == [False, True]


@pytest.mark.asyncio
async def test_client_message_id_rejects_changed_run_body() -> None:
    app = create_app(runtime_fixture())
    auth = {"geoToken": "test-token-client-message-conflict"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        session_response = await client.post(
            "/agent-api/v1/sessions",
            headers={**auth, "Idempotency-Key": "idem-client-conflict-session"},
            json={"title": "消息冲突"},
        )
        session_id = session_response.json()["data"]["session_id"]
        original = run_request(
            message_id="web-msg-natural-conflict",
            client_instance_id="cli-natural-conflict",
        ).model_dump(mode="json")
        changed = {
            **original,
            "input": {
                **original["input"],
                "content": [{"type": "text", "text": "这是不同的问题"}],
            },
        }
        await client.post(
            f"/agent-api/v1/sessions/{session_id}/runs",
            headers={**auth, "Idempotency-Key": "idem-natural-conflict-a"},
            json=original,
        )
        response = await client.post(
            f"/agent-api/v1/sessions/{session_id}/runs",
            headers={**auth, "Idempotency-Key": "idem-natural-conflict-b"},
            json=changed,
        )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "idempotency_conflict"


@pytest.mark.asyncio
async def test_get_result_returns_typed_payload_only_to_its_owner() -> None:
    runtime = runtime_fixture()
    app = create_app(runtime)
    auth = {"geoToken": "test-token-result-owner"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        session_response = await client.post(
            "/agent-api/v1/sessions",
            headers={**auth, "Idempotency-Key": "idem-result-session"},
            json={"title": "Result 测试"},
        )
        session_id = session_response.json()["data"]["session_id"]
        run_response = await client.post(
            f"/agent-api/v1/sessions/{session_id}/runs",
            headers={**auth, "Idempotency-Key": "idem-result-run"},
            json=run_request(
                message_id="web-msg-result-01",
                client_instance_id="cli-result-01",
            ).model_dump(mode="json"),
        )
        run_id = run_response.json()["data"]["run_id"]
        for _ in range(100):
            run_get = await client.get(f"/agent-api/v1/runs/{run_id}", headers=auth)
            if run_get.json()["data"]["status"] == "completed":
                break
            await asyncio.sleep(0.001)
        events = await runtime.events.list_events(run_id=run_id)
        result_id = next(
            event.data["result_id"]
            for event in events
            if event.type == "result.available"
        )

        response = await client.get(
            f"/agent-api/v1/results/{result_id}",
            headers=auth,
        )
        other_user_response = await client.get(
            f"/agent-api/v1/results/{result_id}",
            headers={"geoToken": "test-token-result-other-user"},
        )

    assert response.status_code == 200
    assert response.json()["data"]["kind"] == "table"
    assert response.json()["data"]["data"]["rows"][0]["person_count"] == 128
    assert other_user_response.status_code == 404
    assert "test-token-result-owner" not in response.text


@pytest.mark.asyncio
async def test_get_result_supports_object_profile_result_union() -> None:
    runtime = runtime_fixture()
    app = create_app(runtime)
    auth = {"geoToken": "test-token-profile-result-owner"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        session_response = await client.post(
            "/agent-api/v1/sessions",
            headers={**auth, "Idempotency-Key": "idem-profile-result-session"},
            json={"title": "画像 Result 测试"},
        )
        session_id = session_response.json()["data"]["session_id"]
        owner_user_id = runtime.store.sessions[session_id].owner_user_id
        run = await runtime.service.create_run(
            user_id=owner_user_id,
            session_id=session_id,
            request=run_request(
                message_id="web-msg-profile-result",
                client_instance_id="cli-profile-result",
            ),
        )
        await runtime.service.start_run(user_id=owner_user_id, run_id=run.run_id)
        profile_result = ObjectProfileResult.model_validate(
            {
                "result_id": "res-profile-api-01",
                "data_schema_ref": "schema://data/object-profile/1.0.0",
                "result_fingerprint": "sha256:profile-api",
                "data": {
                    "object_ref": {
                        "object_type": "person",
                        "object_id": "person-01",
                    },
                    "area_code": "330106001",
                    "title": "张某",
                    "fields": [],
                },
            }
        )
        await runtime.store.save_result(
            user_id=owner_user_id,
            run_id=run.run_id,
            result=profile_result,
        )

        response = await client.get(
            "/agent-api/v1/results/res-profile-api-01",
            headers=auth,
        )

    assert response.status_code == 200
    assert response.json()["data"]["kind"] == "object_profile"


@pytest.mark.asyncio
async def test_result_items_returns_cursor_page_for_table_payload() -> None:
    from full_view_agent.domain.models import (
        PopulationMetricRow,
        PopulationMetricTable,
        TableDataResult,
    )

    runtime = runtime_fixture()
    app = create_app(runtime)
    auth = {"geoToken": "test-token-result-items-owner"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        session_response = await client.post(
            "/agent-api/v1/sessions",
            headers={**auth, "Idempotency-Key": "idem-result-items-session"},
            json={"title": "Result Items"},
        )
        session_id = session_response.json()["data"]["session_id"]
        owner_user_id = runtime.store.sessions[session_id].owner_user_id
        run = await runtime.service.create_run(
            user_id=owner_user_id,
            session_id=session_id,
            request=run_request(
                message_id="web-msg-result-items",
                client_instance_id="cli-result-items",
            ),
        )
        await runtime.service.start_run(user_id=owner_user_id, run_id=run.run_id)
        result = TableDataResult(
            result_id="res-items-api-01",
            data_schema_ref="schema://data/population-metric-table/1.0.0",
            result_fingerprint="sha256:result-items-api",
            data=PopulationMetricTable(
                rows=[
                    PopulationMetricRow(
                        area_code=f"33010600{index}",
                        area_name=f"街道{index}",
                        person_count=index,
                    )
                    for index in range(1, 4)
                ]
            ),
            row_count=3,
        )
        await runtime.store.save_result(
            user_id=owner_user_id,
            run_id=run.run_id,
            result=result,
        )

        response = await client.get(
            "/agent-api/v1/results/res-items-api-01/items?limit=2",
            headers=auth,
        )
        cursor = response.json()["meta"]["next_cursor"]
        second_page = await client.get(
            "/agent-api/v1/results/res-items-api-01/items",
            headers=auth,
            params={"limit": 2, "cursor": cursor},
        )
        tampered = await client.get(
            "/agent-api/v1/results/res-items-api-01/items",
            headers=auth,
            params={"limit": 2, "cursor": f"{cursor}changed"},
        )

    assert response.status_code == 200
    assert [item["area_name"] for item in response.json()["data"]] == ["街道1", "街道2"]
    assert response.json()["meta"]["has_next"] is True
    assert response.json()["meta"]["next_cursor"]
    assert [item["area_name"] for item in second_page.json()["data"]] == ["街道3"]
    assert second_page.json()["meta"]["has_next"] is False
    assert tampered.status_code == 422
    assert tampered.json()["error"]["code"] == "validation_error"


def test_result_items_response_accepts_housing_area_group_rows() -> None:
    from full_view_agent.api.app import CursorPageMeta, ResultItemsResponse
    from full_view_agent.domain.models import HousingAreaGroupRow

    response = ResultItemsResponse(
        data=[
            HousingAreaGroupRow(
                area_code="330106",
                area_name="西湖区",
                dwelling_count=100,
            )
        ],
        meta=CursorPageMeta(
            request_id="req-housing-area-items",
            has_next=False,
        ),
    )

    assert response.data[0].area_name == "西湖区"


@pytest.mark.asyncio
async def test_expired_result_keeps_metadata_but_rejects_payload_items() -> None:
    from datetime import UTC, datetime, timedelta

    from full_view_agent.domain.models import PopulationMetricTable, TableDataResult

    runtime = runtime_fixture()
    app = create_app(runtime)
    auth = {"geoToken": "test-token-expired-result-owner"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        session_response = await client.post(
            "/agent-api/v1/sessions",
            headers={**auth, "Idempotency-Key": "idem-expired-result-session"},
            json={"title": "过期结果"},
        )
        session_id = session_response.json()["data"]["session_id"]
        owner_user_id = runtime.store.sessions[session_id].owner_user_id
        run = await runtime.service.create_run(
            user_id=owner_user_id,
            session_id=session_id,
            request=run_request(
                message_id="web-msg-expired-result",
                client_instance_id="cli-expired-result",
            ),
        )
        await runtime.service.start_run(user_id=owner_user_id, run_id=run.run_id)
        result = TableDataResult(
            result_id="res-expired-api-01",
            data_schema_ref="schema://data/population-metric-table/1.0.0",
            result_fingerprint="sha256:expired-result-api",
            data=PopulationMetricTable(rows=[]),
            row_count=0,
        ).model_copy(
            update={
                "payload_expires_at": datetime.now(UTC) - timedelta(seconds=1),
                "evidence_ids": ["evd-expired-api-01"],
            }
        )
        await runtime.store.save_result(
            user_id=owner_user_id,
            run_id=run.run_id,
            result=result,
        )

        metadata = await client.get(
            "/agent-api/v1/results/res-expired-api-01",
            headers=auth,
        )
        items = await client.get(
            "/agent-api/v1/results/res-expired-api-01/items",
            headers=auth,
        )

    assert metadata.status_code == 200
    assert metadata.json()["data"]["payload_status"] == "expired"
    assert "data" not in metadata.json()["data"]
    assert metadata.json()["data"]["evidence_ids"] == ["evd-expired-api-01"]
    assert items.status_code == 410
    assert items.json()["error"]["code"] == "result_payload_expired"


@pytest.mark.asyncio
async def test_expired_analysis_report_metadata_is_explicit_not_object_profile() -> None:
    from datetime import UTC, datetime, timedelta

    from full_view_agent.application.analysis_report import (
        ANALYSIS_REPORT_DATA_SCHEMA_REF,
    )
    from full_view_agent.domain.analysis_report import AnalysisReportDataResult

    runtime = runtime_fixture()
    app = create_app(runtime)
    auth = {"geoToken": "test-token-expired-analysis-report"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        session_response = await client.post(
            "/agent-api/v1/sessions",
            headers={**auth, "Idempotency-Key": "idem-expired-analysis-session"},
            json={"title": "过期研判报告"},
        )
        session_id = session_response.json()["data"]["session_id"]
        owner_user_id = runtime.store.sessions[session_id].owner_user_id
        run = await runtime.service.create_run(
            user_id=owner_user_id,
            session_id=session_id,
            request=run_request(
                message_id="web-msg-expired-analysis",
                client_instance_id="cli-expired-analysis",
            ),
        )
        await runtime.service.start_run(user_id=owner_user_id, run_id=run.run_id)
        result = AnalysisReportDataResult(
            result_id="res-expired-analysis-01",
            data_schema_ref=ANALYSIS_REPORT_DATA_SCHEMA_REF,
            result_fingerprint="sha256:" + "ab" * 32,
            plan_id="plan-expired-analysis-01",
            request_id="req-expired-analysis-01",
            status="failed",
            reason_code="ANALYSIS_FAILED",
            sections=(),
        ).model_copy(
            update={
                "payload_expires_at": datetime.now(UTC) - timedelta(seconds=1),
                "evidence_ids": ["evd-expired-analysis-01"],
            }
        )
        await runtime.store.save_result(
            user_id=owner_user_id,
            run_id=run.run_id,
            result=result,
        )

        metadata = await client.get(
            "/agent-api/v1/results/res-expired-analysis-01",
            headers=auth,
        )
        items = await client.get(
            "/agent-api/v1/results/res-expired-analysis-01/items",
            headers=auth,
        )

    assert metadata.status_code == 200
    data = metadata.json()["data"]
    assert data["kind"] == "analysis_report"
    assert data["payload_status"] == "expired"
    assert "data" not in data
    assert data["title"] == "区域研判报告"
    assert data["summary"] == {
        "status": "failed",
        "section_count": 0,
        "text": "区域研判报告 Payload 已过期。",
    }
    assert data["evidence_ids"] == ["evd-expired-analysis-01"]
    assert items.status_code == 410
    assert items.json()["error"]["code"] == "result_payload_expired"


@pytest.mark.asyncio
async def test_result_exposes_owner_scoped_evidence_without_internal_query_details() -> None:
    runtime = runtime_fixture()
    app = create_app(runtime)
    auth = {"geoToken": "test-token-evidence-owner"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        session_response = await client.post(
            "/agent-api/v1/sessions",
            headers={**auth, "Idempotency-Key": "idem-evidence-session"},
            json={"title": "Evidence 测试"},
        )
        session_id = session_response.json()["data"]["session_id"]
        run_response = await client.post(
            f"/agent-api/v1/sessions/{session_id}/runs",
            headers={**auth, "Idempotency-Key": "idem-evidence-run"},
            json=run_request(
                message_id="web-msg-evidence",
                client_instance_id="cli-evidence",
            ).model_dump(mode="json"),
        )
        run_id = run_response.json()["data"]["run_id"]
        for _ in range(100):
            run_get = await client.get(f"/agent-api/v1/runs/{run_id}", headers=auth)
            if run_get.json()["data"]["status"] == "completed":
                break
            await asyncio.sleep(0.001)
        events = await runtime.events.list_events(run_id=run_id)
        result_id = next(
            event.data["result_id"]
            for event in events
            if event.type == "result.available"
        )
        result_response = await client.get(
            f"/agent-api/v1/results/{result_id}",
            headers=auth,
        )
        evidence_ids = result_response.json()["data"]["evidence_ids"]
        evidence_response = await client.get(
            f"/agent-api/v1/evidence/{evidence_ids[0]}",
            headers=auth,
        ) if evidence_ids else None
        other_user_response = await client.get(
            f"/agent-api/v1/evidence/{evidence_ids[0]}",
            headers={"geoToken": "test-token-evidence-other-user"},
        ) if evidence_ids else None

    assert len(evidence_ids) == 1
    assert evidence_response is not None
    assert evidence_response.status_code == 200
    assert other_user_response is not None
    assert other_user_response.status_code == 404
    evidence = evidence_response.json()["data"]
    assert evidence["result_id"] == result_id
    assert evidence["dataset_id"] == "population"
    assert evidence["source_system"] == "in_memory_fixture"
    assert evidence["effective_area_codes"] == ["330106"]
    assert evidence["dataset_snapshot_version"] is None
    assert evidence["semantic_registry_version"] is None
    assert evidence["metric_definitions"] == []
    assert evidence["as_of"] is None
    assert evidence["freshness"] == {
        "status": "unknown",
        "expected_update_cycle": None,
    }
    assert evidence["policy_fingerprint"].startswith("sha256:")
    serialized = evidence_response.text.casefold()
    assert "sql" not in serialized
    assert "internal_url" not in serialized
    assert "geotoken" not in serialized


@pytest.mark.asyncio
async def test_reauthentication_input_refreshes_credential_and_resumes_run() -> None:
    from full_view_agent.application.errors import ReauthenticationRequired
    from full_view_agent.application.mock_executor import MockRunExecutor

    class ReauthOnceCapability:
        def __init__(self) -> None:
            self.calls = 0

        async def execute(self, **kwargs):
            self.calls += 1
            if self.calls == 1:
                raise ReauthenticationRequired("credential expired")
            from tests.test_mock_executor import RecordingCapability

            return await RecordingCapability().execute(**kwargs)

    runtime = runtime_fixture()
    capability = ReauthOnceCapability()
    runtime.executor = MockRunExecutor(
        service=runtime.service,
        store=runtime.store,
        events=runtime.events,
        capability=capability,
        auth_context_provider=runtime.auth_contexts,
    )
    app = create_app(runtime)
    auth = {"geoToken": "test-token-reauth-user"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        session_response = await client.post(
            "/agent-api/v1/sessions",
            headers={**auth, "Idempotency-Key": "idem-reauth-session"},
            json={"title": "重新认证测试"},
        )
        session_id = session_response.json()["data"]["session_id"]
        run_response = await client.post(
            f"/agent-api/v1/sessions/{session_id}/runs",
            headers={**auth, "Idempotency-Key": "idem-reauth-run"},
            json=run_request(
                message_id="web-msg-reauth",
                client_instance_id="cli-reauth",
            ).model_dump(mode="json"),
        )
        run_id = run_response.json()["data"]["run_id"]
        for _ in range(100):
            waiting_response = await client.get(
                f"/agent-api/v1/runs/{run_id}", headers=auth
            )
            if waiting_response.json()["data"]["status"] == "waiting_input":
                break
            await asyncio.sleep(0.001)
        waiting = waiting_response.json()["data"]
        events = await runtime.events.list_events(run_id=run_id)
        request_event = next(event for event in events if event.type == "reauth_required")

        input_response = await client.post(
            f"/agent-api/v1/runs/{run_id}/inputs",
            headers={
                "geoToken": "test-token-reauth-user",
                "Idempotency-Key": "idem-reauth-input",
            },
            json={
                "input_request_id": request_event.data["input_request_id"],
                "client_instance_id": "cli-reauth",
                "run_state_version": waiting["state_version"],
                "response": {"type": "reauthenticated"},
            },
        )
        for _ in range(100):
            completed_response = await client.get(
                f"/agent-api/v1/runs/{run_id}", headers=auth
            )
            if completed_response.json()["data"]["status"] == "completed":
                break
            await asyncio.sleep(0.001)

    assert input_response.status_code == 202
    assert completed_response.json()["data"]["outcome"] == "success"
    assert capability.calls == 2
    assert "test-token-reauth-user" not in input_response.text


# ---------------------------------------------------------------------------
# Resume idempotency via real HTTP API with counting wrapper
# ---------------------------------------------------------------------------


class _CountingOrchPort:
    """OrchestrationPort wrapper that counts schedule/resume calls."""

    def __init__(self, delegate: object) -> None:
        self._d = delegate
        self.schedule_count = 0
        self.resume_count = 0

    async def execute(self, *, user_id: str, run_id: str) -> None:
        await self._d.execute(user_id=user_id, run_id=run_id)  # type: ignore[attr-defined]

    async def cancel(self, *, user_id: str, run_id: str) -> None:
        await self._d.cancel(user_id=user_id, run_id=run_id)  # type: ignore[attr-defined]

    async def resume(  # noqa: D102
        self,
        *,
        user_id: str,
        run_id: str,
        input_request_id: str,
        run_state_version: int,
    ) -> None:
        self.resume_count += 1
        await self._d.resume(  # type: ignore[attr-defined]
            user_id=user_id,
            run_id=run_id,
            input_request_id=input_request_id,
            run_state_version=run_state_version,
        )

    async def steer(  # noqa: D102
        self,
        *,
        user_id: str,
        run_id: str,
        client_instance_id: str,
        content: str,
    ) -> object:
        return await self._d.steer(  # type: ignore[attr-defined]
            user_id=user_id,
            run_id=run_id,
            client_instance_id=client_instance_id,
            content=content,
        )

    def schedule(self, *, user_id: str, run_id: str) -> None:
        self.schedule_count += 1
        self._d.schedule(user_id=user_id, run_id=run_id)  # type: ignore[attr-defined]

    async def shutdown(self) -> None:
        await self._d.shutdown()  # type: ignore[attr-defined]

    def __getattr__(self, name: str) -> object:
        return getattr(self._d, name)


@pytest.mark.asyncio
async def test_resume_idempotency_via_http_api() -> None:
    """Real API test: non-replay input → +1 resume +1 schedule;
    replay → no additional calls; input.received before run.resumed."""
    from full_view_agent.application.errors import ReauthenticationRequired
    from full_view_agent.application.native_orchestrator import (
        NativeOrchestrator,
    )

    class ReauthOnceCapability:
        def __init__(self) -> None:
            self.calls = 0

        async def execute(self, **kwargs: object) -> object:
            self.calls += 1
            if self.calls == 1:
                raise ReauthenticationRequired("credential expired")
            from tests.test_mock_executor import RecordingCapability

            return await RecordingCapability().execute(**kwargs)

    runtime = runtime_fixture()
    capability = ReauthOnceCapability()
    native = NativeOrchestrator(
        service=runtime.service,
        store=runtime.store,
        events=runtime.events,
        capability=capability,
        auth_context_provider=runtime.auth_contexts,
    )
    counting = _CountingOrchPort(native)
    runtime.executor = counting  # type: ignore[assignment]
    app = create_app(runtime)
    auth = {"geoToken": "test-token-reauth-user"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
    ) as client:
        session_resp = await client.post(
            "/agent-api/v1/sessions",
            headers={**auth, "Idempotency-Key": "idem-resume-session"},
            json={"title": "resume idempotency"},
        )
        session_id = session_resp.json()["data"]["session_id"]
        run_resp = await client.post(
            f"/agent-api/v1/sessions/{session_id}/runs",
            headers={**auth, "Idempotency-Key": "idem-resume-run"},
            json=run_request(
                message_id="web-msg-resume-idem",
                client_instance_id="cli-resume-idem",
            ).model_dump(mode="json"),
        )
        run_id = run_resp.json()["data"]["run_id"]

        # Wait for waiting_input
        for _ in range(100):
            r = await client.get(
                f"/agent-api/v1/runs/{run_id}", headers=auth,
            )
            if r.json()["data"]["status"] == "waiting_input":
                break
            await asyncio.sleep(0.001)
        waiting = r.json()["data"]
        events = await runtime.events.list_events(run_id=run_id)
        req_event = next(
            e for e in events if e.type == "reauth_required"
        )

        # Baseline after initial schedule (from run creation)
        base_schedule = counting.schedule_count
        base_resume = counting.resume_count

        # --- First (non-replay) input ---
        idem_key = "idem-resume-input"
        input_body = {
            "input_request_id": req_event.data["input_request_id"],
            "client_instance_id": "cli-resume-idem",
            "run_state_version": waiting["state_version"],
            "response": {"type": "reauthenticated"},
        }
        input_resp = await client.post(
            f"/agent-api/v1/runs/{run_id}/inputs",
            headers={**auth, "Idempotency-Key": idem_key},
            json=input_body,
        )
        assert input_resp.status_code == 202
        assert input_resp.json()["meta"]["idempotency_replayed"] is not True

        # Wait for completion
        for _ in range(100):
            cr = await client.get(
                f"/agent-api/v1/runs/{run_id}", headers=auth,
            )
            if cr.json()["data"]["status"] == "completed":
                break
            await asyncio.sleep(0.001)

        # Non-replay: exactly +1 resume, +1 schedule
        assert counting.resume_count == base_resume + 1
        assert counting.schedule_count == base_schedule + 1

        # --- Replay with same Idempotency-Key ---
        replay_resp = await client.post(
            f"/agent-api/v1/runs/{run_id}/inputs",
            headers={**auth, "Idempotency-Key": idem_key},
            json=input_body,
        )
        assert replay_resp.status_code == 202
        assert replay_resp.json()["meta"]["idempotency_replayed"] is True
        # Replay must NOT trigger additional resume or schedule
        assert counting.resume_count == base_resume + 1
        assert counting.schedule_count == base_schedule + 1

    # Event ordering: input.received must precede run.resumed
    all_events = await runtime.events.list_events(run_id=run_id)
    event_types = [e.type for e in all_events]
    input_recv_idx = event_types.index("input.received")
    resumed_idx = event_types.index("run.resumed")
    assert input_recv_idx < resumed_idx
    # Capability called exactly twice (first reauth, second success)
    assert capability.calls == 2


@pytest.mark.anyio
async def test_liveness_returns_ok():
    app = create_app(runtime=runtime_fixture())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.anyio
async def test_readiness_reports_store_status():
    app = create_app(runtime=runtime_fixture())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["checks"]["store"] == "ok"
