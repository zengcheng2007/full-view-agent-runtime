from __future__ import annotations

from datetime import UTC, datetime

import pytest

from full_view_agent.application.errors import RunStateConflict
from full_view_agent.domain.capability import Connector, ConnectorAuditEvent
from full_view_agent.infrastructure.capability_repository import (
    PostgresCapabilityRepository,
)
from full_view_agent.infrastructure.postgres_persistence import PostgresAgentPersistence


@pytest.mark.db
@pytest.mark.asyncio
async def test_v017_connector_cas_and_audit_survive_repository_restart(
    pg_schema: dict[str, str],
) -> None:
    dsn, schema = pg_schema["dsn"], pg_schema["schema"]
    first = PostgresAgentPersistence(dsn=dsn, schema=schema)
    await first.initialize()
    repository = PostgresCapabilityRepository(dsn=dsn, schema=schema)
    connector = Connector(
        connector_id="connector.restart",
        name="重启验证连接器",
        base_url="https://93.184.216.34/api",
        allowed_path_prefixes=["/api"],
        created_by="admin-a",
        updated_by="admin-a",
    )
    await repository.save_connector(connector)
    with pytest.raises(RunStateConflict, match="already exists"):
        await repository.save_connector(
            connector.model_copy(update={"name": "不得覆盖的重复连接器"})
        )
    updated = connector.model_copy(
        update={
            "name": "重启后连接器",
            "etag": 2,
            "updated_by": "admin-b",
            "updated_at": datetime.now(UTC),
        }
    )
    event = ConnectorAuditEvent(
        event_id="cae-restart",
        connector_id=connector.connector_id,
        action="update",
        actor="admin-b",
        reason="验证自动迁移与重启读取",
        previous_etag=1,
        new_etag=2,
        changed_fields=["name"],
        from_active=True,
        to_active=True,
    )
    await repository.update_connector(updated, expected_etag=1, event=event)

    restarted = PostgresAgentPersistence(dsn=dsn, schema=schema)
    await restarted.initialize()
    restarted_repository = PostgresCapabilityRepository(dsn=dsn, schema=schema)
    loaded = await restarted_repository.get_connector(connector.connector_id)
    events = await restarted_repository.list_connector_audit_events(
        connector.connector_id
    )

    assert loaded is not None
    assert (loaded.name, loaded.etag, loaded.updated_by) == (
        "重启后连接器",
        2,
        "admin-b",
    )
    assert [(item.action, item.reason) for item in events] == [
        ("update", "验证自动迁移与重启读取")
    ]
