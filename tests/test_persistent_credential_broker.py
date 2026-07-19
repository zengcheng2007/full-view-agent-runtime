from base64 import urlsafe_b64encode
from datetime import UTC, datetime, timedelta
from secrets import token_bytes

import pytest
from pydantic import SecretStr

from full_view_agent.application.errors import CredentialUnavailable
from full_view_agent.infrastructure.credential_broker import (
    EncryptedSqliteCredentialBroker,
)


def test_runtime_credential_factory_requires_key_and_builds_persistent_store(
    tmp_path,
    monkeypatch,
) -> None:
    from full_view_agent.api.app import default_credential_broker

    database_path = tmp_path / "runtime-credentials.db"
    monkeypatch.setenv(
        "FULL_VIEW_CREDENTIAL_KEY",
        urlsafe_b64encode(token_bytes(32)).decode("ascii"),
    )
    monkeypatch.setenv("FULL_VIEW_CREDENTIAL_DB_PATH", str(database_path))

    broker = default_credential_broker()

    assert isinstance(broker, EncryptedSqliteCredentialBroker)


@pytest.mark.asyncio
async def test_encrypted_credential_survives_restart_without_plaintext(tmp_path) -> None:
    database_path = tmp_path / "credentials.db"
    key = token_bytes(32)
    token = "persistent-secret-token"
    first = EncryptedSqliteCredentialBroker(
        database_path=database_path,
        encryption_key=key,
    )
    grant = await first.issue(
        raw_token=SecretStr(token),
        subject_user_id="user-01",
        app_id="full_information_view",
        run_id="run-01",
        source_expires_at=datetime.now(UTC) + timedelta(hours=1),
    )

    assert token.encode() not in database_path.read_bytes()

    restarted = EncryptedSqliteCredentialBroker(
        database_path=database_path,
        encryption_key=key,
    )
    resolved = await restarted.resolve(
        credential_ref=grant.credential_ref,
        subject_user_id="user-01",
        app_id="full_information_view",
        run_id="run-01",
    )

    assert resolved.get_secret_value() == token


@pytest.mark.asyncio
async def test_encrypted_credential_rejects_revoked_or_wrong_key(tmp_path) -> None:
    database_path = tmp_path / "credentials.db"
    key = token_bytes(32)
    broker = EncryptedSqliteCredentialBroker(
        database_path=database_path,
        encryption_key=key,
    )
    grant = await broker.issue(
        raw_token=SecretStr("revocable-token"),
        subject_user_id="user-01",
        app_id="full_information_view",
        run_id="run-01",
        source_expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    await broker.revoke(credential_ref=grant.credential_ref)

    with pytest.raises(CredentialUnavailable):
        await broker.resolve(
            credential_ref=grant.credential_ref,
            subject_user_id="user-01",
            app_id="full_information_view",
            run_id="run-01",
        )

    other_key_broker = EncryptedSqliteCredentialBroker(
        database_path=database_path,
        encryption_key=token_bytes(32),
    )
    second = await broker.issue(
        raw_token=SecretStr("wrong-key-token"),
        subject_user_id="user-01",
        app_id="full_information_view",
        run_id="run-02",
        source_expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    with pytest.raises(CredentialUnavailable):
        await other_key_broker.resolve(
            credential_ref=second.credential_ref,
            subject_user_id="user-01",
            app_id="full_information_view",
            run_id="run-02",
        )
