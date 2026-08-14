from datetime import UTC, datetime, timedelta
from typing import Literal

import pytest
from pydantic import SecretStr

from full_view_agent.api.app import RuntimeContainer
from full_view_agent.application.application_identity_registry import (
    ApplicationIdentityAdapterRegistry,
    ApplicationIdentityAdapterUnavailable,
)
from full_view_agent.domain.application import AgentApplicationDefinition
from full_view_agent.domain.models import LegacyIdentitySnapshot, Principal
from full_view_agent.infrastructure.credential_broker import InMemoryCredentialBroker


class RecordingIdentityAdapter:
    def __init__(self, *, source: str) -> None:
        self.source = source
        self.tokens: list[str] = []

    async def resolve(self, raw_token: SecretStr) -> LegacyIdentitySnapshot:
        self.tokens.append(raw_token.get_secret_value())
        return LegacyIdentitySnapshot(
            principal=Principal(
                tenant_id="tenant-address",
                user_id="address-user",
                org_id="address-org",
                roles=["address_analyst"],
            ),
            source=self.source,
            source_session_expires_at=datetime.now(UTC) + timedelta(hours=1),
            base_area_codes=[],
        )


def _application(
    *, status: Literal["active", "disabled"] = "active"
) -> AgentApplicationDefinition:
    return AgentApplicationDefinition(
        app_id="unified_address",
        name="统一地址平台",
        default_agent_id="unified_address_agent",
        identity_adapter_id="identity.unified_address",
        status=status,
    )


@pytest.mark.asyncio
async def test_registry_resolves_identity_through_application_bound_adapter() -> None:
    adapter = RecordingIdentityAdapter(source="unified-address")
    registry = ApplicationIdentityAdapterRegistry(
        {"identity.unified_address": adapter}
    )

    context = await registry.resolve_for_application(
        application=_application(),
        raw_token=SecretStr("address-token"),
    )

    assert context.app_id == "unified_address"
    assert context.agent_id == "unified_address_agent"
    assert context.identity.principal.tenant_id == "tenant-address"
    assert context.identity.principal.user_id == "address-user"
    assert adapter.tokens == ["address-token"]


@pytest.mark.asyncio
async def test_registry_rejects_disabled_application_before_calling_adapter() -> None:
    adapter = RecordingIdentityAdapter(source="unified-address")
    registry = ApplicationIdentityAdapterRegistry(
        {"identity.unified_address": adapter}
    )

    with pytest.raises(ApplicationIdentityAdapterUnavailable, match="disabled"):
        await registry.resolve_for_application(
            application=_application(status="disabled"),
            raw_token=SecretStr("address-token"),
        )

    assert adapter.tokens == []


@pytest.mark.asyncio
async def test_registry_rejects_unregistered_identity_adapter() -> None:
    registry = ApplicationIdentityAdapterRegistry({})

    with pytest.raises(ApplicationIdentityAdapterUnavailable, match="not registered"):
        await registry.resolve_for_application(
            application=_application(),
            raw_token=SecretStr("address-token"),
        )


@pytest.mark.asyncio
async def test_runtime_resolves_fixed_full_view_application_with_legacy_adapter() -> None:
    adapter = RecordingIdentityAdapter(source="legacy-geo")
    runtime = RuntimeContainer(
        identity_port=adapter,
        credentials=InMemoryCredentialBroker(),
    )

    context = await runtime.resolve_application_identity(
        app_id="full_information_view",
        raw_token=SecretStr("geo-token"),
    )

    assert context.app_id == "full_information_view"
    assert context.identity_adapter_id == "identity.legacy_geo"
    assert adapter.tokens == ["geo-token"]


@pytest.mark.asyncio
async def test_runtime_rejects_reserved_unified_address_without_real_adapter() -> None:
    runtime = RuntimeContainer(credentials=InMemoryCredentialBroker())

    with pytest.raises(ApplicationIdentityAdapterUnavailable):
        await runtime.resolve_application_identity(
            app_id="unified_address",
            raw_token=SecretStr("address-token"),
        )
