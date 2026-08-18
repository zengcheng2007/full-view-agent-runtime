"""P2-1 capability center repository layer.

InMemory for tests, Postgres for production.  Both share the
``CapabilityRepository`` Protocol so the management service stays agnostic.
"""

# pyright: reportArgumentType=false, reportCallIssue=false
# psycopg v3 type stubs are overly strict about SQL string parameters.

from __future__ import annotations

import asyncio
import json
from typing import Protocol

from full_view_agent.application.errors import RunStateConflict
from full_view_agent.domain.capability import (
    CapabilityBase,
    CapabilityLifecycleEvent,
    CapabilitySnapshot,
    CapabilityStatus,
    CapabilityType,
    Connector,
    ConnectorAuditEvent,
    ModelAuditEvent,
    ModelConfig,
    ModelConfigVersion,
    ModelTestRecord,
    SkillCapability,
    ToolCapability,
    WorkflowCapability,
)


class CapabilityRepository(Protocol):
    async def save_tool(self, tool: ToolCapability) -> ToolCapability: ...
    async def save_skill(self, skill: SkillCapability) -> SkillCapability: ...
    async def save_workflow(
        self, workflow: WorkflowCapability
    ) -> WorkflowCapability: ...
    async def get(
        self, capability_id: str, version: str
    ) -> CapabilityBase | None: ...
    async def list_capabilities(
        self,
        *,
        capability_type: CapabilityType | None = None,
        status: CapabilityStatus | None = None,
    ) -> list[CapabilityBase]: ...
    async def put_snapshot(self, snapshot: CapabilitySnapshot) -> None: ...
    async def get_active_snapshot(
        self, capability_id: str
    ) -> CapabilitySnapshot | None: ...
    async def deactivate_snapshots(self, capability_id: str) -> None: ...
    async def record_lifecycle_event(
        self, event: CapabilityLifecycleEvent
    ) -> None: ...
    async def list_lifecycle_events(
        self, capability_id: str
    ) -> list[CapabilityLifecycleEvent]: ...
    async def save_connector(self, connector: Connector) -> None: ...
    async def list_connectors(self, *, active_only: bool = True) -> list[Connector]: ...
    async def get_connector(self, connector_id: str) -> Connector | None: ...
    async def update_connector(
        self,
        connector: Connector,
        *,
        expected_etag: int,
        event: ConnectorAuditEvent,
    ) -> Connector: ...
    async def list_connector_audit_events(
        self, connector_id: str
    ) -> list[ConnectorAuditEvent]: ...


class ModelConfigRepository(Protocol):
    async def save(self, config: ModelConfig) -> None: ...
    async def get(self, config_id: str) -> ModelConfig | None: ...
    async def list_all(self) -> list[ModelConfig]: ...
    async def delete(self, config_id: str) -> None: ...
    async def disable_all(self) -> None: ...
    async def save_version(self, version: ModelConfigVersion) -> None: ...
    async def save_version_key_material(
        self, *, config_id: str, version: int, ciphertext: bytes, nonce: bytes
    ) -> None: ...
    async def load_version_key_material(
        self, *, config_id: str, version: int
    ) -> tuple[bytes, bytes] | None: ...
    async def list_versions(self, config_id: str) -> list[ModelConfigVersion]: ...
    async def save_test_record(self, record: ModelTestRecord) -> None: ...
    async def list_test_records(self, config_id: str) -> list[ModelTestRecord]: ...
    async def save_audit_event(self, event: ModelAuditEvent) -> None: ...
    async def list_audit_events(self, config_id: str) -> list[ModelAuditEvent]: ...


class InMemoryCapabilityRepository:
    """Dict-backed repository for tests and development."""

    def __init__(self) -> None:
        self._tools: dict[tuple[str, str], ToolCapability] = {}
        self._skills: dict[tuple[str, str], SkillCapability] = {}
        self._workflows: dict[tuple[str, str], WorkflowCapability] = {}
        self._snapshots: dict[str, CapabilitySnapshot] = {}
        self._events: list[CapabilityLifecycleEvent] = []
        self._connectors: dict[str, Connector] = {}
        self._connector_events: list[ConnectorAuditEvent] = []
        self._lock = asyncio.Lock()

    async def save_tool(self, tool: ToolCapability) -> ToolCapability:
        async with self._lock:
            key = (tool.capability_id, tool.version)
            existing = self._tools.get(key)
            if existing is not None and existing.etag != tool.etag - 1:
                raise RunStateConflict("concurrent modification detected")
            self._tools[key] = tool
            return tool

    async def save_skill(self, skill: SkillCapability) -> SkillCapability:
        async with self._lock:
            key = (skill.capability_id, skill.version)
            existing = self._skills.get(key)
            if existing is not None and existing.etag != skill.etag - 1:
                raise RunStateConflict("concurrent modification detected")
            self._skills[key] = skill
            return skill

    async def save_workflow(
        self, workflow: WorkflowCapability
    ) -> WorkflowCapability:
        async with self._lock:
            key = (workflow.capability_id, workflow.version)
            existing = self._workflows.get(key)
            if existing is not None and existing.etag != workflow.etag - 1:
                raise RunStateConflict("concurrent modification detected")
            self._workflows[key] = workflow
            return workflow

    async def get(
        self, capability_id: str, version: str
    ) -> CapabilityBase | None:
        key = (capability_id, version)
        return (
            self._tools.get(key)
            or self._skills.get(key)
            or self._workflows.get(key)
        )

    async def list_capabilities(
        self,
        *,
        capability_type: CapabilityType | None = None,
        status: CapabilityStatus | None = None,
    ) -> list[CapabilityBase]:
        all_items: list[CapabilityBase] = []
        all_items.extend(self._tools.values())
        all_items.extend(self._skills.values())
        all_items.extend(self._workflows.values())
        if capability_type is not None:
            all_items = [c for c in all_items if c.capability_type == capability_type]
        if status is not None:
            all_items = [c for c in all_items if c.status == status]
        return sorted(all_items, key=lambda c: (c.capability_id, c.version))

    async def put_snapshot(self, snapshot: CapabilitySnapshot) -> None:
        async with self._lock:
            for sid, existing in list(self._snapshots.items()):
                if (
                    existing.capability_id == snapshot.capability_id
                    and existing.is_active
                    and sid != snapshot.snapshot_id
                ):
                    existing = existing.model_copy(update={"is_active": False})
                    self._snapshots[sid] = existing
            self._snapshots[snapshot.snapshot_id] = snapshot

    async def get_active_snapshot(
        self, capability_id: str
    ) -> CapabilitySnapshot | None:
        for snapshot in self._snapshots.values():
            if (
                snapshot.capability_id == capability_id
                and snapshot.is_active
            ):
                return snapshot
        return None

    async def deactivate_snapshots(self, capability_id: str) -> None:
        async with self._lock:
            for sid, snapshot in self._snapshots.items():
                if snapshot.capability_id == capability_id and snapshot.is_active:
                    self._snapshots[sid] = snapshot.model_copy(
                        update={"is_active": False}
                    )

    async def record_lifecycle_event(
        self, event: CapabilityLifecycleEvent
    ) -> None:
        async with self._lock:
            self._events.append(event)

    async def list_lifecycle_events(
        self, capability_id: str
    ) -> list[CapabilityLifecycleEvent]:
        return [
            e
            for e in self._events
            if e.capability_id == capability_id
        ]

    async def save_connector(self, connector: Connector) -> None:
        async with self._lock:
            if connector.connector_id in self._connectors:
                raise RunStateConflict("connector already exists")
            self._connectors[connector.connector_id] = connector

    async def list_connectors(
        self, *, active_only: bool = True
    ) -> list[Connector]:
        connectors = list(self._connectors.values())
        if active_only:
            connectors = [c for c in connectors if c.is_active]
        return sorted(connectors, key=lambda c: c.name)

    async def get_connector(self, connector_id: str) -> Connector | None:
        return self._connectors.get(connector_id)

    async def update_connector(
        self,
        connector: Connector,
        *,
        expected_etag: int,
        event: ConnectorAuditEvent,
    ) -> Connector:
        async with self._lock:
            existing = self._connectors.get(connector.connector_id)
            if existing is None:
                raise KeyError(connector.connector_id)
            if existing.etag != expected_etag:
                raise RunStateConflict("connector etag conflict")
            self._connectors[connector.connector_id] = connector
            self._connector_events.append(event)
            return connector

    async def list_connector_audit_events(
        self, connector_id: str
    ) -> list[ConnectorAuditEvent]:
        return [
            event
            for event in self._connector_events
            if event.connector_id == connector_id
        ]


class InMemoryModelConfigRepository:
    """Dict-backed model config repository for tests."""

    def __init__(self) -> None:
        self._configs: dict[str, ModelConfig] = {}
        self._versions: dict[tuple[str, int], ModelConfigVersion] = {}
        self._version_key_material: dict[tuple[str, int], tuple[bytes, bytes]] = {}
        self._test_records: list[ModelTestRecord] = []
        self._audit_events: list[ModelAuditEvent] = []
        self._lock = asyncio.Lock()

    async def save(self, config: ModelConfig) -> None:
        async with self._lock:
            self._configs[config.config_id] = config

    async def get(self, config_id: str) -> ModelConfig | None:
        return self._configs.get(config_id)

    async def list_all(self) -> list[ModelConfig]:
        return sorted(self._configs.values(), key=lambda c: c.created_at)

    async def delete(self, config_id: str) -> None:
        async with self._lock:
            self._configs.pop(config_id, None)

    async def disable_all(self) -> None:
        async with self._lock:
            for cid, config in self._configs.items():
                if config.is_enabled:
                    self._configs[cid] = config.model_copy(
                        update={"is_enabled": False}
                    )

    async def save_version(self, version: ModelConfigVersion) -> None:
        async with self._lock:
            key = (version.config_id, version.version)
            existing = self._versions.get(key)
            if (
                existing is not None
                and existing.lifecycle not in {"draft", "tested"}
                and existing != version
            ):
                raise RunStateConflict("model version is immutable")
            self._versions[key] = version

    async def save_version_key_material(
        self, *, config_id: str, version: int, ciphertext: bytes, nonce: bytes
    ) -> None:
        async with self._lock:
            self._version_key_material.setdefault(
                (config_id, version), (ciphertext, nonce)
            )

    async def load_version_key_material(
        self, *, config_id: str, version: int
    ) -> tuple[bytes, bytes] | None:
        return self._version_key_material.get((config_id, version))

    async def list_versions(self, config_id: str) -> list[ModelConfigVersion]:
        return sorted(
            (item for key, item in self._versions.items() if key[0] == config_id),
            key=lambda item: item.version,
        )

    async def save_test_record(self, record: ModelTestRecord) -> None:
        async with self._lock:
            self._test_records.append(record)

    async def list_test_records(self, config_id: str) -> list[ModelTestRecord]:
        return [item for item in self._test_records if item.config_id == config_id]

    async def save_audit_event(self, event: ModelAuditEvent) -> None:
        async with self._lock:
            self._audit_events.append(event)

    async def list_audit_events(self, config_id: str) -> list[ModelAuditEvent]:
        return [item for item in self._audit_events if item.config_id == config_id]


def _capability_to_json(capability: CapabilityBase) -> str:
    return capability.model_dump_json()


def _tool_from_json(data: dict[str, object]) -> ToolCapability:
    return ToolCapability.model_validate(data)


def _skill_from_json(data: dict[str, object]) -> SkillCapability:
    return SkillCapability.model_validate(data)


def _workflow_from_json(data: dict[str, object]) -> WorkflowCapability:
    return WorkflowCapability.model_validate(data)


class PostgresCapabilityRepository:
    """PostgreSQL-backed capability repository for production."""

    def __init__(
        self,
        *,
        dsn: str,
        schema: str = "full_view_agent",
    ) -> None:
        self._dsn = dsn
        self._schema = schema

    async def save_tool(self, tool: ToolCapability) -> ToolCapability:
        import psycopg

        async with await psycopg.AsyncConnection.connect(self._dsn) as conn:
            await conn.execute(
                f"""
                INSERT INTO {self._schema}.capability_tools (
                    capability_id, name, domain, owner, version, status,
                    risk_level, required_permissions, dataset_ids, description,
                    connector_ref, http_method, resource_path,
                    input_schema, output_schema, parameter_mapping, result_mapping,
                    result_kind, data_schema_ref, timeout_ms, max_attempts,
                    max_result_rows, cache_enabled, cache_ttl_seconds,
                    credential_ref, created_by, updated_by, etag, semantic_contract,
                    created_at, updated_at
                ) VALUES (
                    %(capability_id)s, %(name)s, %(domain)s, %(owner)s,
                    %(version)s, %(status)s, %(risk_level)s,
                    %(required_permissions)s, %(dataset_ids)s, %(description)s,
                    %(connector_ref)s, %(http_method)s, %(resource_path)s,
                    %(input_schema)s, %(output_schema)s, %(parameter_mapping)s,
                    %(result_mapping)s, %(result_kind)s, %(data_schema_ref)s,
                    %(timeout_ms)s, %(max_attempts)s, %(max_result_rows)s,
                    %(cache_enabled)s, %(cache_ttl_seconds)s, %(credential_ref)s,
                    %(created_by)s, %(updated_by)s, %(etag)s, %(semantic_contract)s,
                    %(created_at)s, %(updated_at)s
                )
                ON CONFLICT (capability_id, version) DO UPDATE SET
                    name = EXCLUDED.name, status = EXCLUDED.status,
                    description = EXCLUDED.description,
                    semantic_contract = EXCLUDED.semantic_contract,
                    updated_by = EXCLUDED.updated_by, etag = EXCLUDED.etag,
                    updated_at = EXCLUDED.updated_at
                """,
                _tool_params(tool),
            )
        return tool

    async def save_skill(self, skill: SkillCapability) -> SkillCapability:
        import psycopg

        async with await psycopg.AsyncConnection.connect(self._dsn) as conn:
            await conn.execute(
                f"""
                INSERT INTO {self._schema}.capability_skills (
                    capability_id, name, domain, owner, version, status,
                    risk_level, required_permissions, dataset_ids, description,
                    applicable_questions, guidance, allowed_tool_ids,
                    input_constraints, output_constraints,
                    examples, counter_examples,
                    created_by, updated_by, etag,
                    created_at, updated_at
                ) VALUES (
                    %(capability_id)s, %(name)s, %(domain)s, %(owner)s,
                    %(version)s, %(status)s, %(risk_level)s,
                    %(required_permissions)s, %(dataset_ids)s, %(description)s,
                    %(applicable_questions)s, %(guidance)s, %(allowed_tool_ids)s,
                    %(input_constraints)s, %(output_constraints)s,
                    %(examples)s, %(counter_examples)s,
                    %(created_by)s, %(updated_by)s, %(etag)s,
                    %(created_at)s, %(updated_at)s
                )
                ON CONFLICT (capability_id, version) DO UPDATE SET
                    name = EXCLUDED.name, status = EXCLUDED.status,
                    updated_by = EXCLUDED.updated_by, etag = EXCLUDED.etag,
                    updated_at = EXCLUDED.updated_at
                """,
                _skill_params(skill),
            )
        return skill

    async def save_workflow(
        self, workflow: WorkflowCapability
    ) -> WorkflowCapability:
        import psycopg

        async with await psycopg.AsyncConnection.connect(self._dsn) as conn:
            await conn.execute(
                f"""
                INSERT INTO {self._schema}.capability_workflows (
                    capability_id, name, domain, owner, version, status,
                    risk_level, required_permissions, dataset_ids, description,
                    nodes, edges, timeout_seconds, requires_human_confirmation,
                    created_by, updated_by, etag,
                    created_at, updated_at
                ) VALUES (
                    %(capability_id)s, %(name)s, %(domain)s, %(owner)s,
                    %(version)s, %(status)s, %(risk_level)s,
                    %(required_permissions)s, %(dataset_ids)s, %(description)s,
                    %(nodes)s, %(edges)s, %(timeout_seconds)s,
                    %(requires_human_confirmation)s,
                    %(created_by)s, %(updated_by)s, %(etag)s,
                    %(created_at)s, %(updated_at)s
                )
                ON CONFLICT (capability_id, version) DO UPDATE SET
                    name = EXCLUDED.name, status = EXCLUDED.status,
                    nodes = EXCLUDED.nodes, edges = EXCLUDED.edges,
                    updated_by = EXCLUDED.updated_by, etag = EXCLUDED.etag,
                    updated_at = EXCLUDED.updated_at
                """,
                _workflow_params(workflow),
            )
        return workflow

    async def get(
        self, capability_id: str, version: str
    ) -> CapabilityBase | None:
        import psycopg

        async with await psycopg.AsyncConnection.connect(self._dsn) as conn:
            for table, parser in (
                (f"{self._schema}.capability_tools", _tool_from_row),
                (f"{self._schema}.capability_skills", _skill_from_row),
                (f"{self._schema}.capability_workflows", _workflow_from_row),
            ):
                cursor = await conn.execute(
                    f"SELECT * FROM {table} WHERE capability_id = %s AND version = %s",
                    (capability_id, version),
                )
                result = await cursor.fetchone()
                if result is not None:
                    return parser(result)
        return None

    async def list_capabilities(
        self,
        *,
        capability_type: CapabilityType | None = None,
        status: CapabilityStatus | None = None,
    ) -> list[CapabilityBase]:
        import psycopg

        results: list[CapabilityBase] = []
        async with await psycopg.AsyncConnection.connect(self._dsn) as conn:
            if capability_type is None or capability_type == "tool":
                cursor = await conn.execute(
                    f"SELECT * FROM {self._schema}.capability_tools"
                    + _where_clause(status)
                    + " ORDER BY capability_id, version"
                )
                rows = await cursor.fetchall()
                results.extend(_tool_from_row(r) for r in rows)
            if capability_type is None or capability_type == "skill":
                cursor = await conn.execute(
                    f"SELECT * FROM {self._schema}.capability_skills"
                    + _where_clause(status)
                    + " ORDER BY capability_id, version"
                )
                rows = await cursor.fetchall()
                results.extend(_skill_from_row(r) for r in rows)
            if capability_type is None or capability_type == "workflow":
                cursor = await conn.execute(
                    f"SELECT * FROM {self._schema}.capability_workflows"
                    + _where_clause(status)
                    + " ORDER BY capability_id, version"
                )
                rows = await cursor.fetchall()
                results.extend(_workflow_from_row(r) for r in rows)
        return sorted(results, key=lambda c: (c.capability_id, c.version))

    async def put_snapshot(self, snapshot: CapabilitySnapshot) -> None:
        import json as _json

        import psycopg

        async with await psycopg.AsyncConnection.connect(self._dsn) as conn:
            await conn.execute(
                f"UPDATE {self._schema}.capability_snapshots"
                f" SET is_active = false"
                f" WHERE capability_id = %(capability_id)s AND is_active = true",
                {"capability_id": snapshot.capability_id},
            )
            await conn.execute(
                f"""
                INSERT INTO {self._schema}.capability_snapshots (
                    snapshot_id, capability_id, capability_type, version,
                    published_at, published_by, content, is_active
                ) VALUES (
                    %(snapshot_id)s, %(capability_id)s, %(capability_type)s,
                    %(version)s, %(published_at)s, %(published_by)s,
                    %(content)s, %(is_active)s
                )
                """,
                {
                    "snapshot_id": snapshot.snapshot_id,
                    "capability_id": snapshot.capability_id,
                    "capability_type": snapshot.capability_type,
                    "version": snapshot.version,
                    "published_at": snapshot.published_at,
                    "published_by": snapshot.published_by,
                    "content": _json.dumps(snapshot.content),
                    "is_active": snapshot.is_active,
                },
            )

    async def get_active_snapshot(
        self, capability_id: str
    ) -> CapabilitySnapshot | None:
        import psycopg

        async with await psycopg.AsyncConnection.connect(self._dsn) as conn:
            cursor = await conn.execute(
                f"""
                SELECT snapshot_id, capability_id, capability_type, version,
                       published_at, published_by, content, is_active
                  FROM {self._schema}.capability_snapshots
                 WHERE capability_id = %s AND is_active = true
                """,
                (capability_id,),
            )
            row = await cursor.fetchone()
        if row is None:
            return None
        return _snapshot_from_row(row)

    async def deactivate_snapshots(self, capability_id: str) -> None:
        import psycopg

        async with await psycopg.AsyncConnection.connect(self._dsn) as conn:
            await conn.execute(
                f"UPDATE {self._schema}.capability_snapshots"
                f" SET is_active = false"
                f" WHERE capability_id = %s AND is_active = true",
                (capability_id,),
            )

    async def record_lifecycle_event(
        self, event: CapabilityLifecycleEvent
    ) -> None:
        import psycopg

        async with await psycopg.AsyncConnection.connect(self._dsn) as conn:
            await conn.execute(
                f"""
                INSERT INTO {self._schema}.capability_lifecycle_events (
                    event_id, capability_id, from_status, to_status,
                    version, changed_by, changed_at, reason
                ) VALUES (
                    %(event_id)s, %(capability_id)s, %(from_status)s,
                    %(to_status)s, %(version)s, %(changed_by)s,
                    %(changed_at)s, %(reason)s
                )
                """,
                {
                    "event_id": event.event_id,
                    "capability_id": event.capability_id,
                    "from_status": event.from_status,
                    "to_status": event.to_status,
                    "version": event.version,
                    "changed_by": event.changed_by,
                    "changed_at": event.changed_at,
                    "reason": event.reason,
                },
            )

    async def list_lifecycle_events(
        self, capability_id: str
    ) -> list[CapabilityLifecycleEvent]:
        import psycopg

        async with await psycopg.AsyncConnection.connect(self._dsn) as conn:
            cursor = await conn.execute(
                f"""
                SELECT event_id, capability_id, from_status, to_status,
                       version, changed_by, changed_at, reason
                  FROM {self._schema}.capability_lifecycle_events
                 WHERE capability_id = %s
                 ORDER BY changed_at DESC
                """,
                (capability_id,),
            )
            rows = await cursor.fetchall()
        return [
            CapabilityLifecycleEvent(
                event_id=r[0],
                capability_id=r[1],
                from_status=r[2],
                to_status=r[3],
                version=r[4],
                changed_by=r[5],
                changed_at=r[6],
                reason=r[7],
            )
            for r in rows
        ]

    async def save_connector(self, connector: Connector) -> None:
        import psycopg

        async with await psycopg.AsyncConnection.connect(self._dsn) as conn:
            cursor = await conn.execute(
                f"""
                INSERT INTO {self._schema}.connectors (
                    connector_id, name, base_url, description,
                    allowed_path_prefixes, denied_hosts, is_active,
                    credential_ref, timeout_ms, created_by, updated_by, etag,
                    created_at, updated_at
                ) VALUES (
                    %(connector_id)s, %(name)s, %(base_url)s, %(description)s,
                    %(allowed_path_prefixes)s, %(denied_hosts)s, %(is_active)s,
                    %(credential_ref)s, %(timeout_ms)s, %(created_by)s,
                    %(updated_by)s, %(etag)s, %(created_at)s, %(updated_at)s
                )
                ON CONFLICT (connector_id) DO NOTHING
                RETURNING connector_id
                """,
                {
                    "connector_id": connector.connector_id,
                    "name": connector.name,
                    "base_url": connector.base_url,
                    "description": connector.description,
                    "allowed_path_prefixes": connector.allowed_path_prefixes,
                    "denied_hosts": connector.denied_hosts,
                    "is_active": connector.is_active,
                    "credential_ref": connector.credential_ref,
                    "timeout_ms": connector.timeout_ms,
                    "created_by": connector.created_by,
                    "updated_by": connector.updated_by,
                    "etag": connector.etag,
                    "created_at": connector.created_at,
                    "updated_at": connector.updated_at,
                },
            )
            if await cursor.fetchone() is None:
                raise RunStateConflict("connector already exists")

    async def list_connectors(
        self, *, active_only: bool = True
    ) -> list[Connector]:
        import psycopg

        async with await psycopg.AsyncConnection.connect(self._dsn) as conn:
            query = (
                f"SELECT connector_id, name, base_url, description,"
                f"  allowed_path_prefixes, denied_hosts, is_active,"
                f"  credential_ref, timeout_ms, created_by, updated_by, etag,"
                f"  created_at, updated_at"
                f" FROM {self._schema}.connectors"
            )
            if active_only:
                query += " WHERE is_active = true"
            query += " ORDER BY name"
            cursor = await conn.execute(query)
            rows = await cursor.fetchall()
        return [
            Connector(
                connector_id=r[0],
                name=r[1],
                base_url=r[2],
                description=r[3],
                allowed_path_prefixes=list(r[4] or []),
                denied_hosts=list(r[5] or []),
                is_active=r[6],
                credential_ref=r[7],
                timeout_ms=r[8],
                created_by=r[9],
                updated_by=r[10],
                etag=r[11],
                created_at=r[12],
                updated_at=r[13],
            )
            for r in rows
        ]

    async def get_connector(self, connector_id: str) -> Connector | None:
        import psycopg

        async with await psycopg.AsyncConnection.connect(self._dsn) as conn:
            cursor = await conn.execute(
                f"""
                SELECT connector_id, name, base_url, description,
                       allowed_path_prefixes, denied_hosts, is_active,
                       credential_ref, timeout_ms, created_by, updated_by, etag,
                       created_at, updated_at
                  FROM {self._schema}.connectors
                 WHERE connector_id = %s
                """,
                (connector_id,),
            )
            row = await cursor.fetchone()
        if row is None:
            return None
        return Connector(
            connector_id=row[0],
            name=row[1],
            base_url=row[2],
            description=row[3],
            allowed_path_prefixes=list(row[4] or []),
            denied_hosts=list(row[5] or []),
            is_active=row[6],
            credential_ref=row[7],
            timeout_ms=row[8],
            created_by=row[9],
            updated_by=row[10],
            etag=row[11],
            created_at=row[12],
            updated_at=row[13],
        )

    async def update_connector(
        self,
        connector: Connector,
        *,
        expected_etag: int,
        event: ConnectorAuditEvent,
    ) -> Connector:
        import psycopg

        async with await psycopg.AsyncConnection.connect(self._dsn) as conn:
            cursor = await conn.execute(
                f"""
                UPDATE {self._schema}.connectors SET
                    name=%(name)s, base_url=%(base_url)s,
                    description=%(description)s,
                    allowed_path_prefixes=%(allowed_path_prefixes)s,
                    denied_hosts=%(denied_hosts)s, is_active=%(is_active)s,
                    credential_ref=%(credential_ref)s, timeout_ms=%(timeout_ms)s,
                    updated_by=%(updated_by)s, etag=%(etag)s,
                    updated_at=%(updated_at)s
                WHERE connector_id=%(connector_id)s AND etag=%(expected_etag)s
                RETURNING connector_id
                """,
                {
                    **_connector_params(connector),
                    "expected_etag": expected_etag,
                },
            )
            if await cursor.fetchone() is None:
                exists = await conn.execute(
                    f"SELECT 1 FROM {self._schema}.connectors WHERE connector_id=%s",
                    (connector.connector_id,),
                )
                if await exists.fetchone() is None:
                    raise KeyError(connector.connector_id)
                raise RunStateConflict("connector etag conflict")
            await conn.execute(
                f"""
                INSERT INTO {self._schema}.connector_audit_events (
                    event_id, connector_id, action, actor, reason,
                    previous_etag, new_etag, changed_fields,
                    from_active, to_active, changed_at
                ) VALUES (
                    %(event_id)s, %(connector_id)s, %(action)s, %(actor)s,
                    %(reason)s, %(previous_etag)s, %(new_etag)s,
                    %(changed_fields)s, %(from_active)s, %(to_active)s,
                    %(changed_at)s
                )
                """,
                event.model_dump(mode="python"),
            )
        return connector

    async def list_connector_audit_events(
        self, connector_id: str
    ) -> list[ConnectorAuditEvent]:
        import psycopg

        async with await psycopg.AsyncConnection.connect(self._dsn) as conn:
            cursor = await conn.execute(
                f"""
                SELECT event_id, connector_id, action, actor, reason,
                       previous_etag, new_etag, changed_fields,
                       from_active, to_active, changed_at
                FROM {self._schema}.connector_audit_events
                WHERE connector_id=%s ORDER BY changed_at, event_id
                """,
                (connector_id,),
            )
            rows = await cursor.fetchall()
        return [
            ConnectorAuditEvent(
                event_id=row[0], connector_id=row[1], action=row[2],
                actor=row[3], reason=row[4], previous_etag=row[5],
                new_etag=row[6], changed_fields=list(row[7] or []),
                from_active=row[8], to_active=row[9], changed_at=row[10],
            )
            for row in rows
        ]

class PostgresModelConfigRepository:
    """PostgreSQL-backed model config repository."""

    def __init__(
        self,
        *,
        dsn: str,
        schema: str = "full_view_agent",
    ) -> None:
        self._dsn = dsn
        self._schema = schema

    async def save(self, config: ModelConfig) -> None:
        import psycopg

        async with await psycopg.AsyncConnection.connect(self._dsn) as conn:
            await conn.execute(
                f"""
                INSERT INTO {self._schema}.model_configs (
                    config_id, name, api_base_url,
                    api_key_ciphertext, api_key_nonce,
                    model_name, protocol,
                    timeout_seconds, max_output_tokens, max_retries,
                    reasoning_capability,
                    provider_type, capabilities, parameter_profiles, lifecycle,
                    is_enabled, legacy_default, notes, created_at, updated_at,
                    created_by, updated_by, version, etag
                ) VALUES (
                    %(config_id)s, %(name)s, %(api_base_url)s,
                    ''::bytea, ''::bytea,
                    %(model_name)s,
                    %(protocol)s, %(timeout_seconds)s, %(max_output_tokens)s,
                    %(max_retries)s, %(reasoning_capability)s::jsonb,
                    %(provider_type)s, %(capabilities)s::jsonb,
                    %(parameter_profiles)s::jsonb, %(lifecycle)s,
                    %(is_enabled)s, %(legacy_default)s, %(notes)s,
                    %(created_at)s, %(updated_at)s, %(created_by)s,
                    %(updated_by)s, %(version)s, %(etag)s
                )
                ON CONFLICT (config_id) DO UPDATE SET
                    name = EXCLUDED.name,
                    api_base_url = EXCLUDED.api_base_url,
                    model_name = EXCLUDED.model_name,
                    protocol = EXCLUDED.protocol,
                    timeout_seconds = EXCLUDED.timeout_seconds,
                    max_output_tokens = EXCLUDED.max_output_tokens,
                    max_retries = EXCLUDED.max_retries,
                    reasoning_capability = EXCLUDED.reasoning_capability,
                    provider_type = EXCLUDED.provider_type,
                    capabilities = EXCLUDED.capabilities,
                    parameter_profiles = EXCLUDED.parameter_profiles,
                    lifecycle = EXCLUDED.lifecycle,
                    is_enabled = EXCLUDED.is_enabled,
                    legacy_default = EXCLUDED.legacy_default,
                    notes = EXCLUDED.notes,
                    updated_at = EXCLUDED.updated_at,
                    updated_by = EXCLUDED.updated_by,
                    version = EXCLUDED.version,
                    etag = EXCLUDED.etag
                """,
                {
                    "config_id": config.config_id,
                    "name": config.name,
                    "api_base_url": config.api_base_url,
                    "model_name": config.model_name,
                    "protocol": config.protocol,
                    "timeout_seconds": config.timeout_seconds,
                    "max_output_tokens": config.max_output_tokens,
                    "max_retries": config.max_retries,
                    "reasoning_capability": config.reasoning_capability.model_dump_json(),
                    "provider_type": config.provider_type,
                    "capabilities": config.capabilities.model_dump_json(),
                    "parameter_profiles": config.parameter_profiles.model_dump_json(),
                    "lifecycle": config.lifecycle,
                    "is_enabled": config.is_enabled,
                    "legacy_default": config.legacy_default,
                    "notes": config.notes,
                    "created_at": config.created_at,
                    "updated_at": config.updated_at,
                    "created_by": config.created_by,
                    "updated_by": config.updated_by,
                    "version": config.version,
                    "etag": config.etag,
                },
            )

    async def get(self, config_id: str) -> ModelConfig | None:
        import psycopg

        async with await psycopg.AsyncConnection.connect(self._dsn) as conn:
            cursor = await conn.execute(
                f"""
                SELECT config_id, name, api_base_url, model_name, protocol,
                       timeout_seconds, max_output_tokens, max_retries,
                       reasoning_capability,
                       provider_type, capabilities, parameter_profiles, lifecycle,
                       is_enabled, legacy_default, notes, created_at, updated_at,
                       created_by, updated_by, version, etag
                  FROM {self._schema}.model_configs
                 WHERE config_id = %s
                """,
                (config_id,),
            )
            row = await cursor.fetchone()
        if row is None:
            return None
        return _model_config_from_row(row)

    async def list_all(self) -> list[ModelConfig]:
        import psycopg

        async with await psycopg.AsyncConnection.connect(self._dsn) as conn:
            cursor = await conn.execute(
                f"""
                SELECT config_id, name, api_base_url, model_name, protocol,
                       timeout_seconds, max_output_tokens, max_retries,
                       reasoning_capability,
                       provider_type, capabilities, parameter_profiles, lifecycle,
                       is_enabled, legacy_default, notes, created_at, updated_at,
                       created_by, updated_by, version, etag
                  FROM {self._schema}.model_configs
                 ORDER BY created_at
                """
            )
            rows = await cursor.fetchall()
        return [_model_config_from_row(r) for r in rows]

    async def delete(self, config_id: str) -> None:
        import psycopg

        async with await psycopg.AsyncConnection.connect(self._dsn) as conn:
            await conn.execute(
                f"DELETE FROM {self._schema}.model_configs WHERE config_id = %s",
                (config_id,),
            )

    async def disable_all(self) -> None:
        import psycopg

        async with await psycopg.AsyncConnection.connect(self._dsn) as conn:
            await conn.execute(
                f"UPDATE {self._schema}.model_configs SET is_enabled = false"
                f" WHERE is_enabled = true"
            )

    async def save_version(self, version: ModelConfigVersion) -> None:
        import psycopg

        async with await psycopg.AsyncConnection.connect(self._dsn) as conn:
            cursor = await conn.execute(
                f"""
                INSERT INTO {self._schema}.model_config_versions (
                    config_id, version, lifecycle, config,
                    api_key_ciphertext, api_key_nonce, created_by, created_at
                )
                SELECT %s, %s, %s, %s::jsonb,
                       api_key_ciphertext, api_key_nonce, %s, %s
                  FROM {self._schema}.model_configs WHERE config_id = %s
                ON CONFLICT (config_id, version) DO UPDATE SET
                    lifecycle = EXCLUDED.lifecycle,
                    config = EXCLUDED.config,
                    api_key_ciphertext = EXCLUDED.api_key_ciphertext,
                    api_key_nonce = EXCLUDED.api_key_nonce,
                    created_by = EXCLUDED.created_by,
                    created_at = EXCLUDED.created_at
                WHERE {self._schema}.model_config_versions.lifecycle IN ('draft', 'tested')
                """,
                (
                    version.config_id,
                    version.version,
                    version.lifecycle,
                    version.config.model_dump_json(),
                    version.created_by,
                    version.created_at,
                    version.config_id,
                ),
            )
            if cursor.rowcount == 0:
                raise RunStateConflict("model version is immutable")

    async def list_versions(self, config_id: str) -> list[ModelConfigVersion]:
        import psycopg

        async with await psycopg.AsyncConnection.connect(self._dsn) as conn:
            cursor = await conn.execute(
                f"SELECT config_id, version, lifecycle, config, created_by, created_at "
                f"FROM {self._schema}.model_config_versions "
                "WHERE config_id=%s ORDER BY version",
                (config_id,),
            )
            rows = await cursor.fetchall()
        return [
            ModelConfigVersion(
                config_id=str(row[0]), version=int(row[1]), lifecycle=str(row[2]),
                config=_json_field(row[3], {}), created_by=str(row[4]), created_at=row[5],
            )
            for row in rows
        ]

    async def save_version_key_material(
        self, *, config_id: str, version: int, ciphertext: bytes, nonce: bytes
    ) -> None:
        import psycopg

        async with await psycopg.AsyncConnection.connect(self._dsn) as conn:
            await conn.execute(
                f"UPDATE {self._schema}.model_config_versions "
                "SET api_key_ciphertext=%s, api_key_nonce=%s "
                "WHERE config_id=%s AND version=%s",
                (ciphertext, nonce, config_id, version),
            )

    async def load_version_key_material(
        self, *, config_id: str, version: int
    ) -> tuple[bytes, bytes] | None:
        import psycopg

        async with await psycopg.AsyncConnection.connect(self._dsn) as conn:
            cursor = await conn.execute(
                f"SELECT api_key_ciphertext, api_key_nonce "
                f"FROM {self._schema}.model_config_versions "
                "WHERE config_id=%s AND version=%s",
                (config_id, version),
            )
            row = await cursor.fetchone()
        if row is None:
            return None
        return bytes(row[0]), bytes(row[1])

    async def save_test_record(self, record: ModelTestRecord) -> None:
        import psycopg

        async with await psycopg.AsyncConnection.connect(self._dsn) as conn:
            await conn.execute(
                f"""INSERT INTO {self._schema}.model_test_records (
                    test_id, config_id, version, kind, profile, success, latency_ms,
                    actual_parameters, prompt_tokens, completion_tokens,
                    reasoning_tokens, error_code, error_message, tested_by, tested_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s,%s,%s)""",
                (
                    record.test_id, record.config_id, record.version, record.kind,
                    record.profile, record.success, record.latency_ms,
                    json.dumps(record.actual_parameters), record.prompt_tokens,
                    record.completion_tokens, record.reasoning_tokens,
                    record.error_code, record.error_message, record.tested_by,
                    record.tested_at,
                ),
            )

    async def list_test_records(self, config_id: str) -> list[ModelTestRecord]:
        import psycopg

        async with await psycopg.AsyncConnection.connect(self._dsn) as conn:
            cursor = await conn.execute(
                f"""SELECT test_id, config_id, version, kind, success, latency_ms,
                    profile,
                    actual_parameters, prompt_tokens, completion_tokens,
                    reasoning_tokens, error_code, error_message, tested_by, tested_at
                    FROM {self._schema}.model_test_records
                    WHERE config_id=%s ORDER BY tested_at DESC""",
                (config_id,),
            )
            rows = await cursor.fetchall()
        return [
            ModelTestRecord(
                test_id=str(r[0]), config_id=str(r[1]), version=int(r[2]), kind=str(r[3]),
                success=bool(r[4]), latency_ms=r[5], profile=r[6],
                actual_parameters=_json_field(r[7], {}),
                prompt_tokens=r[8], completion_tokens=r[9], reasoning_tokens=r[10],
                error_code=r[11], error_message=r[12], tested_by=str(r[13]),
                tested_at=r[14],
            )
            for r in rows
        ]

    async def save_audit_event(self, event: ModelAuditEvent) -> None:
        import psycopg

        async with await psycopg.AsyncConnection.connect(self._dsn) as conn:
            await conn.execute(
                f"""INSERT INTO {self._schema}.model_audit_events (
                    event_id, config_id, version, action, actor, reason,
                    previous_etag, new_etag, changed_fields, changed_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s)""",
                (
                    event.event_id, event.config_id, event.version, event.action,
                    event.actor, event.reason, event.previous_etag, event.new_etag,
                    json.dumps(event.changed_fields), event.changed_at,
                ),
            )

    async def list_audit_events(self, config_id: str) -> list[ModelAuditEvent]:
        import psycopg

        async with await psycopg.AsyncConnection.connect(self._dsn) as conn:
            cursor = await conn.execute(
                f"""SELECT event_id, config_id, version, action, actor, reason,
                    previous_etag, new_etag, changed_fields, changed_at
                    FROM {self._schema}.model_audit_events
                    WHERE config_id=%s ORDER BY changed_at""",
                (config_id,),
            )
            rows = await cursor.fetchall()
        return [
            ModelAuditEvent(
                event_id=str(r[0]), config_id=str(r[1]), version=int(r[2]),
                action=str(r[3]), actor=str(r[4]), reason=str(r[5]),
                previous_etag=r[6], new_etag=int(r[7]),
                changed_fields=list(_json_field(r[8], [])), changed_at=r[9],
            )
            for r in rows
        ]


# ---- Row mapping helpers ----


def _json_field(value: object, default: object = None) -> object:
    """Parse a JSON field from a psycopg row value."""
    if value is None:
        return default
    if isinstance(value, str):
        return json.loads(value)
    return value


def _str_field(value: object, default: str = "") -> str:
    return str(value) if value is not None else default


def _where_clause(status: CapabilityStatus | None) -> str:
    if status is None:
        return ""
    return f" WHERE status = '{status}'"


def _tool_params(tool: ToolCapability) -> dict[str, object]:
    return {
        "capability_id": tool.capability_id,
        "name": tool.name,
        "domain": tool.domain,
        "owner": tool.owner,
        "version": tool.version,
        "status": tool.status,
        "risk_level": tool.risk_level,
        "required_permissions": tool.required_permissions,
        "dataset_ids": tool.dataset_ids,
        "description": tool.description,
        "connector_ref": tool.connector_ref,
        "http_method": tool.http_method,
        "resource_path": tool.resource_path,
        "input_schema": json.dumps(tool.input_schema),
        "output_schema": json.dumps(tool.output_schema),
        "parameter_mapping": json.dumps(tool.parameter_mapping),
        "result_mapping": json.dumps(tool.result_mapping),
        "result_kind": tool.result_kind,
        "data_schema_ref": tool.data_schema_ref,
        "timeout_ms": tool.timeout_ms,
        "max_attempts": tool.max_attempts,
        "max_result_rows": tool.max_result_rows,
        "cache_enabled": tool.cache_enabled,
        "cache_ttl_seconds": tool.cache_ttl_seconds,
        "credential_ref": tool.credential_ref,
        "semantic_contract": (
            json.dumps(tool.semantic_contract.model_dump(mode="json"))
            if tool.semantic_contract is not None
            else None
        ),
        "created_by": tool.created_by,
        "updated_by": tool.updated_by,
        "etag": tool.etag,
        "created_at": tool.created_at,
        "updated_at": tool.updated_at,
    }


def _connector_params(connector: Connector) -> dict[str, object]:
    return {
        "connector_id": connector.connector_id,
        "name": connector.name,
        "base_url": connector.base_url,
        "description": connector.description,
        "allowed_path_prefixes": connector.allowed_path_prefixes,
        "denied_hosts": connector.denied_hosts,
        "is_active": connector.is_active,
        "credential_ref": connector.credential_ref,
        "timeout_ms": connector.timeout_ms,
        "created_by": connector.created_by,
        "updated_by": connector.updated_by,
        "etag": connector.etag,
        "created_at": connector.created_at,
        "updated_at": connector.updated_at,
    }


def _skill_params(skill: SkillCapability) -> dict[str, object]:
    return {
        "capability_id": skill.capability_id,
        "name": skill.name,
        "domain": skill.domain,
        "owner": skill.owner,
        "version": skill.version,
        "status": skill.status,
        "risk_level": skill.risk_level,
        "required_permissions": skill.required_permissions,
        "dataset_ids": skill.dataset_ids,
        "description": skill.description,
        "applicable_questions": skill.applicable_questions,
        "guidance": skill.guidance,
        "allowed_tool_ids": skill.allowed_tool_ids,
        "input_constraints": json.dumps(skill.input_constraints),
        "output_constraints": json.dumps(skill.output_constraints),
        "examples": json.dumps(skill.examples),
        "counter_examples": json.dumps(skill.counter_examples),
        "created_by": skill.created_by,
        "updated_by": skill.updated_by,
        "etag": skill.etag,
        "created_at": skill.created_at,
        "updated_at": skill.updated_at,
    }


def _workflow_params(wf: WorkflowCapability) -> dict[str, object]:
    return {
        "capability_id": wf.capability_id,
        "name": wf.name,
        "domain": wf.domain,
        "owner": wf.owner,
        "version": wf.version,
        "status": wf.status,
        "risk_level": wf.risk_level,
        "required_permissions": wf.required_permissions,
        "dataset_ids": wf.dataset_ids,
        "description": wf.description,
        "nodes": json.dumps([n.model_dump(mode="json") for n in wf.nodes]),
        "edges": json.dumps([e.model_dump(mode="json") for e in wf.edges]),
        "timeout_seconds": wf.timeout_seconds,
        "requires_human_confirmation": wf.requires_human_confirmation,
        "created_by": wf.created_by,
        "updated_by": wf.updated_by,
        "etag": wf.etag,
        "created_at": wf.created_at,
        "updated_at": wf.updated_at,
    }


def _tool_from_row(row: tuple[object, ...] | list[object]) -> ToolCapability:
    r = list(row)
    return ToolCapability(
        capability_id=_str_field(r[0]),
        name=_str_field(r[1]),
        domain=_str_field(r[2]),
        owner=_str_field(r[3]),
        version=_str_field(r[4]),
        status=_str_field(r[5]),
        risk_level=_str_field(r[6]),
        required_permissions=list(r[7] or []),  # type: ignore[arg-type]
        dataset_ids=list(r[8] or []),  # type: ignore[arg-type]
        description=_str_field(r[9]),
        connector_ref=_str_field(r[10]),
        http_method=_str_field(r[11]),
        resource_path=_str_field(r[12]),
        input_schema=_json_field(r[13], {}),  # type: ignore[return-value]
        output_schema=_json_field(r[14], {}),  # type: ignore[return-value]
        parameter_mapping=_json_field(r[15], {}),  # type: ignore[return-value]
        result_mapping=_json_field(r[16], {}),  # type: ignore[return-value]
        result_kind=_str_field(r[17]),
        data_schema_ref=_str_field(r[18]),
        timeout_ms=int(r[19]),  # type: ignore[arg-type]
        max_attempts=int(r[20]),  # type: ignore[arg-type]
        max_result_rows=int(r[21]),  # type: ignore[arg-type]
        cache_enabled=bool(r[22]),
        cache_ttl_seconds=int(r[23]),  # type: ignore[arg-type]
        credential_ref=_str_field(r[24]) or None,
        created_at=r[25],  # type: ignore[arg-type]
        updated_at=r[26],  # type: ignore[arg-type]
        created_by=_str_field(r[27]) if len(r) > 27 else "system",
        updated_by=_str_field(r[28]) if len(r) > 28 else "system",
        etag=int(r[29]) if len(r) > 29 else 1,  # type: ignore[arg-type]
        semantic_contract=(
            _json_field(r[30], None) if len(r) > 30 else None
        ),
    )


def _skill_from_row(row: tuple[object, ...] | list[object]) -> SkillCapability:
    r = list(row)
    return SkillCapability(
        capability_id=_str_field(r[0]),
        name=_str_field(r[1]),
        domain=_str_field(r[2]),
        owner=_str_field(r[3]),
        version=_str_field(r[4]),
        status=_str_field(r[5]),
        risk_level=_str_field(r[6]),
        required_permissions=list(r[7] or []),  # type: ignore[arg-type]
        dataset_ids=list(r[8] or []),  # type: ignore[arg-type]
        description=_str_field(r[9]),
        applicable_questions=list(r[10] or []),  # type: ignore[arg-type]
        guidance=_str_field(r[11]),
        allowed_tool_ids=list(r[12] or []),  # type: ignore[arg-type]
        input_constraints=_json_field(r[13], {}),  # type: ignore[return-value]
        output_constraints=_json_field(r[14], {}),  # type: ignore[return-value]
        examples=_json_field(r[15], []),  # type: ignore[return-value]
        counter_examples=_json_field(r[16], []),  # type: ignore[return-value]
        created_at=r[17],  # type: ignore[arg-type]
        updated_at=r[18],  # type: ignore[arg-type]
        created_by=_str_field(r[19]) if len(r) > 19 else "system",
        updated_by=_str_field(r[20]) if len(r) > 20 else "system",
        etag=int(r[21]) if len(r) > 21 else 1,  # type: ignore[arg-type]
    )


def _workflow_from_row(row: tuple[object, ...] | list[object]) -> WorkflowCapability:
    r = list(row)
    return WorkflowCapability(
        capability_id=_str_field(r[0]),
        name=_str_field(r[1]),
        domain=_str_field(r[2]),
        owner=_str_field(r[3]),
        version=_str_field(r[4]),
        status=_str_field(r[5]),
        risk_level=_str_field(r[6]),
        required_permissions=list(r[7] or []),  # type: ignore[arg-type]
        dataset_ids=list(r[8] or []),  # type: ignore[arg-type]
        description=_str_field(r[9]),
        nodes=_json_field(r[10], []),  # type: ignore[return-value]
        edges=_json_field(r[11], []),  # type: ignore[return-value]
        timeout_seconds=int(r[12]),  # type: ignore[arg-type]
        requires_human_confirmation=bool(r[13]),
        created_at=r[14],  # type: ignore[arg-type]
        updated_at=r[15],  # type: ignore[arg-type]
        created_by=_str_field(r[16]) if len(r) > 16 else "system",
        updated_by=_str_field(r[17]) if len(r) > 17 else "system",
        etag=int(r[18]) if len(r) > 18 else 1,  # type: ignore[arg-type]
    )


def _snapshot_from_row(row: tuple[object, ...] | list[object]) -> CapabilitySnapshot:
    r = list(row)
    content = _json_field(r[6], {})
    return CapabilitySnapshot(
        snapshot_id=_str_field(r[0]),
        capability_id=_str_field(r[1]),
        capability_type=str(r[2]),  # type: ignore[arg-type]
        version=_str_field(r[3]),
        published_at=r[4],  # type: ignore[arg-type]
        published_by=_str_field(r[5]),
        content=content,  # type: ignore[arg-type]
        is_active=bool(r[7]),
    )


def _model_config_from_row(row: tuple[object, ...] | list[object]) -> ModelConfig:
    r = list(row)
    return ModelConfig(
        config_id=_str_field(r[0]),
        name=_str_field(r[1]),
        api_base_url=_str_field(r[2]),
        model_name=_str_field(r[3]),
        protocol=_str_field(r[4]),
        timeout_seconds=int(r[5]),  # type: ignore[arg-type]
        max_output_tokens=int(r[6]),  # type: ignore[arg-type]
        max_retries=int(r[7]),  # type: ignore[arg-type]
        reasoning_capability=_json_field(r[8], {}),  # type: ignore[arg-type]
        provider_type=_str_field(r[9], "openai_compatible"),
        capabilities=_json_field(r[10], {}),  # type: ignore[arg-type]
        parameter_profiles=_json_field(r[11], {}),  # type: ignore[arg-type]
        lifecycle=_str_field(r[12], "draft"),
        is_enabled=bool(r[13]),
        legacy_default=bool(r[14]),
        notes=_str_field(r[15]),
        created_at=r[16],  # type: ignore[arg-type]
        updated_at=r[17],  # type: ignore[arg-type]
        created_by=_str_field(r[18]),
        updated_by=_str_field(r[19], "system"),
        version=int(r[20]),  # type: ignore[arg-type]
        etag=int(r[21]),  # type: ignore[arg-type]
    )
