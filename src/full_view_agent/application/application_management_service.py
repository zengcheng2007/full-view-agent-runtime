"""Management boundary for applications and their capability grants."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol

from full_view_agent.application.errors import ResourceNotFound, RunStateConflict
from full_view_agent.domain.application import (
    AgentApplicationDefinition,
    ApplicationCapabilityBinding,
)
from full_view_agent.domain.capability import CapabilityBase


class ApplicationRegistry(Protocol):
    async def save_application(
        self,
        application: AgentApplicationDefinition,
        *,
        expected_etag: int | None = None,
    ) -> AgentApplicationDefinition: ...

    async def get_application(self, app_id: str) -> AgentApplicationDefinition | None: ...

    async def list_applications(
        self, *, active_only: bool = False
    ) -> list[AgentApplicationDefinition]: ...

    async def bind_capability(
        self, binding: ApplicationCapabilityBinding
    ) -> ApplicationCapabilityBinding: ...

    async def get_capability_binding(
        self, *, app_id: str, capability_id: str, capability_version: str
    ) -> ApplicationCapabilityBinding | None: ...

    async def save_capability_binding(
        self,
        binding: ApplicationCapabilityBinding,
        *,
        expected_etag: int | None = None,
    ) -> ApplicationCapabilityBinding: ...

    async def list_capability_bindings(
        self, *, app_id: str, enabled_only: bool = False
    ) -> list[ApplicationCapabilityBinding]: ...


class CapabilityReader(Protocol):
    async def get(self, capability_id: str, version: str) -> CapabilityBase | None: ...


class ApplicationManagementService:
    def __init__(
        self,
        *,
        application_registry: ApplicationRegistry,
        capability_repository: CapabilityReader,
    ) -> None:
        self._applications = application_registry
        self._capabilities = capability_repository

    async def register_application(
        self,
        application: AgentApplicationDefinition,
        *,
        changed_by: str = "system",
        reason: str = "",
    ) -> AgentApplicationDefinition:
        if application.status != "disabled":
            raise RunStateConflict("applications must be registered disabled")
        if await self._applications.get_application(application.app_id) is not None:
            raise RunStateConflict("application is already registered")
        now = datetime.now(UTC)
        registered = application.model_copy(
            update={
                "status": "disabled",
                "created_at": now,
                "updated_at": now,
                "updated_by": changed_by,
                "last_reason": reason,
                "etag": 1,
            }
        )
        return await self._applications.save_application(registered)

    async def enable_application(
        self,
        *,
        app_id: str,
        expected_etag: int,
        changed_by: str,
        reason: str = "",
    ) -> AgentApplicationDefinition:
        return await self._set_application_status(
            app_id=app_id,
            status="active",
            expected_etag=expected_etag,
            changed_by=changed_by,
            reason=reason,
        )

    async def disable_application(
        self,
        *,
        app_id: str,
        expected_etag: int,
        changed_by: str,
        reason: str = "",
    ) -> AgentApplicationDefinition:
        return await self._set_application_status(
            app_id=app_id,
            status="disabled",
            expected_etag=expected_etag,
            changed_by=changed_by,
            reason=reason,
        )

    async def _set_application_status(
        self,
        *,
        app_id: str,
        status: str,
        expected_etag: int,
        changed_by: str,
        reason: str,
    ) -> AgentApplicationDefinition:
        application = await self._applications.get_application(app_id)
        if application is None:
            raise ResourceNotFound("application is not registered")
        if application.etag != expected_etag:
            raise RunStateConflict("application etag mismatch")
        if application.status == status:
            raise RunStateConflict(f"application is already {status}")
        updated = application.model_copy(
            update={
                "status": status,
                "updated_at": datetime.now(UTC),
                "updated_by": changed_by,
                "last_reason": reason,
                "etag": application.etag + 1,
            }
        )
        return await self._applications.save_application(
            updated,
            expected_etag=expected_etag,
        )

    async def list_applications(
        self, *, active_only: bool = False
    ) -> list[AgentApplicationDefinition]:
        return await self._applications.list_applications(active_only=active_only)

    async def bind_capability(
        self,
        *,
        app_id: str,
        capability_id: str,
        capability_version: str,
        changed_by: str = "system",
        reason: str = "",
    ) -> ApplicationCapabilityBinding:
        application = await self._applications.get_application(app_id)
        if application is None:
            raise ResourceNotFound("application is not registered")
        capability = await self._capabilities.get(
            capability_id,
            capability_version,
        )
        if capability is None or capability.status != "published":
            raise RunStateConflict("binding requires a published capability")
        existing = await self._applications.get_capability_binding(
            app_id=app_id,
            capability_id=capability_id,
            capability_version=capability_version,
        )
        if existing is not None:
            raise RunStateConflict("capability binding already exists")
        return await self._applications.save_capability_binding(
            ApplicationCapabilityBinding(
                app_id=app_id,
                capability_id=capability_id,
                capability_version=capability_version,
                enabled=False,
                changed_by=changed_by,
                reason=reason,
            )
        )

    async def enable_capability_binding(
        self,
        *,
        app_id: str,
        capability_id: str,
        capability_version: str,
        expected_etag: int,
        changed_by: str,
        reason: str = "",
    ) -> ApplicationCapabilityBinding:
        application = await self._applications.get_application(app_id)
        if application is None:
            raise ResourceNotFound("application is not registered")
        if application.status != "active":
            raise RunStateConflict("binding enable requires an active application")
        capability = await self._capabilities.get(capability_id, capability_version)
        if capability is None or capability.status != "published":
            raise RunStateConflict("binding enable requires a published capability")
        return await self._set_binding_enabled(
            app_id=app_id,
            capability_id=capability_id,
            capability_version=capability_version,
            enabled=True,
            expected_etag=expected_etag,
            changed_by=changed_by,
            reason=reason,
        )

    async def disable_capability_binding(
        self,
        *,
        app_id: str,
        capability_id: str,
        capability_version: str,
        expected_etag: int,
        changed_by: str,
        reason: str = "",
    ) -> ApplicationCapabilityBinding:
        return await self._set_binding_enabled(
            app_id=app_id,
            capability_id=capability_id,
            capability_version=capability_version,
            enabled=False,
            expected_etag=expected_etag,
            changed_by=changed_by,
            reason=reason,
        )

    async def _set_binding_enabled(
        self,
        *,
        app_id: str,
        capability_id: str,
        capability_version: str,
        enabled: bool,
        expected_etag: int,
        changed_by: str,
        reason: str,
    ) -> ApplicationCapabilityBinding:
        binding = await self._applications.get_capability_binding(
            app_id=app_id,
            capability_id=capability_id,
            capability_version=capability_version,
        )
        if binding is None:
            raise ResourceNotFound("capability binding not found")
        if binding.etag != expected_etag:
            raise RunStateConflict("capability binding etag mismatch")
        if binding.enabled == enabled:
            state = "enabled" if enabled else "disabled"
            raise RunStateConflict(f"capability binding is already {state}")
        updated = binding.model_copy(
            update={
                "enabled": enabled,
                "updated_at": datetime.now(UTC),
                "changed_by": changed_by,
                "reason": reason,
                "etag": binding.etag + 1,
            }
        )
        return await self._applications.save_capability_binding(
            updated,
            expected_etag=expected_etag,
        )

    async def list_capability_bindings(
        self,
        *,
        app_id: str,
        enabled_only: bool = False,
    ) -> list[ApplicationCapabilityBinding]:
        if await self._applications.get_application(app_id) is None:
            raise ResourceNotFound("application is not registered")
        return await self._applications.list_capability_bindings(
            app_id=app_id,
            enabled_only=enabled_only,
        )
