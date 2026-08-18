"""Persistence for application-scoped Agent definitions and releases."""

# pyright: reportArgumentType=false

from __future__ import annotations

import asyncio
import json

from full_view_agent.application.errors import RunStateConflict
from full_view_agent.domain.agent_definition import (
    AgentDefinition,
    AgentModelPolicy,
    AgentReleaseSnapshot,
    AgentVersion,
    RunAgentReleaseSnapshot,
)


class InMemoryAgentRepository:
    def __init__(self) -> None:
        self._agents: dict[tuple[str, str], AgentDefinition] = {}
        self._versions: dict[tuple[str, str, str], AgentVersion] = {}
        self._policies: dict[tuple[str, str, str], AgentModelPolicy] = {}
        self._releases: dict[tuple[str, str, str], AgentReleaseSnapshot] = {}
        self._active: dict[tuple[str, str], AgentReleaseSnapshot] = {}
        self._run_snapshots: dict[str, RunAgentReleaseSnapshot] = {}
        self._lock = asyncio.Lock()

    async def save_agent(self, agent: AgentDefinition) -> AgentDefinition:
        async with self._lock:
            key = (agent.app_id, agent.agent_id)
            if key in self._agents:
                raise RunStateConflict("agent is already registered")
            self._agents[key] = agent
        return agent

    async def get_agent(self, app_id: str, agent_id: str) -> AgentDefinition | None:
        return self._agents.get((app_id, agent_id))

    async def list_agents(self, app_id: str) -> list[AgentDefinition]:
        return sorted(
            (item for item in self._agents.values() if item.app_id == app_id),
            key=lambda item: item.agent_id,
        )

    async def save_version(self, version: AgentVersion) -> AgentVersion:
        key = (version.app_id, version.agent_id, version.version)
        async with self._lock:
            if key in self._versions:
                raise RunStateConflict("agent version already exists")
            self._versions[key] = version
        return version

    async def get_version(self, app_id: str, agent_id: str, version: str) -> AgentVersion | None:
        return self._versions.get((app_id, agent_id, version))

    async def list_versions(self, app_id: str, agent_id: str) -> list[AgentVersion]:
        return sorted(
            (
                item
                for item in self._versions.values()
                if item.app_id == app_id and item.agent_id == agent_id
            ),
            key=lambda item: item.version,
        )

    async def save_model_policy(
        self, app_id: str, agent_id: str, version: str, policy: AgentModelPolicy
    ) -> AgentModelPolicy:
        self._policies[(app_id, agent_id, version)] = policy
        return policy

    async def get_model_policy(
        self, app_id: str, agent_id: str, version: str
    ) -> AgentModelPolicy | None:
        return self._policies.get((app_id, agent_id, version))

    async def publish(
        self, version: AgentVersion, release: AgentReleaseSnapshot
    ) -> AgentReleaseSnapshot:
        key = (version.app_id, version.agent_id, version.version)
        async with self._lock:
            existing = self._releases.get(key)
            if existing is not None:
                return existing
            self._versions[key] = version
            self._releases[key] = release
            self._active[(version.app_id, version.agent_id)] = release
        return release

    async def get_active_release(self, app_id: str, agent_id: str) -> AgentReleaseSnapshot | None:
        return self._active.get((app_id, agent_id))

    async def bind_run(self, snapshot: RunAgentReleaseSnapshot) -> RunAgentReleaseSnapshot:
        async with self._lock:
            existing = self._run_snapshots.get(snapshot.run_id)
            if existing is not None:
                return existing
            self._run_snapshots[snapshot.run_id] = snapshot
        return snapshot

    async def get_run_snapshot(self, run_id: str) -> RunAgentReleaseSnapshot | None:
        return self._run_snapshots.get(run_id)

    async def is_model_referenced(self, config_id: str) -> bool:
        return any(
            ref.model_config_id == config_id
            for release in self._releases.values()
            for ref in release.model_refs
        )


class PostgresAgentRepository:
    """PostgreSQL implementation using immutable JSON contract payloads."""

    def __init__(self, *, dsn: str, schema: str = "full_view_agent") -> None:
        self._dsn = dsn
        self._schema = schema

    async def save_agent(self, agent: AgentDefinition) -> AgentDefinition:
        import psycopg

        async with await psycopg.AsyncConnection.connect(self._dsn) as conn:
            try:
                await conn.execute(
                    f'INSERT INTO "{self._schema}".agent_definitions '
                    "(app_id, agent_id, data_json) VALUES (%s, %s, %s::jsonb)",
                    (agent.app_id, agent.agent_id, agent.model_dump_json()),
                )
            except psycopg.errors.UniqueViolation as exc:
                raise RunStateConflict("agent is already registered") from exc
        return agent

    async def get_agent(self, app_id: str, agent_id: str) -> AgentDefinition | None:
        row = await self._fetchone(
            "agent_definitions", "app_id = %s AND agent_id = %s", (app_id, agent_id)
        )
        return AgentDefinition.model_validate(row[0]) if row else None

    async def list_agents(self, app_id: str) -> list[AgentDefinition]:
        rows = await self._fetchall("agent_definitions", "app_id = %s", (app_id,), order="agent_id")
        return [AgentDefinition.model_validate(row[0]) for row in rows]

    async def save_version(self, version: AgentVersion) -> AgentVersion:
        import psycopg

        async with await psycopg.AsyncConnection.connect(self._dsn) as conn:
            try:
                await conn.execute(
                    f'INSERT INTO "{self._schema}".agent_versions '
                    "(app_id, agent_id, version, data_json) VALUES (%s, %s, %s, %s::jsonb)",
                    (version.app_id, version.agent_id, version.version, version.model_dump_json()),
                )
            except psycopg.errors.UniqueViolation as exc:
                raise RunStateConflict("agent version already exists") from exc
        return version

    async def get_version(self, app_id: str, agent_id: str, version: str) -> AgentVersion | None:
        row = await self._fetchone(
            "agent_versions",
            "app_id = %s AND agent_id = %s AND version = %s",
            (app_id, agent_id, version),
        )
        return AgentVersion.model_validate(row[0]) if row else None

    async def list_versions(self, app_id: str, agent_id: str) -> list[AgentVersion]:
        rows = await self._fetchall(
            "agent_versions",
            "app_id = %s AND agent_id = %s",
            (app_id, agent_id),
            order="version",
        )
        return [AgentVersion.model_validate(row[0]) for row in rows]

    async def save_model_policy(
        self, app_id: str, agent_id: str, version: str, policy: AgentModelPolicy
    ) -> AgentModelPolicy:
        import psycopg

        async with await psycopg.AsyncConnection.connect(self._dsn) as conn:
            await conn.execute(
                f'INSERT INTO "{self._schema}".agent_model_policies '
                "(app_id, agent_id, version, data_json) VALUES (%s, %s, %s, %s::jsonb) "
                "ON CONFLICT (app_id, agent_id, version) "
                "DO UPDATE SET data_json = EXCLUDED.data_json",
                (app_id, agent_id, version, policy.model_dump_json()),
            )
        return policy

    async def get_model_policy(
        self, app_id: str, agent_id: str, version: str
    ) -> AgentModelPolicy | None:
        row = await self._fetchone(
            "agent_model_policies",
            "app_id = %s AND agent_id = %s AND version = %s",
            (app_id, agent_id, version),
        )
        return AgentModelPolicy.model_validate(row[0]) if row else None

    async def publish(
        self, version: AgentVersion, release: AgentReleaseSnapshot
    ) -> AgentReleaseSnapshot:
        import psycopg

        try:
            async with await psycopg.AsyncConnection.connect(self._dsn) as conn:
                await conn.execute(
                f'UPDATE "{self._schema}".agent_versions SET data_json = %s::jsonb '
                "WHERE app_id = %s AND agent_id = %s AND version = %s",
                (version.model_dump_json(), version.app_id, version.agent_id, version.version),
            )
                await conn.execute(
                f'UPDATE "{self._schema}".agent_release_snapshots SET is_active = false '
                "WHERE app_id = %s AND agent_id = %s AND is_active",
                (release.app_id, release.agent_id),
            )
                cursor = await conn.execute(
                f'INSERT INTO "{self._schema}".agent_release_snapshots '
                "(release_id, app_id, agent_id, agent_version, is_active, data_json) "
                "VALUES (%s, %s, %s, %s, true, %s::jsonb) "
                "ON CONFLICT (app_id, agent_id, agent_version) DO UPDATE SET is_active = true "
                "RETURNING data_json",
                (
                    release.release_id,
                    release.app_id,
                    release.agent_id,
                    release.agent_version,
                    release.model_dump_json(),
                ),
            )
                row = await cursor.fetchone()
                assert row is not None
        except psycopg.errors.UniqueViolation as exc:
            raise RunStateConflict("concurrent agent publish conflict") from exc
        return AgentReleaseSnapshot.model_validate(row[0])

    async def get_active_release(self, app_id: str, agent_id: str) -> AgentReleaseSnapshot | None:
        row = await self._fetchone(
            "agent_release_snapshots",
            "app_id = %s AND agent_id = %s AND is_active",
            (app_id, agent_id),
        )
        return AgentReleaseSnapshot.model_validate(row[0]) if row else None

    async def bind_run(self, snapshot: RunAgentReleaseSnapshot) -> RunAgentReleaseSnapshot:
        import psycopg

        async with await psycopg.AsyncConnection.connect(self._dsn) as conn:
            cursor = await conn.execute(
                f'INSERT INTO "{self._schema}".run_agent_release_snapshots '
                "(run_id, release_id, data_json) VALUES (%s, %s, %s::jsonb) "
                "ON CONFLICT (run_id) DO NOTHING RETURNING data_json",
                (snapshot.run_id, snapshot.release_id, snapshot.model_dump_json()),
            )
            row = await cursor.fetchone()
        if row:
            return RunAgentReleaseSnapshot.model_validate(row[0])
        existing = await self.get_run_snapshot(snapshot.run_id)
        if existing is None:
            raise RunStateConflict("run release snapshot race lost without winner")
        return existing

    async def get_run_snapshot(self, run_id: str) -> RunAgentReleaseSnapshot | None:
        row = await self._fetchone("run_agent_release_snapshots", "run_id = %s", (run_id,))
        return RunAgentReleaseSnapshot.model_validate(row[0]) if row else None

    async def is_model_referenced(self, config_id: str) -> bool:
        import psycopg

        async with await psycopg.AsyncConnection.connect(self._dsn) as conn:
            cursor = await conn.execute(
                f'SELECT 1 FROM "{self._schema}".agent_release_snapshots '
                "WHERE data_json -> 'model_refs' @> %s::jsonb LIMIT 1",
                (json.dumps([{"model_config_id": config_id}]),),
            )
            return await cursor.fetchone() is not None

    async def _fetchone(self, table: str, where: str, params: tuple[object, ...]):
        import psycopg

        async with await psycopg.AsyncConnection.connect(self._dsn) as conn:
            cursor = await conn.execute(
                f'SELECT data_json FROM "{self._schema}".{table} WHERE {where}', params
            )
            return await cursor.fetchone()

    async def _fetchall(self, table: str, where: str, params: tuple[object, ...], *, order: str):
        import psycopg

        async with await psycopg.AsyncConnection.connect(self._dsn) as conn:
            cursor = await conn.execute(
                f'SELECT data_json FROM "{self._schema}".{table} WHERE {where} ORDER BY {order}',
                params,
            )
            return await cursor.fetchall()
