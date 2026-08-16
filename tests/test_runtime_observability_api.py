from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

import full_view_agent.api.runtime_observability_routes as observability_routes
from full_view_agent.api.app import RuntimeContainer, create_app
from full_view_agent.application.model_config_repository import ModelConfigSnapshot
from full_view_agent.application.run_capability_snapshot_store import (
    PersistedRunCapabilitySnapshot,
)
from full_view_agent.domain.agent_definition import (
    AgentModelVersionRef,
    RunAgentReleaseSnapshot,
)
from full_view_agent.domain.models import (
    AgentApplication,
    AgentRun,
    AgentSession,
    AuthContext,
    AuthDataScopes,
    LegacyIdentitySnapshot,
    Principal,
)
from full_view_agent.infrastructure.credential_broker import InMemoryCredentialBroker
from full_view_agent.infrastructure.event_broker import InMemoryEventBroker
from full_view_agent.infrastructure.postgres_persistence import PostgresAgentPersistence
from full_view_agent.infrastructure.runtime_observability_repository import (
    InMemoryRuntimeObservabilityRepository,
)

NOW = datetime(2026, 8, 15, 0, 0, tzinfo=UTC)
BASE = "/capability-api/v1/applications/full_information_view/runtime"
WINDOW = {"from": "2026-08-15T00:00:00Z", "to": "2026-08-15T01:00:00Z"}


def _identity(*, tenant_id: str = "tenant-a", roles: list[str] | None = None):
    return LegacyIdentitySnapshot(
        principal=Principal(
            tenant_id=tenant_id,
            user_id="runtime-admin",
            org_id="platform-admins",
            roles=roles if roles is not None else ["admin"],
        ),
        source="runtime-observability-test",
        source_session_expires_at=NOW + timedelta(hours=1),
        base_area_codes=[],
    )


def _runtime(identity: LegacyIdentitySnapshot | None = None) -> RuntimeContainer:
    runtime = RuntimeContainer(
        credentials=InMemoryCredentialBroker(),
        events=InMemoryEventBroker(retention_seconds=3600, now=lambda: NOW),
    )
    runtime.capability_identity_port = AsyncMock()
    runtime.capability_identity_port.resolve = AsyncMock(return_value=identity or _identity())
    return runtime


def _seed_run(
    runtime: RuntimeContainer,
    *,
    run_id: str,
    session_id: str,
    tenant_id: str = "tenant-a",
    app_id: str = "full_information_view",
    status: str = "completed",
    outcome: str | None = "success",
    started_offset: int = 0,
    duration_seconds: int = 2,
) -> None:
    session = AgentSession(
        session_id=session_id,
        owner_tenant_id=tenant_id,
        owner_user_id=f"owner-{session_id}",
        app_id=app_id,
        title=f"Session {session_id}",
        created_at=NOW + timedelta(seconds=started_offset),
        updated_at=NOW + timedelta(seconds=started_offset + duration_seconds),
    )
    started = NOW + timedelta(seconds=started_offset)
    run = AgentRun(
        run_id=run_id,
        session_id=session_id,
        origin_client_instance_id="browser-1",
        status=status,  # type: ignore[arg-type]
        outcome=outcome,  # type: ignore[arg-type]
        input_message_id=f"msg-{run_id}",
        base_context_version=1,
        created_at=started,
        started_at=started,
        completed_at=(
            started + timedelta(seconds=duration_seconds)
            if status in {"completed", "failed", "cancelled", "expired"}
            else None
        ),
    )
    runtime.store.sessions[session_id] = session  # type: ignore[attr-defined]
    runtime.store.runs[run_id] = run  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_runtime_overview_is_tenant_and_application_scoped() -> None:
    runtime = _runtime()
    _seed_run(runtime, run_id="run-ok", session_id="ses-ok")
    _seed_run(
        runtime,
        run_id="run-failed",
        session_id="ses-failed",
        status="failed",
        outcome="failed",
        started_offset=10,
        duration_seconds=10,
    )
    _seed_run(
        runtime,
        run_id="run-other-tenant",
        session_id="ses-other-tenant",
        tenant_id="tenant-b",
    )
    _seed_run(
        runtime,
        run_id="run-other-app",
        session_id="ses-other-app",
        app_id="other_application",
    )

    app = create_app(runtime)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(
            BASE + "/overview", params=WINDOW, headers={"geoToken": "admin"}
        )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["application_id"] == "full_information_view"
    assert data["readiness"]["status"] == "ok"
    assert data["session_count"] == 2
    assert data["run_count"] == 2
    assert data["terminal_status_distribution"] == {"completed": 1, "failed": 1}
    assert data["success_rate"] == 0.5
    assert data["latency_ms"] == {"p50": 2000, "p95": 10000}
    assert data["runtime"]["capability_generation"] >= 0
    assert data["runtime"]["global_loaded_tools"] >= 0
    assert data["runtime"]["global_loaded_skills"] >= 0
    assert data["runtime"]["global_loaded_workflows"] >= 0
    assert "loaded_tools" not in data["runtime"]
    serialized = response.text.casefold()
    assert "token" not in serialized
    assert "credential" not in serialized


@pytest.mark.asyncio
async def test_runtime_run_list_is_filtered_and_cursor_paginated() -> None:
    runtime = _runtime()
    _seed_run(runtime, run_id="run-1", session_id="ses-1", status="failed", outcome="failed")
    _seed_run(
        runtime,
        run_id="run-2",
        session_id="ses-2",
        status="failed",
        outcome="failed",
        started_offset=5,
    )
    _seed_run(runtime, run_id="run-3", session_id="ses-3", started_offset=10)
    app = create_app(runtime)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        first = await client.get(
            BASE + "/runs",
            params={"status": "failed", "limit": 1, **WINDOW},
            headers={"geoToken": "admin"},
        )
        second = await client.get(
            BASE + "/runs",
            params={
                "status": "failed",
                "limit": 1,
                "cursor": first.json()["meta"]["next_cursor"],
                **WINDOW,
            },
            headers={"geoToken": "admin"},
        )

    assert first.status_code == second.status_code == 200
    assert [first.json()["data"][0]["run_id"], second.json()["data"][0]["run_id"]] == [
        "run-2",
        "run-1",
    ]
    assert first.json()["meta"]["has_next"] is True
    assert second.json()["meta"]["has_next"] is False


@pytest.mark.asyncio
async def test_runtime_run_pagination_is_pushed_down_to_repository(monkeypatch) -> None:
    runtime = _runtime()
    _seed_run(runtime, run_id="run-page-1", session_id="ses-page-1")
    _seed_run(runtime, run_id="run-page-2", session_id="ses-page-2", started_offset=5)
    repository = InMemoryRuntimeObservabilityRepository(runtime.store, runtime.events)  # type: ignore[arg-type]
    repository.list_runs = AsyncMock(wraps=repository.list_runs)  # type: ignore[method-assign]
    monkeypatch.setattr(
        observability_routes,
        "runtime_observability_repository",
        lambda _store, _events, **_kwargs: repository,
    )
    app = create_app(runtime)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(
            BASE + "/runs", params={"limit": 1, **WINDOW}, headers={"geoToken": "admin"}
        )

    assert response.status_code == 200
    assert repository.list_runs.await_count == 1  # type: ignore[attr-defined]
    assert repository.list_runs.await_args.kwargs["limit"] == 2  # type: ignore[attr-defined]
    assert repository.list_runs.await_args.kwargs["before"] is None  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_runtime_run_cursor_is_stable_when_a_newer_run_is_inserted() -> None:
    runtime = _runtime()
    _seed_run(runtime, run_id="run-anchor-1", session_id="ses-anchor-1")
    _seed_run(runtime, run_id="run-anchor-2", session_id="ses-anchor-2", started_offset=5)
    app = create_app(runtime)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        first = await client.get(
            BASE + "/runs", params={"limit": 1, **WINDOW}, headers={"geoToken": "admin"}
        )
        _seed_run(
            runtime,
            run_id="run-inserted-after-page-one",
            session_id="ses-inserted-after-page-one",
            started_offset=10,
        )
        second = await client.get(
            BASE + "/runs",
            params={"limit": 1, "cursor": first.json()["meta"]["next_cursor"], **WINDOW},
            headers={"geoToken": "admin"},
        )

    assert first.json()["data"][0]["run_id"] == "run-anchor-2"
    assert second.status_code == 200
    assert second.json()["data"][0]["run_id"] == "run-anchor-1"


@pytest.mark.asyncio
async def test_runtime_lists_reject_unbounded_or_invalid_time_windows() -> None:
    runtime = _runtime()
    _seed_run(runtime, run_id="run-window", session_id="ses-window")
    app = create_app(runtime)
    too_wide = {
        "from": "2026-06-01T00:00:00Z",
        "to": "2026-08-15T00:00:00Z",
    }

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        runs = await client.get(BASE + "/runs", params=too_wide, headers={"geoToken": "admin"})
        sessions = await client.get(
            BASE + "/sessions", params=too_wide, headers={"geoToken": "admin"}
        )

    assert runs.status_code == sessions.status_code == 409
    assert runs.json()["error"]["code"] == "run_state_conflict"


@pytest.mark.asyncio
async def test_runtime_session_list_is_paginated_with_scoped_run_counts() -> None:
    runtime = _runtime()
    _seed_run(runtime, run_id="run-session-1", session_id="ses-session-1")
    _seed_run(
        runtime,
        run_id="run-session-2",
        session_id="ses-session-2",
        started_offset=5,
    )
    runtime.store.runs["run-session-extra"] = runtime.store.runs[  # type: ignore[attr-defined]
        "run-session-2"
    ].model_copy(update={"run_id": "run-session-extra"})
    app = create_app(runtime)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(
            BASE + "/sessions", params={"limit": 1, **WINDOW}, headers={"geoToken": "admin"}
        )

    assert response.status_code == 200
    assert response.json()["data"] == [
        {
            "session_id": "ses-session-2",
            "title": "Session ses-session-2",
            "status": "active",
            "active_run_id": None,
            "created_at": "2026-08-15T00:00:05Z",
            "updated_at": "2026-08-15T00:00:07Z",
            "version": 1,
            "run_count": 2,
        }
    ]
    assert response.json()["meta"]["has_next"] is True


@pytest.mark.asyncio
async def test_runtime_timeline_normalizes_references_and_strips_sensitive_payloads() -> None:
    runtime = _runtime()
    _seed_run(runtime, run_id="run-timeline", session_id="ses-timeline")
    owner = runtime.store.sessions["ses-timeline"].owner_user_id  # type: ignore[attr-defined]
    await runtime.auth_contexts.put(
        AuthContext(
            auth_context_id="auth-1",
            auth_context_fingerprint="sha256:auth-safe",
            principal=Principal(tenant_id="tenant-a", user_id=owner, org_id="org", roles=[]),
            application=AgentApplication(
                app_id="full_information_view", agent_id="governance_general_agent"
            ),
            entitlements=[],
            data_scopes=AuthDataScopes(areas=[], datasets=[], field_policy_set="aggregate-only"),
            purpose="interactive_analysis",
            session_id="ses-timeline",
            run_id="run-timeline",
            credential_ref="credential-must-never-appear",
            issued_at=NOW,
            expires_at=NOW + timedelta(hours=1),
            policy_version="policy-1",
        )
    )
    await runtime.agent_repository.bind_run(
        RunAgentReleaseSnapshot(
            release_id="release-1",
            app_id="full_information_view",
            agent_id="governance_general_agent",
            agent_version="0.0.2",
            capability_refs=("governance.query_population_metrics@1.0.0",),
            model_refs=(
                AgentModelVersionRef(
                    model_config_id="model-primary",
                    config_version=3,
                    role="primary",
                    order=0,
                ),
            ),
            published_by="system",
            reason="baseline",
            run_id="run-timeline",
            tenant_id="tenant-a",
            bound_at=NOW,
        )
    )
    await runtime.run_capability_snapshot_store.store_if_absent(
        PersistedRunCapabilitySnapshot(
            run_id="run-timeline",
            tool_versions={"governance.query_population_metrics": "1.0.0"},
            skill_versions={"skill.population": "1.0.0"},
            workflow_versions={"workflow.population": "2.0.0"},
            captured_at=NOW,
            application_scoped=True,
            agent_scoped=True,
        )
    )
    await runtime.run_model_binding_repository.store_binding(
        "run-timeline",
        ModelConfigSnapshot(
            config_id="model-primary",
            config_version=3,
            name="Primary",
            api_base_url="https://secret-model-host.invalid",
            model_name="safe-model-name",
            protocol="openai_compatible",
            timeout_seconds=30,
            max_output_tokens=1000,
            max_retries=1,
            api_key_ciphertext=b"ciphertext-must-never-appear",
            api_key_nonce=b"nonce-must-never-appear",
        ),
    )
    await runtime.events.publish(
        event_type="model.requested",
        session_id="ses-timeline",
        run_id="run-timeline",
        data={
            "model_turn": 1,
            "prompt_version": "prompt@1.2.0",
            "message_count": 4,
            "raw_prompt": "TOP SECRET PROMPT",
            "api_key": "secret-key",
        },
    )
    await runtime.events.publish(
        event_type="tool.started",
        session_id="ses-timeline",
        run_id="run-timeline",
        data={
            "tool_call_id": "call-1",
            "tool_id": "governance.query_population_metrics",
            "arguments": {"person_name": "sensitive"},
        },
    )
    await runtime.events.publish(
        event_type="tool.failed",
        session_id="ses-timeline",
        run_id="run-timeline",
        data={
            "tool_result": {
                "tool_call_id": "call-1",
                "tool_id": "governance.query_population_metrics",
                "tool_version": "1.0.0",
                "status": "failed",
                "summary": "contains sensitive upstream body",
                "warnings": ["upstream_timeout"],
                "evidence_ids": [],
            },
            "request_body": {"secret": "do-not-return"},
        },
    )
    await runtime.events.publish(
        event_type="result.available",
        session_id="ses-timeline",
        run_id="run-timeline",
        data={"result_id": "res-1"},
    )
    await runtime.events.publish(
        event_type="evidence.available",
        session_id="ses-timeline",
        run_id="run-timeline",
        data={"result_id": "res-1", "evidence_id": "ev-1"},
    )
    app = create_app(runtime)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(
            BASE + "/runs/run-timeline/timeline", headers={"geoToken": "admin"}
        )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["run_id"] == "run-timeline"
    assert data["events_are_retention_bound"] is True
    assert data["event_history_status"] == "within_retention"
    assert data["event_retention_cutoff"] is not None
    assert {item["category"] for item in data["items"]} >= {
        "auth",
        "release",
        "capability",
        "run",
        "model",
        "tool",
        "result",
        "evidence",
        "skill",
        "workflow",
    }
    model_bound = next(item for item in data["items"] if item["event_type"] == "model.bound")
    assert model_bound["stage"] == "setup"
    assert model_bound["detail_level"] == "technical"
    assert model_bound["model_ref"] == {
        "config_id": "model-primary",
        "config_version": 3,
        "model_name": "safe-model-name",
    }
    failed = next(item for item in data["items"] if item["event_type"] == "tool.failed")
    requested = next(
        item for item in data["items"] if item["event_type"] == "model.requested"
    )
    assert requested["stage"] == "reasoning"
    assert requested["display_label"] == "第 1 轮模型分析"
    assert requested["detail_level"] == "summary"
    assert failed["stage"] == "execution"
    assert failed["display_label"] == "执行业务能力"
    assert failed["display_summary"] == "业务能力执行失败。"
    assert failed["stable_error_code"] == "upstream_timeout"
    assert failed["capability_ref"] == {
        "capability_id": "governance.query_population_metrics",
        "version": "1.0.0",
        "type": "tool",
    }
    assert data["result_ids"] == ["res-1"]
    assert data["evidence_ids"] == ["ev-1"]
    serialized = response.text
    for secret in (
        "TOP SECRET PROMPT",
        "secret-key",
        "sensitive",
        "upstream body",
        "credential-must-never-appear",
        "secret-model-host",
        "ciphertext-must-never-appear",
    ):
        assert secret not in serialized


@pytest.mark.asyncio
async def test_runtime_timeline_marks_expired_event_history_instead_of_looking_complete() -> None:
    runtime = _runtime()
    broker = InMemoryEventBroker(retention_seconds=3600, now=lambda: NOW)
    runtime.events = broker
    _seed_run(runtime, run_id="run-expired-events", session_id="ses-expired-events")
    await broker.publish(
        event_type="result.available",
        session_id="ses-expired-events",
        run_id="run-expired-events",
        data={"result_id": "result-that-will-expire"},
    )
    broker._now = lambda: NOW + timedelta(hours=2)
    app = create_app(runtime)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(
            BASE + "/runs/run-expired-events/timeline", headers={"geoToken": "admin"}
        )

    assert response.status_code == 200
    assert response.json()["data"]["event_history_status"] == "expired_or_partial"
    assert response.json()["data"]["result_ids"] == []


@pytest.mark.asyncio
async def test_runtime_model_monitor_aggregates_turns_without_prompt_content() -> None:
    runtime = _runtime()
    _seed_run(runtime, run_id="run-model", session_id="ses-model")
    await runtime.agent_repository.bind_run(
        RunAgentReleaseSnapshot(
            release_id="release-model",
            app_id="full_information_view",
            agent_id="governance_general_agent",
            agent_version="0.0.2",
            model_refs=(
                AgentModelVersionRef(
                    model_config_id="model-primary", config_version=2, role="primary", order=0
                ),
                AgentModelVersionRef(
                    model_config_id="model-fallback", config_version=3, role="fallback", order=1
                ),
            ),
            published_by="system",
            reason="baseline",
            run_id="run-model",
            tenant_id="tenant-a",
            bound_at=NOW,
        )
    )
    await runtime.run_model_binding_repository.store_binding(
        "run-model",
        ModelConfigSnapshot(
            config_id="model-fallback",
            config_version=3,
            name="Fallback",
            api_base_url="https://must-not-appear.invalid",
            model_name="safe-model-name",
            protocol="openai_compatible",
            timeout_seconds=30,
            max_output_tokens=1000,
            max_retries=1,
            api_key_ciphertext=b"secret",
            api_key_nonce=b"nonce",
        ),
    )
    for event_type, turn, error in (
        ("model.requested", 1, None),
        ("model.responded", 1, None),
        ("model.requested", 2, None),
        ("model.failed", 2, "provider_timeout"),
    ):
        data = {
            "model_turn": turn,
            "raw_prompt": "never expose model prompt",
        }
        if event_type == "model.responded":
            data["usage"] = {
                "prompt_tokens": 13,
                "completion_tokens": 5,
                "total_tokens": 18,
            }
        if error:
            data["error_code"] = error
        await runtime.events.publish(
            event_type=event_type,
            session_id="ses-model",
            run_id="run-model",
            data=data,
        )
    runtime.events.list_events = AsyncMock(wraps=runtime.events.list_events)
    runtime.agent_repository.get_run_snapshot = AsyncMock(
        wraps=runtime.agent_repository.get_run_snapshot
    )
    runtime.run_model_binding_repository.load_binding = AsyncMock(
        wraps=runtime.run_model_binding_repository.load_binding
    )
    runtime.run_model_binding_repository.load_snapshot = AsyncMock(
        wraps=runtime.run_model_binding_repository.load_snapshot
    )
    app = create_app(runtime)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(BASE + "/models", params=WINDOW, headers={"geoToken": "admin"})

    assert response.status_code == 200
    assert response.json()["data"] == [
        {
            "config_id": "model-fallback",
            "config_version": 3,
            "model_name": "safe-model-name",
            "agent_id": "governance_general_agent",
            "request_count": 2,
            "response_count": 1,
            "failure_count": 1,
            "fallback_run_count": 1,
            "prompt_tokens": 13,
            "completion_tokens": 5,
            "total_tokens": 18,
            "usage_event_count": 1,
            "success_rate": 0.5,
            "latency_ms": {"p50": 0, "p95": 0},
            "last_error_code": "provider_timeout",
        }
    ]
    assert "never expose model prompt" not in response.text
    assert runtime.events.list_events.await_count == 0
    assert runtime.agent_repository.get_run_snapshot.await_count == 0
    assert runtime.run_model_binding_repository.load_binding.await_count == 0
    assert runtime.run_model_binding_repository.load_snapshot.await_count == 0
    assert response.json()["meta"]["event_retention_seconds"] == 3600
    assert response.json()["meta"]["event_window_status"] == "retention_bounded"
    assert response.json()["meta"]["events_are_retention_bound"] is True
    assert response.json()["meta"]["truncated"] is False


@pytest.mark.asyncio
async def test_runtime_event_metrics_clamp_windows_to_event_retention() -> None:
    runtime = _runtime()
    app = create_app(runtime)
    params = {"from": "2026-08-14T22:00:00Z", "to": "2026-08-15T00:00:00Z"}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        models = await client.get(BASE + "/models", params=params, headers={"geoToken": "admin"})
        capabilities = await client.get(
            BASE + "/capabilities", params=params, headers={"geoToken": "admin"}
        )

    assert models.status_code == capabilities.status_code == 200
    assert models.json()["meta"]["requested_from"] == "2026-08-14T22:00:00Z"
    assert models.json()["meta"]["effective_from"] == "2026-08-14T23:00:00Z"
    assert models.json()["meta"]["effective_to"] == "2026-08-15T00:00:00Z"
    assert models.json()["meta"]["truncated"] is True


@pytest.mark.asyncio
async def test_runtime_capability_monitor_aggregates_version_and_stable_error_code() -> None:
    runtime = _runtime()
    _seed_run(runtime, run_id="run-tool", session_id="ses-tool")
    for event_type, call_id, status, warning in (
        ("tool.started", "call-ok", None, None),
        ("tool.completed", "call-ok", "success", None),
        ("tool.started", "call-denied", None, None),
        ("tool.completed", "call-denied", "denied", "policy_denied"),
        ("tool.started", "call-failed", None, None),
        ("tool.failed", "call-failed", "failed", "upstream_timeout"),
    ):
        data: dict[str, object] = {
            "tool_call_id": call_id,
            "tool_id": "governance.query_population_metrics",
        }
        if status:
            data["tool_result"] = {
                "tool_call_id": call_id,
                "tool_id": "governance.query_population_metrics",
                "tool_version": "1.0.0",
                "status": status,
                "warnings": [warning] if warning else [],
            }
        await runtime.events.publish(
            event_type=event_type,
            session_id="ses-tool",
            run_id="run-tool",
            data=data,
        )
    runtime.events.list_events = AsyncMock(wraps=runtime.events.list_events)
    runtime.capability_repository.get = AsyncMock(wraps=runtime.capability_repository.get)
    runtime.capability_repository.list_capabilities = AsyncMock(
        wraps=runtime.capability_repository.list_capabilities
    )
    app = create_app(runtime)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(
            BASE + "/capabilities",
            params={**WINDOW, "capability_type": "tool"},
            headers={"geoToken": "admin"},
        )

    assert response.status_code == 200
    assert response.json()["data"] == [
        {
            "capability_id": "governance.query_population_metrics",
            "capability_version": "1.0.0",
            "capability_type": "tool",
            "connector_id": None,
            "invocation_count": 3,
            "success_count": 1,
            "failure_count": 1,
            "denied_count": 1,
            "success_rate": pytest.approx(1 / 3),
            "latency_ms": {"p50": 0, "p95": 0},
            "last_error_code": "upstream_timeout",
        }
    ]
    assert runtime.events.list_events.await_count == 0
    assert runtime.capability_repository.get.await_count == 0
    assert runtime.capability_repository.list_capabilities.await_count == 1


@pytest.mark.asyncio
async def test_runtime_alerts_are_deterministically_derived_from_window_thresholds() -> None:
    runtime = _runtime()
    for index in range(5):
        _seed_run(
            runtime,
            run_id=f"run-alert-{index}",
            session_id=f"ses-alert-{index}",
            status="failed" if index == 0 else "completed",
            outcome="failed" if index == 0 else "success",
            started_offset=index,
            duration_seconds=31 if index == 4 else 1,
        )
    app = create_app(runtime)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(BASE + "/alerts", params=WINDOW, headers={"geoToken": "admin"})

    assert response.status_code == 200
    alerts = response.json()["data"]
    assert [(item["severity"], item["code"]) for item in alerts] == [
        ("warning", "run_failure_rate_high"),
        ("warning", "run_p95_latency_high"),
    ]
    assert alerts[0]["observed_value"] == pytest.approx(0.2)
    assert alerts[0]["threshold"] == 0.2
    assert alerts[0]["status"] == "active"
    assert alerts[0]["summary"] == "终态运行失败率达到或超过确定性阈值。"
    assert alerts[1]["summary"] == "运行 P95 耗时达到或超过确定性阈值。"


@pytest.mark.asyncio
async def test_runtime_failure_alert_counts_only_explicit_failed_outcomes() -> None:
    runtime = _runtime()
    for index, outcome in enumerate(("partial", "denied", "cancelled", "expired", "success")):
        _seed_run(
            runtime,
            run_id=f"run-nonfailure-{index}",
            session_id=f"ses-nonfailure-{index}",
            status=(
                "cancelled"
                if outcome == "cancelled"
                else "expired"
                if outcome == "expired"
                else "completed"
            ),
            outcome=outcome,
            started_offset=index,
        )
    app = create_app(runtime)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(BASE + "/alerts", params=WINDOW, headers={"geoToken": "admin"})

    assert response.status_code == 200
    assert all(item["code"] != "run_failure_rate_high" for item in response.json()["data"])


@pytest.mark.asyncio
async def test_runtime_observability_requires_admin_and_never_accepts_tenant_override() -> None:
    runtime = _runtime(_identity(roles=["governance_analyst"]))
    app = create_app(runtime)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        denied = await client.get(BASE + "/overview", headers={"geoToken": "user"})
        override = await client.get(
            BASE + "/overview?tenant_id=tenant-b", headers={"geoToken": "user"}
        )

    assert denied.status_code == 403
    assert override.status_code in {403, 422}


def test_runtime_observability_openapi_is_typed_and_contains_no_secret_fields() -> None:
    schema = create_app(_runtime()).openapi()
    paths = schema["paths"]
    template = "/capability-api/v1/applications/{app_id}/runtime"
    expected = {
        template + "/overview",
        template + "/sessions",
        template + "/runs",
        template + "/runs/{run_id}/timeline",
        template + "/models",
        template + "/capabilities",
        template + "/alerts",
    }
    assert expected <= set(paths)
    overview_schema = paths[template + "/overview"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]
    assert overview_schema["$ref"].endswith("/RuntimeOverviewResponse")
    timeline_ref = paths[template + "/runs/{run_id}/timeline"]["get"]["responses"]["200"][
        "content"
    ]["application/json"]["schema"]["$ref"]
    assert timeline_ref.endswith("/RuntimeTimelineResponse")
    schemas = str(
        {
            name: value
            for name, value in schema["components"]["schemas"].items()
            if name.startswith("Runtime")
        }
    ).casefold()
    for forbidden in ("credential_ref", "raw_prompt", "api_key", "request_body"):
        assert forbidden not in schemas


def test_runtime_observability_postgres_migration_adds_keyset_and_window_indexes() -> None:
    migration = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "migrations"
        / "V026_runtime_observability_indexes.sql"
    ).read_text(encoding="utf-8")
    assert "idx_fva_obs_sessions_scope_updated" in migration
    assert "idx_fva_obs_runs_scope_created" in migration
    assert "idx_fva_obs_runs_status" in migration
    assert "idx_fva_obs_events_window" in migration
    assert "CREATE TRIGGER trg_fva_obs_sessions_projection" in migration
    assert "CREATE TRIGGER trg_fva_obs_runs_projection" in migration
    assert "CREATE TRIGGER trg_fva_obs_events_projection" in migration
    persistence = PostgresAgentPersistence(
        dsn="postgresql://unused.invalid/test", schema="fva_observability_inventory"
    )
    assert any(
        "idx_fva_obs_sessions_scope_updated" in sql
        for sql in persistence._p2_migration_statements()
    )
