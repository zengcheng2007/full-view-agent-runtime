from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, Protocol, cast

import psycopg

from full_view_agent.application.errors import RunStateConflict
from full_view_agent.domain.models import AgentEvent, AgentRun, AgentSession
from full_view_agent.infrastructure.event_broker import InMemoryEventBroker
from full_view_agent.infrastructure.memory_store import InMemoryAgentStore
from full_view_agent.infrastructure.postgres_persistence import PostgresAgentPersistence

SessionStatus = Literal["active", "archived"]


@dataclass(frozen=True)
class RuntimeModelAuthority:
    run_id: str
    config_id: str
    config_version: int
    model_name: str | None
    agent_id: str | None
    is_fallback: bool


class RuntimeObservabilityRepository(Protocol):
    async def list_sessions(
        self,
        *,
        tenant_id: str,
        app_id: str,
        status: SessionStatus | None = None,
        created_from: datetime | None = None,
        created_to: datetime | None = None,
        limit: int | None = None,
        before: tuple[datetime, str] | None = None,
    ) -> list[AgentSession]: ...

    async def list_runs(
        self,
        *,
        tenant_id: str,
        app_id: str,
        session_id: str | None = None,
        status: str | None = None,
        outcome: str | None = None,
        mode: str | None = None,
        created_from: datetime | None = None,
        created_to: datetime | None = None,
        limit: int | None = None,
        before: tuple[datetime, str] | None = None,
    ) -> list[AgentRun]: ...

    async def get_run(self, *, tenant_id: str, app_id: str, run_id: str) -> AgentRun | None: ...

    async def get_run_owner(self, *, tenant_id: str, app_id: str, run_id: str) -> str | None: ...

    async def count_runs_by_session(
        self, *, tenant_id: str, app_id: str, session_ids: list[str]
    ) -> dict[str, int]: ...

    async def list_events(
        self, *, tenant_id: str, app_id: str, run_ids: list[str]
    ) -> list[AgentEvent]: ...

    async def list_model_authorities(
        self, *, tenant_id: str, app_id: str, run_ids: list[str]
    ) -> dict[str, RuntimeModelAuthority]: ...


class InMemoryRuntimeObservabilityRepository:
    def __init__(
        self,
        store: InMemoryAgentStore,
        events: InMemoryEventBroker,
        *,
        model_bindings: Any = None,
        agent_repository: Any = None,
    ) -> None:
        self._store = store
        self._events = events
        self._model_bindings = model_bindings
        self._agent_repository = agent_repository

    async def list_sessions(
        self,
        *,
        tenant_id: str,
        app_id: str,
        status: SessionStatus | None = None,
        created_from: datetime | None = None,
        created_to: datetime | None = None,
        limit: int | None = None,
        before: tuple[datetime, str] | None = None,
    ) -> list[AgentSession]:
        async with self._store._lock:
            sessions = [
                session
                for session in self._store.sessions.values()
                if session.owner_tenant_id == tenant_id
                and session.app_id == app_id
                and (status is None or session.status == status)
                and (created_from is None or session.created_at >= created_from)
                and (created_to is None or session.created_at <= created_to)
            ]
        ordered = sorted(
            sessions,
            key=lambda item: (item.updated_at, item.session_id),
            reverse=True,
        )
        if before is not None:
            ordered = [item for item in ordered if (item.updated_at, item.session_id) < before]
        return ordered[:limit] if limit is not None else ordered

    async def list_runs(
        self,
        *,
        tenant_id: str,
        app_id: str,
        session_id: str | None = None,
        status: str | None = None,
        outcome: str | None = None,
        mode: str | None = None,
        created_from: datetime | None = None,
        created_to: datetime | None = None,
        limit: int | None = None,
        before: tuple[datetime, str] | None = None,
    ) -> list[AgentRun]:
        async with self._store._lock:
            allowed_sessions = {
                item.session_id
                for item in self._store.sessions.values()
                if item.owner_tenant_id == tenant_id and item.app_id == app_id
            }
            runs = [
                run
                for run in self._store.runs.values()
                if run.session_id in allowed_sessions
                and (session_id is None or run.session_id == session_id)
                and (status is None or run.status == status)
                and (outcome is None or run.outcome == outcome)
                and (mode is None or run.mode == mode)
                and (created_from is None or run.created_at >= created_from)
                and (created_to is None or run.created_at <= created_to)
            ]
        ordered = sorted(runs, key=lambda item: (item.created_at, item.run_id), reverse=True)
        if before is not None:
            ordered = [item for item in ordered if (item.created_at, item.run_id) < before]
        return ordered[:limit] if limit is not None else ordered

    async def get_run(self, *, tenant_id: str, app_id: str, run_id: str) -> AgentRun | None:
        runs = await self.list_runs(
            tenant_id=tenant_id,
            app_id=app_id,
        )
        return next((run for run in runs if run.run_id == run_id), None)

    async def get_run_owner(self, *, tenant_id: str, app_id: str, run_id: str) -> str | None:
        async with self._store._lock:
            run = self._store.runs.get(run_id)
            session = self._store.sessions.get(run.session_id) if run is not None else None
            if session is None or session.owner_tenant_id != tenant_id or session.app_id != app_id:
                return None
            return session.owner_user_id

    async def count_runs_by_session(
        self, *, tenant_id: str, app_id: str, session_ids: list[str]
    ) -> dict[str, int]:
        wanted = set(session_ids)
        if not wanted:
            return {}
        async with self._store._lock:
            allowed = {
                session.session_id
                for session in self._store.sessions.values()
                if session.session_id in wanted
                and session.owner_tenant_id == tenant_id
                and session.app_id == app_id
            }
            counts = {session_id: 0 for session_id in allowed}
            for run in self._store.runs.values():
                if run.session_id in allowed:
                    counts[run.session_id] += 1
        return counts

    async def list_events(
        self, *, tenant_id: str, app_id: str, run_ids: list[str]
    ) -> list[AgentEvent]:
        async with self._store._lock:
            allowed = {
                run.run_id
                for run in self._store.runs.values()
                if run.run_id in run_ids
                and (session := self._store.sessions.get(run.session_id)) is not None
                and session.owner_tenant_id == tenant_id
                and session.app_id == app_id
            }
        async with self._events._condition:
            now = self._events._now()
            events = [
                event
                for run_id in allowed
                for event in self._events._events.get(run_id, [])
                if self._events._expires_at.get(event.event_id, now) > now
            ]
        return sorted(events, key=lambda item: (item.occurred_at, item.run_id, item.sequence))

    async def list_model_authorities(
        self, *, tenant_id: str, app_id: str, run_ids: list[str]
    ) -> dict[str, RuntimeModelAuthority]:
        if self._model_bindings is None:
            return {}
        async with self._store._lock:
            allowed = {
                run.run_id
                for run in self._store.runs.values()
                if run.run_id in run_ids
                and (session := self._store.sessions.get(run.session_id)) is not None
                and session.owner_tenant_id == tenant_id
                and session.app_id == app_id
            }
        bindings = getattr(self._model_bindings, "_bindings", {})
        snapshots = getattr(self._model_bindings, "_snapshots", {})
        releases = getattr(self._agent_repository, "_run_snapshots", {})
        result: dict[str, RuntimeModelAuthority] = {}
        for run_id in allowed:
            binding = bindings.get(run_id)
            if binding is None:
                continue
            snapshot = snapshots.get((binding.config_id, binding.config_version))
            release = releases.get(run_id)
            if release is not None and (release.tenant_id != tenant_id or release.app_id != app_id):
                release = None
            result[run_id] = RuntimeModelAuthority(
                run_id=run_id,
                config_id=binding.config_id,
                config_version=binding.config_version,
                model_name=snapshot.model_name if snapshot is not None else None,
                agent_id=release.agent_id if release is not None else None,
                is_fallback=bool(
                    release is not None
                    and any(
                        ref.model_config_id == binding.config_id
                        and ref.config_version == binding.config_version
                        and ref.role == "fallback"
                        for ref in release.model_refs
                    )
                ),
            )
        return result


class PostgresRuntimeObservabilityRepository:
    def __init__(self, *, dsn: str, schema: str) -> None:
        self._dsn = dsn
        self._schema = schema

    async def list_sessions(
        self,
        *,
        tenant_id: str,
        app_id: str,
        status: SessionStatus | None = None,
        created_from: datetime | None = None,
        created_to: datetime | None = None,
        limit: int | None = None,
        before: tuple[datetime, str] | None = None,
    ) -> list[AgentSession]:
        clauses = ["owner_tenant_id = %s", "app_id = %s"]
        params: list[object] = [tenant_id, app_id]
        if status is not None:
            clauses.append("data_json::jsonb->>'status' = %s")
            params.append(status)
        if created_from is not None:
            clauses.append("obs_created_at >= %s")
            params.append(created_from)
        if created_to is not None:
            clauses.append("obs_created_at <= %s")
            params.append(created_to)
        if before is not None:
            clauses.append("(obs_updated_at, session_id) < (%s, %s)")
            params.extend(before)
        sql = (
            f'SELECT data_json FROM "{self._schema}".sessions WHERE '
            + " AND ".join(clauses)
            + " ORDER BY obs_updated_at DESC, session_id DESC"
        )
        if limit is not None:
            sql += " LIMIT %s"
            params.append(limit)
        async with await psycopg.AsyncConnection.connect(self._dsn) as connection:
            rows = await (await connection.execute(cast(Any, sql), params)).fetchall()
        sessions = [AgentSession.model_validate_json(row[0]) for row in rows]
        return sorted(
            sessions,
            key=lambda item: (item.updated_at, item.session_id),
            reverse=True,
        )

    async def list_runs(
        self,
        *,
        tenant_id: str,
        app_id: str,
        session_id: str | None = None,
        status: str | None = None,
        outcome: str | None = None,
        mode: str | None = None,
        created_from: datetime | None = None,
        created_to: datetime | None = None,
        limit: int | None = None,
        before: tuple[datetime, str] | None = None,
    ) -> list[AgentRun]:
        clauses = ["s.owner_tenant_id = %s", "s.app_id = %s"]
        params: list[object] = [tenant_id, app_id]
        filters = {
            "r.session_id = %s": session_id,
            "r.obs_status = %s": status,
            "r.obs_outcome = %s": outcome,
            "r.obs_mode = %s": mode,
        }
        for clause, value in filters.items():
            if value is not None:
                clauses.append(clause)
                params.append(value)
        if created_from is not None:
            clauses.append("r.obs_created_at >= %s")
            params.append(created_from)
        if created_to is not None:
            clauses.append("r.obs_created_at <= %s")
            params.append(created_to)
        if before is not None:
            clauses.append("(r.obs_created_at, r.run_id) < (%s, %s)")
            params.extend(before)
        sql = (
            f'SELECT r.data_json FROM "{self._schema}".runs r '
            f'JOIN "{self._schema}".sessions s ON s.session_id = r.session_id '
            "WHERE " + " AND ".join(clauses) + " ORDER BY r.obs_created_at DESC, r.run_id DESC"
        )
        if limit is not None:
            sql += " LIMIT %s"
            params.append(limit)
        async with await psycopg.AsyncConnection.connect(self._dsn) as connection:
            rows = await (await connection.execute(cast(Any, sql), params)).fetchall()
        runs = [AgentRun.model_validate_json(row[0]) for row in rows]
        return sorted(runs, key=lambda item: (item.created_at, item.run_id), reverse=True)

    async def get_run(self, *, tenant_id: str, app_id: str, run_id: str) -> AgentRun | None:
        sql = (
            f'SELECT r.data_json FROM "{self._schema}".runs r '
            f'JOIN "{self._schema}".sessions s ON s.session_id = r.session_id '
            "WHERE r.run_id = %s AND s.owner_tenant_id = %s AND s.app_id = %s"
        )
        async with await psycopg.AsyncConnection.connect(self._dsn) as connection:
            row = await (
                await connection.execute(
                    cast(Any, sql),
                    (run_id, tenant_id, app_id),
                )
            ).fetchone()
        return AgentRun.model_validate_json(row[0]) if row is not None else None

    async def get_run_owner(self, *, tenant_id: str, app_id: str, run_id: str) -> str | None:
        sql = (
            f'SELECT s.owner_user_id FROM "{self._schema}".runs r '
            f'JOIN "{self._schema}".sessions s ON s.session_id = r.session_id '
            "WHERE r.run_id = %s AND s.owner_tenant_id = %s AND s.app_id = %s"
        )
        async with await psycopg.AsyncConnection.connect(self._dsn) as connection:
            row = await (
                await connection.execute(
                    cast(Any, sql),
                    (run_id, tenant_id, app_id),
                )
            ).fetchone()
        return str(row[0]) if row is not None else None

    async def count_runs_by_session(
        self, *, tenant_id: str, app_id: str, session_ids: list[str]
    ) -> dict[str, int]:
        if not session_ids:
            return {}
        sql = (
            f'SELECT r.session_id, COUNT(*) FROM "{self._schema}".runs r '
            f'JOIN "{self._schema}".sessions s ON s.session_id = r.session_id '
            "WHERE r.session_id = ANY(%s) AND s.owner_tenant_id = %s AND s.app_id = %s "
            "GROUP BY r.session_id"
        )
        async with await psycopg.AsyncConnection.connect(self._dsn) as connection:
            rows = await (
                await connection.execute(cast(Any, sql), (session_ids, tenant_id, app_id))
            ).fetchall()
        return {str(row[0]): int(row[1]) for row in rows}

    async def list_events(
        self, *, tenant_id: str, app_id: str, run_ids: list[str]
    ) -> list[AgentEvent]:
        if not run_ids:
            return []
        sql = (
            f'SELECT e.data_json FROM "{self._schema}".events e '
            f'JOIN "{self._schema}".runs r ON r.run_id = e.run_id '
            f'JOIN "{self._schema}".sessions s ON s.session_id = r.session_id '
            "WHERE e.run_id = ANY(%s) AND s.owner_tenant_id = %s "
            "AND s.app_id = %s AND e.expires_at > %s "
            "ORDER BY e.run_id, e.sequence LIMIT 100001"
        )
        async with await psycopg.AsyncConnection.connect(self._dsn) as connection:
            rows = await (
                await connection.execute(
                    cast(Any, sql),
                    (run_ids, tenant_id, app_id, datetime.now(UTC)),
                )
            ).fetchall()
        if len(rows) > 100_000:
            raise RunStateConflict("runtime event scan exceeds 100000 rows")
        return [AgentEvent.model_validate_json(row[0]) for row in rows]

    async def list_model_authorities(
        self, *, tenant_id: str, app_id: str, run_ids: list[str]
    ) -> dict[str, RuntimeModelAuthority]:
        if not run_ids:
            return {}
        sql = (
            f"SELECT r.run_id, b.config_id, b.config_version, m.model_name, a.data_json "
            f'FROM "{self._schema}".runs r '
            f'JOIN "{self._schema}".sessions s ON s.session_id = r.session_id '
            f'JOIN "{self._schema}".run_model_bindings b ON b.run_id = r.run_id '
            f'LEFT JOIN "{self._schema}".run_model_config_snapshots m '
            "ON m.config_id = b.config_id AND m.config_version = b.config_version "
            f'LEFT JOIN "{self._schema}".run_agent_release_snapshots a ON a.run_id = r.run_id '
            "WHERE r.run_id = ANY(%s) AND s.owner_tenant_id = %s AND s.app_id = %s"
        )
        async with await psycopg.AsyncConnection.connect(self._dsn) as connection:
            rows = await (
                await connection.execute(cast(Any, sql), (run_ids, tenant_id, app_id))
            ).fetchall()
        result: dict[str, RuntimeModelAuthority] = {}
        for run_id, config_id, config_version, model_name, release_json in rows:
            release = None
            if release_json is not None:
                from full_view_agent.domain.agent_definition import RunAgentReleaseSnapshot

                release = RunAgentReleaseSnapshot.model_validate(release_json)
                if release.tenant_id != tenant_id or release.app_id != app_id:
                    release = None
            result[str(run_id)] = RuntimeModelAuthority(
                run_id=str(run_id),
                config_id=str(config_id),
                config_version=int(config_version),
                model_name=str(model_name) if model_name is not None else None,
                agent_id=release.agent_id if release is not None else None,
                is_fallback=bool(
                    release is not None
                    and any(
                        ref.model_config_id == config_id
                        and ref.config_version == config_version
                        and ref.role == "fallback"
                        for ref in release.model_refs
                    )
                ),
            )
        return result


def runtime_observability_repository(
    store: object,
    events: object,
    *,
    model_bindings: object = None,
    agent_repository: object = None,
) -> RuntimeObservabilityRepository:
    if isinstance(store, InMemoryAgentStore) and isinstance(events, InMemoryEventBroker):
        return InMemoryRuntimeObservabilityRepository(
            store,
            events,
            model_bindings=model_bindings,
            agent_repository=agent_repository,
        )
    if isinstance(store, PostgresAgentPersistence):
        return PostgresRuntimeObservabilityRepository(
            dsn=store._dsn,
            schema=store._schema,
        )
    raise RuntimeError("runtime observability requires a supported authority store")
