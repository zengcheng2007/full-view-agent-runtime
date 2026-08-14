from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import httpx
import pytest

from full_view_agent.api.app import RuntimeContainer, create_app
from full_view_agent.application.capability_management_service import (
    CapabilityManagementService,
)
from full_view_agent.application.errors import RunStateConflict
from full_view_agent.domain.capability import Connector
from full_view_agent.domain.models import LegacyIdentitySnapshot, Principal
from full_view_agent.infrastructure.capability_repository import (
    InMemoryCapabilityRepository,
)
from full_view_agent.infrastructure.credential_broker import InMemoryCredentialBroker


async def _seed(repository: InMemoryCapabilityRepository) -> Connector:
    connector = Connector(
        connector_id="connector.governance",
        name="治理网关",
        base_url="https://93.184.216.34/api",
        allowed_path_prefixes=["/api"],
        denied_hosts=[],
        credential_ref="vault.governance.secret",
        created_by="seed-admin",
        updated_by="seed-admin",
    )
    await repository.save_connector(connector)
    return connector


def _runtime(repository: InMemoryCapabilityRepository) -> RuntimeContainer:
    runtime = RuntimeContainer(
        credentials=InMemoryCredentialBroker(),
        capability_repository=repository,
    )
    runtime.connector_allowed_private_hosts = frozenset()
    runtime.capability_identity_port = AsyncMock()
    runtime.capability_identity_port.resolve = AsyncMock(
        return_value=LegacyIdentitySnapshot(
            principal=Principal(
                tenant_id="platform",
                user_id="connector-admin",
                org_id="platform-admins",
                roles=["admin"],
            ),
            source="connector-management-test",
            source_session_expires_at=datetime.now(UTC) + timedelta(minutes=5),
            base_area_codes=[],
        )
    )
    return runtime


@pytest.mark.asyncio
async def test_connector_update_and_disable_are_optimistic_and_audited() -> None:
    repository = InMemoryCapabilityRepository()
    original = await _seed(repository)
    service = CapabilityManagementService(repository, allowed_private_hosts=frozenset())

    updated = await service.update_connector(
        connector_id=original.connector_id,
        expected_etag=1,
        actor="connector-admin",
        reason="切换健康检查路径",
        name="治理网关（新版）",
        allowed_path_prefixes=["/api/v2"],
    )
    disabled = await service.set_connector_active(
        connector_id=original.connector_id,
        is_active=False,
        expected_etag=2,
        actor="connector-admin",
        reason="上游维护",
    )

    assert updated.etag == 2
    assert updated.updated_by == "connector-admin"
    assert disabled.etag == 3
    assert disabled.is_active is False
    events = await repository.list_connector_audit_events(original.connector_id)
    assert [(event.action, event.reason, event.actor) for event in events] == [
        ("update", "切换健康检查路径", "connector-admin"),
        ("disable", "上游维护", "connector-admin"),
    ]

    with pytest.raises(RunStateConflict):
        await service.set_connector_active(
            connector_id=original.connector_id,
            is_active=True,
            expected_etag=2,
            actor="connector-admin",
            reason="过期页面提交",
        )

    with pytest.raises(RunStateConflict):
        await service.update_connector(
            connector_id=original.connector_id,
            expected_etag=1,
            actor="connector-admin",
            reason="过期编辑不得触发目标校验",
            base_url="http://169.254.169.254/latest/meta-data",
        )


@pytest.mark.asyncio
async def test_connector_patch_reuses_ssrf_and_path_security_validation() -> None:
    repository = InMemoryCapabilityRepository()
    await _seed(repository)
    service = CapabilityManagementService(repository, allowed_private_hosts=frozenset())

    with pytest.raises(ValueError, match="SSRF"):
        await service.update_connector(
            connector_id="connector.governance",
            expected_etag=1,
            actor="connector-admin",
            reason="错误配置",
            base_url="http://169.254.169.254/latest/meta-data",
        )
    with pytest.raises(ValueError, match="允许路径"):
        await service.update_connector(
            connector_id="connector.governance",
            expected_etag=1,
            actor="connector-admin",
            reason="错误配置",
            allowed_path_prefixes=["api", "/api/../admin"],
        )

    with pytest.raises(ValueError, match="允许路径"):
        await service.update_connector(
            connector_id="connector.governance",
            expected_etag=1,
            actor="connector-admin",
            reason="拒绝双斜线路径",
            allowed_path_prefixes=["//geo-qxst/api/v1"],
        )

    with pytest.raises(ValueError, match="允许路径"):
        await service.update_connector(
            connector_id="connector.governance",
            expected_etag=1,
            actor="connector-admin",
            reason="拒绝非规范尾斜线",
            allowed_path_prefixes=["/api/v1/"],
        )


@pytest.mark.asyncio
async def test_connector_can_correct_legacy_noncanonical_path_but_create_is_unique() -> None:
    repository = InMemoryCapabilityRepository()
    legacy = Connector(
        connector_id="legacy.connector",
        name="历史连接器",
        base_url="https://93.184.216.34",
        allowed_path_prefixes=["//geo-qxst/api/v1"],
    )
    await repository.save_connector(legacy)
    service = CapabilityManagementService(repository, allowed_private_hosts=frozenset())

    corrected = await service.update_connector(
        connector_id=legacy.connector_id,
        expected_etag=1,
        actor="connector-admin",
        reason="修正历史双斜线路径",
        allowed_path_prefixes=["/geo-qxst/api/v1"],
    )
    assert corrected.allowed_path_prefixes == ["/geo-qxst/api/v1"]

    with pytest.raises(RunStateConflict, match="already exists"):
        await service.create_connector(
            connector_id=legacy.connector_id,
            name="不应覆盖",
            base_url="https://93.184.216.34",
            allowed_path_prefixes=["/api"],
            created_by="connector-admin",
        )


@pytest.mark.asyncio
async def test_connector_create_reuses_explicit_private_host_allowlist() -> None:
    repository = InMemoryCapabilityRepository()
    service = CapabilityManagementService(
        repository, allowed_private_hosts=frozenset({"127.0.0.1"})
    )

    created = await service.create_connector(
        connector_id="connector.local-governance",
        name="本地治理网关",
        base_url="http://127.0.0.1:9666",
        allowed_path_prefixes=["/geo-qxst/api/v1"],
        created_by="connector-admin",
    )
    assert created.base_url == "http://127.0.0.1:9666"

    with pytest.raises(ValueError, match="SSRF"):
        await service.create_connector(
            connector_id="connector.local-denied",
            name="拒绝优先连接器",
            base_url="http://127.0.0.1:9666",
            allowed_path_prefixes=["/api"],
            denied_hosts=["127.0.0.1"],
            created_by="connector-admin",
        )


@pytest.mark.asyncio
async def test_concurrent_connector_create_does_not_overwrite_identity() -> None:
    repository = InMemoryCapabilityRepository()
    first = Connector(
        connector_id="connector.concurrent",
        name="连接器甲",
        base_url="https://93.184.216.34",
        allowed_path_prefixes=["/api"],
    )
    second = first.model_copy(update={"name": "连接器乙"})

    outcomes = await asyncio.gather(
        repository.save_connector(first),
        repository.save_connector(second),
        return_exceptions=True,
    )
    assert sum(item is None for item in outcomes) == 1
    assert sum(isinstance(item, RunStateConflict) for item in outcomes) == 1


@pytest.mark.asyncio
async def test_connector_http_management_masks_credential_and_returns_409() -> None:
    repository = InMemoryCapabilityRepository()
    await _seed(repository)
    app = create_app(_runtime(repository))
    headers = {"geoToken": "legacy-admin-token"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers=headers,
    ) as client:
        listed = await client.get(
            "/capability-api/v1/connectors", params={"active_only": "false"}
        )
        fetched = await client.get(
            "/capability-api/v1/connectors/connector.governance"
        )
        patched = await client.patch(
            "/capability-api/v1/connectors/connector.governance",
            json={
                "expected_etag": 1,
                "reason": "更新名称",
                "name": "治理网关二期",
            },
        )
        conflict = await client.post(
            "/capability-api/v1/connectors/connector.governance/disable",
            json={"expected_etag": 1, "reason": "旧页面操作"},
        )
        audit = await client.get(
            "/capability-api/v1/connectors/connector.governance/audit-events"
        )

    assert listed.status_code == 200
    assert listed.json()["data"][0]["credential_ref"] == "***"
    assert "vault.governance.secret" not in listed.text
    assert fetched.status_code == 200
    assert fetched.json()["data"]["credential_ref"] == "***"
    assert "vault.governance.secret" not in fetched.text
    assert patched.status_code == 200
    assert patched.json()["data"]["etag"] == 2
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "run_state_conflict"
    assert audit.status_code == 200
    assert audit.json()["data"] == [
        {
            "event_id": audit.json()["data"][0]["event_id"],
            "connector_id": "connector.governance",
            "action": "update",
            "actor": "connector-admin",
            "reason": "更新名称",
            "previous_etag": 1,
            "new_etag": 2,
            "changed_fields": ["name"],
            "from_active": True,
            "to_active": True,
            "changed_at": audit.json()["data"][0]["changed_at"],
        }
    ]


@pytest.mark.asyncio
async def test_connector_http_rejects_unsafe_configuration_as_422_not_500() -> None:
    repository = InMemoryCapabilityRepository()
    await _seed(repository)
    app = create_app(_runtime(repository))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"geoToken": "legacy-admin-token"},
    ) as client:
        response = await client.patch(
            "/capability-api/v1/connectors/connector.governance",
            json={
                "expected_etag": 1,
                "reason": "拒绝不安全路径",
                "allowed_path_prefixes": ["//geo-qxst/api/v1"],
            },
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "connector_configuration_invalid"


@pytest.mark.asyncio
@pytest.mark.parametrize("suffix", ["enable", "disable"])
async def test_connector_lifecycle_requires_reason(suffix: str) -> None:
    repository = InMemoryCapabilityRepository()
    await _seed(repository)
    app = create_app(_runtime(repository))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"geoToken": "legacy-admin-token"},
    ) as client:
        response = await client.post(
            f"/capability-api/v1/connectors/connector.governance/{suffix}",
            json={"expected_etag": 1},
        )

    assert response.status_code == 422
