from __future__ import annotations

import psycopg
import pytest

from full_view_agent.domain.capability import ToolSemanticContract
from full_view_agent.infrastructure.postgres_persistence import PostgresAgentPersistence


@pytest.mark.db
@pytest.mark.asyncio
async def test_v029_publishes_immutable_population_contract_and_survives_restart(
    pg_schema: dict[str, str],
) -> None:
    dsn, schema = pg_schema["dsn"], pg_schema["schema"]
    first = PostgresAgentPersistence(dsn=dsn, schema=schema)
    await first.initialize()

    async def read_state() -> tuple[dict[str, object], list[tuple[str, bool]]]:
        async with await psycopg.AsyncConnection.connect(dsn) as connection:
            contract_cursor = await connection.execute(
                f'SELECT semantic_contract FROM "{schema}".capability_tools '
                "WHERE capability_id = %s AND version = '1.2.0'",
                ("governance.query_population_metrics",),
            )
            contract_row = await contract_cursor.fetchone()
            assert contract_row is not None
            binding_cursor = await connection.execute(
                f'SELECT capability_version, enabled FROM "{schema}".'
                "application_capability_bindings "
                "WHERE app_id = 'full_information_view' AND capability_id = %s "
                "ORDER BY capability_version",
                ("governance.query_population_metrics",),
            )
            return contract_row[0], await binding_cursor.fetchall()

    contract_payload, bindings = await read_state()
    contract = ToolSemanticContract.model_validate(contract_payload)
    assert contract.intent_terms
    assert all(shape.argument_template for shape in contract.query_shapes)
    assert ("1.1.0", False) in bindings
    assert ("1.2.0", True) in bindings

    restarted = PostgresAgentPersistence(dsn=dsn, schema=schema)
    await restarted.initialize()
    restarted_payload, restarted_bindings = await read_state()
    assert restarted_payload == contract_payload
    assert restarted_bindings == bindings
