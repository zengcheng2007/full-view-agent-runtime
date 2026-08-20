from __future__ import annotations

import pytest

from full_view_agent.application.application_management_service import (
    ApplicationManagementService,
)
from full_view_agent.application.errors import RunStateConflict
from full_view_agent.domain.application import AgentApplicationDefinition
from full_view_agent.domain.capability import ToolCapability
from full_view_agent.infrastructure.application_registry import (
    InMemoryApplicationRegistry,
)
from full_view_agent.infrastructure.capability_repository import (
    InMemoryCapabilityRepository,
)


def _application(*, status: str = "disabled") -> AgentApplicationDefinition:
    return AgentApplicationDefinition(
        app_id="unified_address",
        name="统一地址平台",
        default_agent_id="unified_address_agent",
        identity_adapter_id="identity.unified_address",
        status=status,  # type: ignore[arg-type]
    )


async def _service() -> tuple[
    ApplicationManagementService,
    InMemoryApplicationRegistry,
    InMemoryCapabilityRepository,
]:
    applications = InMemoryApplicationRegistry()
    capabilities = InMemoryCapabilityRepository()
    return (
        ApplicationManagementService(
            application_registry=applications,
            capability_repository=capabilities,
        ),
        applications,
        capabilities,
    )


@pytest.mark.asyncio
async def test_application_registration_cannot_activate_in_one_post() -> None:
    service, _, _ = await _service()

    with pytest.raises(RunStateConflict, match="registered disabled"):
        await service.register_application(_application(status="active"))


@pytest.mark.asyncio
async def test_application_enable_disable_is_etag_guarded_and_traced() -> None:
    service, _, _ = await _service()
    created = await service.register_application(
        _application(), changed_by="admin-a", reason="initial registration"
    )

    enabled = await service.enable_application(
        app_id=created.app_id,
        expected_etag=created.etag,
        changed_by="admin-b",
        reason="identity adapter accepted",
    )

    assert enabled.status == "active"
    assert enabled.etag == created.etag + 1
    assert enabled.updated_by == "admin-b"
    assert enabled.last_reason == "identity adapter accepted"

    with pytest.raises(RunStateConflict, match="etag"):
        await service.disable_application(
            app_id=enabled.app_id,
            expected_etag=created.etag,
            changed_by="admin-c",
            reason="stale request",
        )

    disabled = await service.disable_application(
        app_id=enabled.app_id,
        expected_etag=enabled.etag,
        changed_by="admin-c",
        reason="maintenance",
    )
    assert disabled.status == "disabled"
    assert disabled.updated_by == "admin-c"
    assert disabled.last_reason == "maintenance"


@pytest.mark.asyncio
async def test_binding_is_created_disabled_then_explicitly_enabled_and_disabled() -> None:
    service, _, capabilities = await _service()
    application = await service.register_application(_application())
    application = await service.enable_application(
        app_id=application.app_id,
        expected_etag=application.etag,
        changed_by="admin",
        reason="ready",
    )
    capability = ToolCapability(
        capability_id="address.resolve_standard",
        name="标准地址解析",
        owner="address-team",
        version="1.0.0",
        status="published",
        guidance="Test guidance for 标准地址解析",
        connector_ref="address-api",
        resource_path="/v1/resolve",
    )
    await capabilities.save_tool(capability)

    created = await service.bind_capability(
        app_id=application.app_id,
        capability_id=capability.capability_id,
        capability_version=capability.version,
        changed_by="admin-a",
        reason="candidate binding",
    )
    assert created.enabled is False
    assert created.changed_by == "admin-a"

    enabled = await service.enable_capability_binding(
        app_id=application.app_id,
        capability_id=capability.capability_id,
        capability_version=capability.version,
        expected_etag=created.etag,
        changed_by="admin-b",
        reason="smoke test passed",
    )
    assert enabled.enabled is True
    assert enabled.etag == created.etag + 1
    assert enabled.reason == "smoke test passed"

    disabled = await service.disable_capability_binding(
        app_id=application.app_id,
        capability_id=capability.capability_id,
        capability_version=capability.version,
        expected_etag=enabled.etag,
        changed_by="admin-c",
        reason="upstream incident",
    )
    assert disabled.enabled is False
    assert disabled.changed_by == "admin-c"


@pytest.mark.asyncio
async def test_binding_cannot_be_enabled_while_application_is_disabled() -> None:
    service, _, capabilities = await _service()
    application = await service.register_application(_application())
    capability = ToolCapability(
        capability_id="address.resolve_standard",
        name="标准地址解析",
        owner="address-team",
        version="1.0.0",
        status="published",
        guidance="Test guidance for 标准地址解析",
        connector_ref="address-api",
        resource_path="/v1/resolve",
    )
    await capabilities.save_tool(capability)
    binding = await service.bind_capability(
        app_id=application.app_id,
        capability_id=capability.capability_id,
        capability_version=capability.version,
    )

    with pytest.raises(RunStateConflict, match="active application"):
        await service.enable_capability_binding(
            app_id=application.app_id,
            capability_id=capability.capability_id,
            capability_version=capability.version,
            expected_etag=binding.etag,
            changed_by="admin",
        )


def test_v015_migration_adds_lifecycle_trace_columns() -> None:
    from pathlib import Path

    migration = (
        Path(__file__).parents[1] / "scripts/migrations/V015_application_lifecycle.sql"
    ).read_text(encoding="utf-8")

    for column in ("etag", "updated_by", "last_reason", "updated_at", "changed_by", "reason"):
        assert column in migration
    assert "SELECT 15" in migration
