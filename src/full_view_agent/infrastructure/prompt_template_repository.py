# pyright: reportArgumentType=false
from __future__ import annotations

from full_view_agent.domain.prompt_template import PromptLifecycleEvent, PromptTemplate


class InMemoryPromptTemplateRepository:
    def __init__(self) -> None:
        self._templates: dict[tuple[str, str], PromptTemplate] = {}
        self._events: list[PromptLifecycleEvent] = []

    async def save(self, template: PromptTemplate) -> None:
        self._templates[(template.prompt_id, template.version)] = template

    async def get(self, prompt_id: str, version: str) -> PromptTemplate | None:
        return self._templates.get((prompt_id, version))

    async def list(self, *, app_id: str | None = None) -> list[PromptTemplate]:
        items = list(self._templates.values())
        if app_id is not None:
            items = [item for item in items if item.app_id == app_id]
        return sorted(items, key=lambda item: (item.app_id, item.prompt_id, item.version))

    async def append_event(self, event: PromptLifecycleEvent) -> None:
        self._events.append(event)

    async def list_events(
        self, *, prompt_id: str | None = None
    ) -> list[PromptLifecycleEvent]:
        events = self._events
        if prompt_id is not None:
            events = [item for item in events if item.prompt_id == prompt_id]
        return sorted(events, key=lambda item: item.changed_at)


class PostgresPromptTemplateRepository:
    def __init__(self, *, dsn: str, schema: str = "full_view_agent") -> None:
        self._dsn = dsn
        self._schema = schema

    async def save(self, template: PromptTemplate) -> None:
        import psycopg

        async with await psycopg.AsyncConnection.connect(self._dsn) as conn:
            await conn.execute(
                f"""
                INSERT INTO {self._schema}.prompt_templates (
                    prompt_id, app_id, prompt_layer, name, version, content, status, etag,
                    created_by, updated_by, created_at, updated_at
                ) VALUES (
                    %(prompt_id)s, %(app_id)s, %(layer)s, %(name)s, %(version)s, %(content)s,
                    %(status)s, %(etag)s, %(created_by)s, %(updated_by)s,
                    %(created_at)s, %(updated_at)s
                )
                ON CONFLICT (prompt_id, version) DO UPDATE SET
                    app_id=EXCLUDED.app_id, prompt_layer=EXCLUDED.prompt_layer,
                    name=EXCLUDED.name,
                    content=EXCLUDED.content, status=EXCLUDED.status,
                    etag=EXCLUDED.etag, updated_by=EXCLUDED.updated_by,
                    updated_at=EXCLUDED.updated_at
                """,
                template.model_dump(mode="python"),
            )

    async def get(self, prompt_id: str, version: str) -> PromptTemplate | None:
        items = await self._fetch(
            "WHERE prompt_id = %s AND version = %s", (prompt_id, version)
        )
        return items[0] if items else None

    async def list(self, *, app_id: str | None = None) -> list[PromptTemplate]:
        return await self._fetch(
            "WHERE app_id = %s" if app_id is not None else "",
            (app_id,) if app_id is not None else (),
        )

    async def _fetch(
        self, where: str, params: tuple[object, ...]
    ) -> list[PromptTemplate]:
        import psycopg

        async with await psycopg.AsyncConnection.connect(self._dsn) as conn:
            cursor = await conn.execute(
                f"""
                SELECT prompt_id, app_id, prompt_layer, name, version, content, status, etag,
                       created_by, updated_by, created_at, updated_at
                  FROM {self._schema}.prompt_templates {where}
                 ORDER BY app_id, prompt_id, version
                """,
                params,
            )
            rows = await cursor.fetchall()
        return [
            PromptTemplate(
                prompt_id=row[0], app_id=row[1], layer=row[2], name=row[3],
                version=row[4], content=row[5], status=row[6], etag=row[7],
                created_by=row[8], updated_by=row[9], created_at=row[10],
                updated_at=row[11],
            )
            for row in rows
        ]

    async def append_event(self, event: PromptLifecycleEvent) -> None:
        import psycopg

        async with await psycopg.AsyncConnection.connect(self._dsn) as conn:
            await conn.execute(
                f"""
                INSERT INTO {self._schema}.prompt_lifecycle_events (
                    event_id, prompt_id, version, from_status, to_status,
                    actor, reason, changed_at
                ) VALUES (
                    %(event_id)s, %(prompt_id)s, %(version)s, %(from_status)s,
                    %(to_status)s, %(actor)s, %(reason)s, %(changed_at)s
                )
                """,
                event.model_dump(mode="python"),
            )

    async def list_events(
        self, *, prompt_id: str | None = None
    ) -> list[PromptLifecycleEvent]:
        import psycopg

        where = "WHERE prompt_id = %s" if prompt_id is not None else ""
        params = (prompt_id,) if prompt_id is not None else ()
        async with await psycopg.AsyncConnection.connect(self._dsn) as conn:
            cursor = await conn.execute(
                f"""
                SELECT event_id, prompt_id, version, from_status, to_status,
                       actor, reason, changed_at
                  FROM {self._schema}.prompt_lifecycle_events {where}
                 ORDER BY changed_at
                """,
                params,
            )
            rows = await cursor.fetchall()
        return [
            PromptLifecycleEvent(
                event_id=row[0], prompt_id=row[1], version=row[2],
                from_status=row[3], to_status=row[4], actor=row[5],
                reason=row[6], changed_at=row[7],
            )
            for row in rows
        ]
