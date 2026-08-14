from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import httpx
import pytest

from full_view_agent.api.app import RuntimeContainer, create_app
from full_view_agent.application.context_builder import AgentContextBuilder
from full_view_agent.application.errors import RunStateConflict
from full_view_agent.application.harness import HarnessState
from full_view_agent.application.prompt_template_service import PromptTemplateService
from full_view_agent.application.runtime_prompt_registry import RuntimePromptRegistry
from full_view_agent.application.session_run_service import SessionRunService
from full_view_agent.application.tool_registry import ToolRegistry
from full_view_agent.domain.models import LegacyIdentitySnapshot, Principal
from full_view_agent.domain.prompt_template import RuntimePromptSnapshot
from full_view_agent.infrastructure.credential_broker import InMemoryCredentialBroker
from full_view_agent.infrastructure.memory_store import InMemoryAgentStore
from full_view_agent.infrastructure.prompt_template_repository import (
    InMemoryPromptTemplateRepository,
)

from .test_policy import population_auth_context
from .test_session_run_service import run_request


@pytest.mark.asyncio
async def test_prompt_lifecycle_exposes_only_approved_published_guidance() -> None:
    repo = InMemoryPromptTemplateRepository()
    service = PromptTemplateService(repo)
    created = await service.create(
        prompt_id="full-view.operator-guidance",
        app_id="full_information_view",
        name="全息图补充指令",
        version="1.0.0",
        content="回答使用简洁中文。",
        actor="admin",
        reason="初始化",
    )
    assert await service.get_effective(app_id="full_information_view") is None

    testing = await service.transition(
        prompt_id=created.prompt_id,
        version=created.version,
        to_status="testing",
        expected_etag=created.etag,
        actor="admin",
        reason="测试",
    )
    approved = await service.transition(
        prompt_id=created.prompt_id,
        version=created.version,
        to_status="pending_approval",
        expected_etag=testing.etag,
        actor="approver",
        reason="审批通过",
    )
    published = await service.transition(
        prompt_id=created.prompt_id,
        version=created.version,
        to_status="published",
        expected_etag=approved.etag,
        actor="publisher",
        reason="发布",
    )

    effective = await service.get_effective(app_id="full_information_view")
    assert effective is not None
    assert effective.content == "回答使用简洁中文。"
    assert effective.composite_version == "full-view.operator-guidance@1.0.0"

    events = await repo.list_events(prompt_id=created.prompt_id)
    assert [event.to_status for event in events] == [
        "draft",
        "testing",
        "pending_approval",
        "published",
    ]
    assert published.etag == 4


@pytest.mark.asyncio
async def test_prompt_publish_rejects_second_effective_version_per_app() -> None:
    repo = InMemoryPromptTemplateRepository()
    service = PromptTemplateService(repo)

    async def publish(version: str) -> None:
        item = await service.create(
            prompt_id="full-view.operator-guidance",
            app_id="full_information_view",
            name="补充指令",
            version=version,
            content=f"版本 {version}",
            actor="admin",
            reason="创建",
        )
        for status in ("testing", "pending_approval", "published"):
            item = await service.transition(
                prompt_id=item.prompt_id,
                version=item.version,
                to_status=status,
                expected_etag=item.etag,
                actor="admin",
                reason=status,
            )

    await publish("1.0.0")
    with pytest.raises(RunStateConflict, match="already published"):
        await publish("1.1.0")


@pytest.mark.asyncio
async def test_context_builder_pins_prompt_snapshot_for_existing_run() -> None:
    store = InMemoryAgentStore()
    session_service = SessionRunService(store)
    session = await session_service.create_session(user_id="user-01", title="提示词")
    run = await session_service.create_run(
        user_id="user-01", session_id=session.session_id, request=run_request()
    )
    auth = population_auth_context().model_copy(
        update={"session_id": session.session_id, "run_id": run.run_id}
    )
    v1 = RuntimePromptSnapshot(
        prompt_id="full-view.guidance",
        app_id="full_information_view",
        version="1.0.0",
        content="使用第一版业务术语。",
    )
    registry = RuntimePromptRegistry(v1)
    live = AgentContextBuilder(
        store=store,
        registry=ToolRegistry.default(),
        prompt_registry=registry,
    )
    pinned = live.for_registry(ToolRegistry.default(), prompt_snapshot=v1)
    registry.activate(
        v1.model_copy(update={"version": "2.0.0", "content": "使用第二版业务术语。"})
    )

    old_request = await pinned.build(
        user_id="user-01", auth_context=auth, state=HarnessState()
    )
    new_request = await live.build(
        user_id="user-01", auth_context=auth, state=HarnessState()
    )

    assert "第一版业务术语" in (old_request.messages[0].content or "")
    assert old_request.prompt_version.endswith("full-view.guidance@1.0.0")
    assert "第二版业务术语" in (new_request.messages[0].content or "")
    assert new_request.prompt_version.endswith("full-view.guidance@2.0.0")


@pytest.mark.asyncio
async def test_prompt_control_plane_http_lifecycle_and_effective_contract() -> None:
    runtime = RuntimeContainer(credentials=InMemoryCredentialBroker())
    identity = LegacyIdentitySnapshot(
        principal=Principal(
            tenant_id="platform",
            user_id="prompt-admin",
            org_id="platform-admins",
            roles=["admin"],
        ),
        source="prompt-test",
        source_session_expires_at=datetime.now(UTC) + timedelta(minutes=5),
        base_area_codes=[],
    )
    runtime.capability_identity_port = AsyncMock()
    runtime.capability_identity_port.resolve = AsyncMock(return_value=identity)
    app = create_app(runtime)
    headers = {"geoToken": "admin-token"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers=headers,
    ) as client:
        response = await client.post(
            "/capability-api/v1/prompt-templates",
            json={
                "prompt_id": "full-view.operator-guidance",
                "app_id": "full_information_view",
                "name": "补充指令",
                "version": "1.0.0",
                "content": "统一使用中文业务术语。",
                "reason": "初始化",
            },
        )
        assert response.status_code == 201
        item = response.json()["data"]
        for action in ("testing", "approve", "publish"):
            response = await client.post(
                f"/capability-api/v1/prompt-templates/"
                f"{item['prompt_id']}/{item['version']}/{action}",
                json={"expected_etag": item["etag"], "reason": action},
            )
            assert response.status_code == 200
            item = response.json()["data"]
        response = await client.get(
            "/capability-api/v1/prompt-templates/effective",
            params={"app_id": "full_information_view"},
        )
        audit = await client.get(
            "/capability-api/v1/prompt-templates/"
            "full-view.operator-guidance/audit-events"
        )

    assert response.status_code == 200
    assert response.json()["data"]["content"] == "统一使用中文业务术语。"
    assert runtime.runtime_prompt_registry.snapshot() is not None
    assert audit.status_code == 200
    assert [event["to_status"] for event in audit.json()["data"]] == [
        "draft",
        "testing",
        "pending_approval",
        "published",
    ]
