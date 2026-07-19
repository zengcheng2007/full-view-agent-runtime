from datetime import UTC, datetime, timedelta

import pytest
from pydantic import SecretStr

from full_view_agent.application.errors import ResourceNotFound


@pytest.mark.asyncio
async def test_credential_broker_returns_reference_without_exposing_raw_token() -> None:
    from full_view_agent.infrastructure.credential_broker import (
        InMemoryCredentialBroker,
    )

    now = datetime.now(UTC)
    broker = InMemoryCredentialBroker(default_ttl_seconds=300)

    grant = await broker.issue(
        raw_token=SecretStr("raw-geo-token-must-stay-private"),
        subject_user_id="user-01",
        app_id="full_information_view",
        run_id="run-01",
        source_expires_at=now + timedelta(hours=1),
    )
    resolved = await broker.resolve(
        credential_ref=grant.credential_ref,
        subject_user_id="user-01",
        app_id="full_information_view",
        run_id="run-01",
    )

    assert grant.credential_type == "legacy_geo_token"
    assert resolved.get_secret_value() == "raw-geo-token-must-stay-private"
    assert "raw-geo-token-must-stay-private" not in grant.model_dump_json()
    assert "raw-geo-token-must-stay-private" not in repr(broker)


@pytest.mark.asyncio
async def test_credential_broker_rejects_different_run_binding() -> None:
    from full_view_agent.infrastructure.credential_broker import (
        InMemoryCredentialBroker,
    )

    now = datetime.now(UTC)
    broker = InMemoryCredentialBroker(default_ttl_seconds=300)
    grant = await broker.issue(
        raw_token=SecretStr("run-bound-token"),
        subject_user_id="user-01",
        app_id="full_information_view",
        run_id="run-01",
        source_expires_at=now + timedelta(hours=1),
    )

    with pytest.raises(ResourceNotFound):
        await broker.resolve(
            credential_ref=grant.credential_ref,
            subject_user_id="user-01",
            app_id="full_information_view",
            run_id="run-02",
        )


@pytest.mark.asyncio
async def test_credential_broker_rejects_expired_reference() -> None:
    from full_view_agent.infrastructure.credential_broker import (
        InMemoryCredentialBroker,
    )

    clock = [datetime(2026, 7, 18, 9, 0, tzinfo=UTC)]
    broker = InMemoryCredentialBroker(
        default_ttl_seconds=60,
        now=lambda: clock[0],
    )
    grant = await broker.issue(
        raw_token=SecretStr("short-lived-token"),
        subject_user_id="user-01",
        app_id="full_information_view",
        run_id="run-01",
        source_expires_at=clock[0] + timedelta(hours=1),
    )
    clock[0] += timedelta(seconds=61)

    with pytest.raises(ResourceNotFound):
        await broker.resolve(
            credential_ref=grant.credential_ref,
            subject_user_id="user-01",
            app_id="full_information_view",
            run_id="run-01",
        )


@pytest.mark.asyncio
async def test_credential_broker_revokes_all_credentials_for_logged_out_subject() -> None:
    from datetime import UTC, datetime, timedelta

    from pydantic import SecretStr

    from full_view_agent.application.errors import CredentialUnavailable
    from full_view_agent.infrastructure.credential_broker import (
        InMemoryCredentialBroker,
    )

    broker = InMemoryCredentialBroker()
    grant = await broker.issue(
        raw_token=SecretStr("logout-token"),
        subject_user_id="user-logout",
        app_id="full_information_view",
        run_id="run-logout",
        source_expires_at=datetime.now(UTC) + timedelta(hours=1),
    )

    await broker.revoke_subject(subject_user_id="user-logout")

    with pytest.raises(CredentialUnavailable):
        await broker.resolve(
            credential_ref=grant.credential_ref,
            subject_user_id="user-logout",
            app_id="full_information_view",
            run_id="run-logout",
        )
