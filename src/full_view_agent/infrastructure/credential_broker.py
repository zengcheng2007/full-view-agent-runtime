import asyncio
import os
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pydantic import SecretStr

from full_view_agent.application.errors import CredentialUnavailable
from full_view_agent.application.session_run_service import new_id
from full_view_agent.domain.models import CredentialGrant


@dataclass(frozen=True, repr=False)
class _StoredCredential:
    raw_token: SecretStr
    grant: CredentialGrant


class InMemoryCredentialBroker:
    def __init__(
        self,
        *,
        default_ttl_seconds: int = 300,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._default_ttl = timedelta(seconds=default_ttl_seconds)
        self._now = now or (lambda: datetime.now(UTC))
        self._lock = asyncio.Lock()
        self._credentials: dict[str, _StoredCredential] = {}

    async def issue(
        self,
        *,
        raw_token: SecretStr,
        subject_user_id: str,
        app_id: str,
        run_id: str,
        source_expires_at: datetime,
    ) -> CredentialGrant:
        now = self._now()
        grant = CredentialGrant(
            credential_ref=new_id("cred"),
            subject_user_id=subject_user_id,
            app_id=app_id,
            run_id=run_id,
            created_at=now,
            expires_at=min(source_expires_at, now + self._default_ttl),
        )
        async with self._lock:
            self._credentials[grant.credential_ref] = _StoredCredential(
                raw_token=raw_token,
                grant=grant,
            )
        return grant

    async def resolve(
        self,
        *,
        credential_ref: str,
        subject_user_id: str,
        app_id: str,
        run_id: str,
    ) -> SecretStr:
        async with self._lock:
            stored = self._credentials.get(credential_ref)
            if stored is None:
                raise CredentialUnavailable("credential not found")
            grant = stored.grant
            if (
                grant.subject_user_id != subject_user_id
                or grant.app_id != app_id
                or grant.run_id != run_id
                or grant.expires_at <= self._now()
            ):
                raise CredentialUnavailable("credential not found")
            return stored.raw_token

    async def revoke(self, *, credential_ref: str) -> None:
        async with self._lock:
            self._credentials.pop(credential_ref, None)

    async def revoke_subject(self, *, subject_user_id: str) -> None:
        async with self._lock:
            self._credentials = {
                ref: stored
                for ref, stored in self._credentials.items()
                if stored.grant.subject_user_id != subject_user_id
            }


class UnconfiguredCredentialBroker:
    async def issue(self, **_kwargs) -> CredentialGrant:
        raise CredentialUnavailable("credential store encryption key is not configured")

    async def resolve(self, **_kwargs) -> SecretStr:
        raise CredentialUnavailable("credential store encryption key is not configured")

    async def revoke(self, **_kwargs) -> None:
        return None

    async def revoke_subject(self, **_kwargs) -> None:
        return None


class EncryptedSqliteCredentialBroker:
    def __init__(
        self,
        *,
        database_path: Path,
        encryption_key: bytes,
        default_ttl_seconds: int = 300,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if len(encryption_key) not in {16, 24, 32}:
            raise ValueError("credential encryption key must be 16, 24, or 32 bytes")
        self._database_path = Path(database_path)
        self._cipher = AESGCM(encryption_key)
        self._default_ttl = timedelta(seconds=default_ttl_seconds)
        self._now = now or (lambda: datetime.now(UTC))
        self._init_lock = asyncio.Lock()
        self._initialized = False

    async def issue(
        self,
        *,
        raw_token: SecretStr,
        subject_user_id: str,
        app_id: str,
        run_id: str,
        source_expires_at: datetime,
    ) -> CredentialGrant:
        await self._ensure_initialized()
        now = self._now()
        grant = CredentialGrant(
            credential_ref=new_id("cred"),
            subject_user_id=subject_user_id,
            app_id=app_id,
            run_id=run_id,
            created_at=now,
            expires_at=min(source_expires_at, now + self._default_ttl),
        )
        nonce = os.urandom(12)
        ciphertext = self._cipher.encrypt(
            nonce,
            raw_token.get_secret_value().encode("utf-8"),
            _credential_aad(grant),
        )
        await asyncio.to_thread(self._insert, grant, nonce, ciphertext)
        return grant

    async def resolve(
        self,
        *,
        credential_ref: str,
        subject_user_id: str,
        app_id: str,
        run_id: str,
    ) -> SecretStr:
        await self._ensure_initialized()
        row = await asyncio.to_thread(self._fetch, credential_ref)
        if row is None:
            raise CredentialUnavailable("credential not found")
        grant = CredentialGrant(
            credential_ref=row[0],
            subject_user_id=row[3],
            app_id=row[4],
            run_id=row[5],
            created_at=datetime.fromisoformat(row[6]),
            expires_at=datetime.fromisoformat(row[7]),
        )
        if (
            row[8] is not None
            or grant.subject_user_id != subject_user_id
            or grant.app_id != app_id
            or grant.run_id != run_id
            or grant.expires_at <= self._now()
        ):
            raise CredentialUnavailable("credential not found")
        try:
            plaintext = self._cipher.decrypt(row[1], row[2], _credential_aad(grant))
        except InvalidTag as exc:
            raise CredentialUnavailable("credential cannot be decrypted") from exc
        return SecretStr(plaintext.decode("utf-8"))

    async def revoke(self, *, credential_ref: str) -> None:
        await self._ensure_initialized()
        await asyncio.to_thread(
            self._mark_revoked,
            "credential_ref = ?",
            (credential_ref,),
        )

    async def revoke_subject(self, *, subject_user_id: str) -> None:
        await self._ensure_initialized()
        await asyncio.to_thread(
            self._mark_revoked,
            "subject_user_id = ?",
            (subject_user_id,),
        )

    async def _ensure_initialized(self) -> None:
        if self._initialized:
            return
        async with self._init_lock:
            if self._initialized:
                return
            await asyncio.to_thread(self._initialize)
            self._initialized = True

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._database_path, timeout=10)

    def _initialize(self) -> None:
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS agent_credentials (
                    credential_ref TEXT PRIMARY KEY,
                    token_ciphertext BLOB NOT NULL,
                    nonce BLOB NOT NULL,
                    subject_user_id TEXT NOT NULL,
                    app_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    revoked_at TEXT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_agent_credentials_subject "
                "ON agent_credentials(subject_user_id)"
            )

    def _insert(
        self,
        grant: CredentialGrant,
        nonce: bytes,
        ciphertext: bytes,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO agent_credentials (
                    credential_ref, token_ciphertext, nonce, subject_user_id,
                    app_id, run_id, created_at, expires_at, revoked_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    grant.credential_ref,
                    ciphertext,
                    nonce,
                    grant.subject_user_id,
                    grant.app_id,
                    grant.run_id,
                    grant.created_at.isoformat(),
                    grant.expires_at.isoformat(),
                ),
            )

    def _fetch(self, credential_ref: str):
        with self._connect() as connection:
            return connection.execute(
                """
                SELECT credential_ref, nonce, token_ciphertext, subject_user_id,
                       app_id, run_id, created_at, expires_at, revoked_at
                  FROM agent_credentials
                 WHERE credential_ref = ?
                """,
                (credential_ref,),
            ).fetchone()

    def _mark_revoked(self, where_clause: str, parameters: tuple[str, ...]) -> None:
        with self._connect() as connection:
            connection.execute(
                f"UPDATE agent_credentials SET revoked_at = ? WHERE {where_clause}",
                (self._now().isoformat(), *parameters),
            )


def _credential_aad(grant: CredentialGrant) -> bytes:
    return "\x1f".join(
        [
            grant.credential_ref,
            grant.subject_user_id,
            grant.app_id,
            grant.run_id,
            grant.expires_at.isoformat(),
        ]
    ).encode("utf-8")
