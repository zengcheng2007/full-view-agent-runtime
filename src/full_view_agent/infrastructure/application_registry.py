"""Application registry repositories.

The in-memory implementation is used by tests and local composition.  The
PostgreSQL implementation is added after the public behavior is frozen.
"""

# pyright: reportArgumentType=false, reportCallIssue=false

from __future__ import annotations

import asyncio

from full_view_agent.application.errors import ResourceNotFound, RunStateConflict
from full_view_agent.domain.application import (
    AgentApplicationDefinition,
    ApplicationCapabilityBinding,
)


class InMemoryApplicationRegistry:
    def __init__(
        self,
        applications: list[AgentApplicationDefinition] | None = None,
        bindings: list[ApplicationCapabilityBinding] | None = None,
    ) -> None:
        self._applications: dict[str, AgentApplicationDefinition] = {
            item.app_id: item for item in applications or []
        }
        self._bindings: dict[tuple[str, str, str], ApplicationCapabilityBinding] = {
            (item.app_id, item.capability_id, item.capability_version): item
            for item in bindings or []
        }
        self._lock = asyncio.Lock()

    async def save_application(
        self,
        application: AgentApplicationDefinition,
        *,
        expected_etag: int | None = None,
    ) -> AgentApplicationDefinition:
        async with self._lock:
            existing = self._applications.get(application.app_id)
            if expected_etag is not None and (existing is None or existing.etag != expected_etag):
                raise RunStateConflict("application etag mismatch")
            self._applications[application.app_id] = application
        return application

    async def get_application(self, app_id: str) -> AgentApplicationDefinition | None:
        return self._applications.get(app_id)

    async def list_applications(
        self, *, active_only: bool = False
    ) -> list[AgentApplicationDefinition]:
        applications = self._applications.values()
        if active_only:
            applications = (item for item in applications if item.status == "active")
        return sorted(applications, key=lambda item: item.app_id)

    async def bind_capability(
        self, binding: ApplicationCapabilityBinding
    ) -> ApplicationCapabilityBinding:
        return await self.save_capability_binding(binding)

    async def get_capability_binding(
        self, *, app_id: str, capability_id: str, capability_version: str
    ) -> ApplicationCapabilityBinding | None:
        return self._bindings.get((app_id, capability_id, capability_version))

    async def save_capability_binding(
        self,
        binding: ApplicationCapabilityBinding,
        *,
        expected_etag: int | None = None,
    ) -> ApplicationCapabilityBinding:
        application = await self.get_application(binding.app_id)
        if application is None:
            raise ResourceNotFound("application is not registered")
        key = (
            binding.app_id,
            binding.capability_id,
            binding.capability_version,
        )
        async with self._lock:
            existing = self._bindings.get(key)
            if expected_etag is not None and (existing is None or existing.etag != expected_etag):
                raise RunStateConflict("capability binding etag mismatch")
            self._bindings[key] = binding
        return binding

    async def list_capability_bindings(
        self,
        *,
        app_id: str,
        enabled_only: bool = False,
    ) -> list[ApplicationCapabilityBinding]:
        bindings = (item for item in self._bindings.values() if item.app_id == app_id)
        if enabled_only:
            bindings = (item for item in bindings if item.enabled)
        return sorted(
            bindings,
            key=lambda item: (item.capability_id, item.capability_version),
        )


class PostgresApplicationRegistry:
    def __init__(self, *, dsn: str, schema: str = "full_view_agent") -> None:
        self._dsn = dsn
        self._schema = schema

    async def save_application(
        self,
        application: AgentApplicationDefinition,
        *,
        expected_etag: int | None = None,
    ) -> AgentApplicationDefinition:
        import psycopg

        async with await psycopg.AsyncConnection.connect(self._dsn) as conn:
            if expected_etag is None:
                await conn.execute(
                    f"""
                INSERT INTO {self._schema}.agent_applications (
                    app_id, name, default_agent_id, identity_adapter_id,
                    status, description, created_at, updated_at,
                    updated_by, last_reason, etag
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (app_id) DO UPDATE SET
                    name = EXCLUDED.name,
                    default_agent_id = EXCLUDED.default_agent_id,
                    identity_adapter_id = EXCLUDED.identity_adapter_id,
                    status = EXCLUDED.status,
                    description = EXCLUDED.description,
                    updated_at = EXCLUDED.updated_at,
                    updated_by = EXCLUDED.updated_by,
                    last_reason = EXCLUDED.last_reason,
                    etag = EXCLUDED.etag
                    """,
                    _application_values(application),
                )
            else:
                cursor = await conn.execute(
                    f"""
                    UPDATE {self._schema}.agent_applications SET
                        name = %s, default_agent_id = %s, identity_adapter_id = %s,
                        status = %s, description = %s, updated_at = %s,
                        updated_by = %s, last_reason = %s, etag = %s
                    WHERE app_id = %s AND etag = %s
                    """,
                    (
                        application.name,
                        application.default_agent_id,
                        application.identity_adapter_id,
                        application.status,
                        application.description,
                        application.updated_at,
                        application.updated_by,
                        application.last_reason,
                        application.etag,
                        application.app_id,
                        expected_etag,
                    ),
                )
                if cursor.rowcount != 1:
                    raise RunStateConflict("application etag mismatch")
        return application

    async def get_application(self, app_id: str) -> AgentApplicationDefinition | None:
        import psycopg

        async with await psycopg.AsyncConnection.connect(self._dsn) as conn:
            cursor = await conn.execute(
                f"""
                SELECT app_id, name, default_agent_id, identity_adapter_id,
                       status, description, created_at, updated_at,
                       updated_by, last_reason, etag
                FROM {self._schema}.agent_applications
                WHERE app_id = %s
                """,
                (app_id,),
            )
            row = await cursor.fetchone()
        return _application_from_row(row) if row is not None else None

    async def list_applications(
        self, *, active_only: bool = False
    ) -> list[AgentApplicationDefinition]:
        import psycopg

        where = "WHERE status = 'active'" if active_only else ""
        async with await psycopg.AsyncConnection.connect(self._dsn) as conn:
            cursor = await conn.execute(
                f"""
                SELECT app_id, name, default_agent_id, identity_adapter_id,
                       status, description, created_at, updated_at,
                       updated_by, last_reason, etag
                FROM {self._schema}.agent_applications
                {where}
                ORDER BY app_id
                """
            )
            rows = await cursor.fetchall()
        return [_application_from_row(row) for row in rows]

    async def bind_capability(
        self, binding: ApplicationCapabilityBinding
    ) -> ApplicationCapabilityBinding:
        return await self.save_capability_binding(binding)

    async def get_capability_binding(
        self, *, app_id: str, capability_id: str, capability_version: str
    ) -> ApplicationCapabilityBinding | None:
        import psycopg

        async with await psycopg.AsyncConnection.connect(self._dsn) as conn:
            cursor = await conn.execute(
                f"""
                SELECT app_id, capability_id, capability_version, enabled,
                       created_at, updated_at, changed_by, reason, etag
                FROM {self._schema}.application_capability_bindings
                WHERE app_id = %s AND capability_id = %s AND capability_version = %s
                """,
                (app_id, capability_id, capability_version),
            )
            row = await cursor.fetchone()
        return _binding_from_row(row) if row is not None else None

    async def save_capability_binding(
        self,
        binding: ApplicationCapabilityBinding,
        *,
        expected_etag: int | None = None,
    ) -> ApplicationCapabilityBinding:
        import psycopg

        if await self.get_application(binding.app_id) is None:
            raise ResourceNotFound("application is not registered")
        async with await psycopg.AsyncConnection.connect(self._dsn) as conn:
            if expected_etag is None:
                await conn.execute(
                    f"""
                INSERT INTO {self._schema}.application_capability_bindings (
                    app_id, capability_id, capability_version, enabled, created_at,
                    updated_at, changed_by, reason, etag
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (app_id, capability_id, capability_version)
                DO UPDATE SET enabled = EXCLUDED.enabled,
                    updated_at = EXCLUDED.updated_at,
                    changed_by = EXCLUDED.changed_by,
                    reason = EXCLUDED.reason,
                    etag = EXCLUDED.etag
                    """,
                    _binding_values(binding),
                )
            else:
                cursor = await conn.execute(
                    f"""
                    UPDATE {self._schema}.application_capability_bindings SET
                        enabled = %s, updated_at = %s, changed_by = %s,
                        reason = %s, etag = %s
                    WHERE app_id = %s AND capability_id = %s
                      AND capability_version = %s AND etag = %s
                    """,
                    (
                        binding.enabled,
                        binding.updated_at,
                        binding.changed_by,
                        binding.reason,
                        binding.etag,
                        binding.app_id,
                        binding.capability_id,
                        binding.capability_version,
                        expected_etag,
                    ),
                )
                if cursor.rowcount != 1:
                    raise RunStateConflict("capability binding etag mismatch")
        return binding

    async def list_capability_bindings(
        self,
        *,
        app_id: str,
        enabled_only: bool = False,
    ) -> list[ApplicationCapabilityBinding]:
        import psycopg

        enabled_clause = "AND enabled = true" if enabled_only else ""
        async with await psycopg.AsyncConnection.connect(self._dsn) as conn:
            cursor = await conn.execute(
                f"""
                SELECT app_id, capability_id, capability_version, enabled,
                       created_at, updated_at, changed_by, reason, etag
                FROM {self._schema}.application_capability_bindings
                WHERE app_id = %s {enabled_clause}
                ORDER BY capability_id, capability_version
                """,
                (app_id,),
            )
            rows = await cursor.fetchall()
        return [_binding_from_row(row) for row in rows]


def _application_from_row(row: object) -> AgentApplicationDefinition:
    values = list(row)  # type: ignore[arg-type]
    return AgentApplicationDefinition(
        app_id=str(values[0]),
        name=str(values[1]),
        default_agent_id=str(values[2]),
        identity_adapter_id=str(values[3]),
        status=str(values[4]),  # type: ignore[arg-type]
        description=str(values[5]),
        created_at=values[6],
        updated_at=values[7],
        updated_by=str(values[8]) if len(values) > 8 else "system",
        last_reason=str(values[9]) if len(values) > 9 else "",
        etag=int(values[10]) if len(values) > 10 else 1,
    )


def _binding_from_row(row: object) -> ApplicationCapabilityBinding:
    values = list(row)  # type: ignore[arg-type]
    return ApplicationCapabilityBinding(
        app_id=str(values[0]),
        capability_id=str(values[1]),
        capability_version=str(values[2]),
        enabled=bool(values[3]),
        created_at=values[4],
        updated_at=values[5] if len(values) > 5 else values[4],
        changed_by=str(values[6]) if len(values) > 6 else "system",
        reason=str(values[7]) if len(values) > 7 else "",
        etag=int(values[8]) if len(values) > 8 else 1,
    )


def _application_values(application: AgentApplicationDefinition) -> tuple[object, ...]:
    return (
        application.app_id,
        application.name,
        application.default_agent_id,
        application.identity_adapter_id,
        application.status,
        application.description,
        application.created_at,
        application.updated_at,
        application.updated_by,
        application.last_reason,
        application.etag,
    )


def _binding_values(binding: ApplicationCapabilityBinding) -> tuple[object, ...]:
    return (
        binding.app_id,
        binding.capability_id,
        binding.capability_version,
        binding.enabled,
        binding.created_at,
        binding.updated_at,
        binding.changed_by,
        binding.reason,
        binding.etag,
    )


def default_application_registry(
    *,
    dsn: str | None,
    schema: str = "full_view_agent",
) -> InMemoryApplicationRegistry | PostgresApplicationRegistry:
    if dsn:
        return PostgresApplicationRegistry(dsn=dsn, schema=schema)
    applications = [
        AgentApplicationDefinition(
            app_id="full_information_view",
            name="全量信息视图",
            default_agent_id="governance_general_agent",
            identity_adapter_id="identity.legacy_geo",
        ),
        AgentApplicationDefinition(
            app_id="unified_address",
            name="统一地址平台",
            default_agent_id="unified_address_agent",
            identity_adapter_id="identity.unified_address",
            status="disabled",
            description="待身份适配器和首批地址能力就绪后启用",
        ),
    ]
    return InMemoryApplicationRegistry(
        applications=applications,
        bindings=[
            ApplicationCapabilityBinding(
                app_id="full_information_view",
                capability_id=capability_id,
                capability_version=capability_version,
            )
            for capability_id, capability_version in (
                ("governance.resolve_area", "1.0.0"),
                ("governance.query_population_metrics", "1.3.0"),
                ("governance.query_housing_metrics", "1.0.0"),
                ("governance.query_event_metrics", "1.0.0"),
                ("governance.query_enterprise_metrics", "1.0.0"),
                ("governance.get_governance_overview", "1.0.0"),
                ("governance.query_governance_power_metrics", "1.0.0"),
                ("governance.get_object_profile", "1.0.0"),
                ("knowledge.search", "1.0.0"),
            )
        ],
    )
