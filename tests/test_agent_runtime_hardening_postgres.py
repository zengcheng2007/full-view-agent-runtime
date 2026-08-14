from __future__ import annotations

import asyncio

import pytest

from full_view_agent.application.errors import RunStateConflict
from full_view_agent.domain.agent_definition import (
    AgentDefinition,
    AgentModelVersionRef,
    AgentReleaseSnapshot,
    AgentVersion,
    RunAgentReleaseSnapshot,
)
from full_view_agent.infrastructure.agent_repository import PostgresAgentRepository


def _release(*, version: str, release_id: str) -> AgentReleaseSnapshot:
    return AgentReleaseSnapshot(
        release_id=release_id,
        app_id="full_information_view",
        agent_id="concurrent_agent",
        agent_version=version,
        model_refs=(
            AgentModelVersionRef(
                model_config_id="model_primary",
                config_version=1,
                role="primary",
                order=0,
            ),
        ),
        published_by="admin",
        reason=f"publish {version}",
    )


@pytest.mark.asyncio
async def test_postgres_release_and_run_binding_survive_repository_restart(
    pg_schema,
) -> None:
    first = PostgresAgentRepository(dsn=pg_schema["dsn"], schema=pg_schema["schema"])
    agent = AgentDefinition(
        app_id="full_information_view",
        agent_id="concurrent_agent",
        name="并发发布智能体",
    )
    version = AgentVersion(
        app_id=agent.app_id,
        agent_id=agent.agent_id,
        version="1.0.0",
        status="published",
    )
    release = _release(version=version.version, release_id="release-restart-v1")
    await first.save_agent(agent)
    await first.save_version(version)
    published = await first.publish(version, release)
    bound = await first.bind_run(
        RunAgentReleaseSnapshot(
            **published.model_dump(mode="python"),
            run_id="run-restart-v1",
        )
    )

    reopened = PostgresAgentRepository(
        dsn=pg_schema["dsn"], schema=pg_schema["schema"]
    )

    assert await reopened.get_active_release(agent.app_id, agent.agent_id) == published
    assert await reopened.get_run_snapshot(bound.run_id) == bound


@pytest.mark.asyncio
async def test_concurrent_postgres_publish_has_one_winner_and_domain_conflict(
    pg_schema,
) -> None:
    repository = PostgresAgentRepository(
        dsn=pg_schema["dsn"], schema=pg_schema["schema"]
    )
    agent = AgentDefinition(
        app_id="full_information_view",
        agent_id="concurrent_agent",
        name="并发发布智能体",
    )
    await repository.save_agent(agent)
    versions = [
        AgentVersion(
            app_id=agent.app_id,
            agent_id=agent.agent_id,
            version=version,
            status="published",
        )
        for version in ("1.0.0", "2.0.0")
    ]
    for version in versions:
        await repository.save_version(version)

    outcomes = await asyncio.gather(
        *(
            repository.publish(
                version,
                _release(
                    version=version.version,
                    release_id=f"release-concurrent-{version.version}",
                ),
            )
            for version in versions
        ),
        return_exceptions=True,
    )

    successes = [item for item in outcomes if isinstance(item, AgentReleaseSnapshot)]
    conflicts = [item for item in outcomes if isinstance(item, RunStateConflict)]
    assert len(successes) == 1, repr(outcomes)
    assert len(conflicts) == 1, repr(outcomes)
    active = await repository.get_active_release(agent.app_id, agent.agent_id)
    assert active == successes[0]
