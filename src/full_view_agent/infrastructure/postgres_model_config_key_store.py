"""P2-3 PostgreSQL Model Config Key Store.

Persistent encrypted key storage for model API keys using PostgreSQL.
Keys are stored in the model_configs table alongside the config metadata.
"""

from __future__ import annotations

import logging
import os

from pydantic import SecretStr

from full_view_agent.application.errors import ResourceNotFound

logger = logging.getLogger(__name__)


class PostgresModelConfigKeyStore:
    """PostgreSQL-backed encrypted key store for model API keys.

    Keys are stored in the model_configs table:
    - api_key_ciphertext: AES-GCM encrypted API key
    - api_key_nonce: AES-GCM nonce (12 bytes)

    This provides true restart persistence - keys survive process restarts
    and are loaded from the database on each startup.
    """

    def __init__(
        self,
        *,
        dsn: str,
        schema: str = "full_view_agent",
        encryption_key: bytes,
    ) -> None:
        """Initialize the PostgreSQL key store.

        Args:
            dsn: PostgreSQL connection string
            schema: Database schema name
            encryption_key: AES-GCM encryption key (16, 24, or 32 bytes)
        """
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        if len(encryption_key) not in {16, 24, 32}:
            raise ValueError("encryption key must be 16, 24, or 32 bytes")

        self._dsn = dsn
        self._schema = schema
        self._cipher = AESGCM(encryption_key)

    async def store_key(
        self, *, config_id: str, api_key: SecretStr
    ) -> None:
        """Store an encrypted API key in PostgreSQL.

        Args:
            config_id: The model config ID
            api_key: The API key to encrypt and store
        """
        import psycopg

        # Generate nonce and encrypt
        nonce = os.urandom(12)
        ciphertext = self._cipher.encrypt(
            nonce,
            api_key.get_secret_value().encode("utf-8"),
            config_id.encode("utf-8"),
        )

        # Store in database
        async with await psycopg.AsyncConnection.connect(self._dsn) as conn:
            query = (
                f"UPDATE {self._schema}.model_configs"
                f" SET api_key_ciphertext = %s,"
                f" api_key_nonce = %s"
                f" WHERE config_id = %s"
            )
            cur = await conn.execute(query, (ciphertext, nonce, config_id))  # pyright: ignore[reportArgumentType]
            if cur.rowcount == 0:
                raise ResourceNotFound(
                    f"model config {config_id} not found"
                )

        logger.debug(f"Stored encrypted API key for config {config_id}")

    async def resolve_key(self, *, config_id: str) -> SecretStr:
        """Load and decrypt an API key from PostgreSQL.

        Args:
            config_id: The model config ID

        Returns:
            The decrypted API key

        Raises:
            ResourceNotFound: If config not found or key cannot be decrypted
        """
        import psycopg
        from cryptography.exceptions import InvalidTag

        async with await psycopg.AsyncConnection.connect(self._dsn) as conn:
            query = (
                f"SELECT api_key_ciphertext, api_key_nonce"
                f" FROM {self._schema}.model_configs"
                f" WHERE config_id = %s"
            )
            cur = await conn.execute(query, (config_id,))  # pyright: ignore[reportArgumentType]
            row = await cur.fetchone()

        if row is None:
            raise ResourceNotFound(
                f"api key for config {config_id} not found"
            )

        ciphertext, nonce = row

        if not ciphertext or not nonce:
            raise ResourceNotFound(
                f"api key for config {config_id} not found"
            )

        try:
            plaintext = self._cipher.decrypt(
                nonce, ciphertext, config_id.encode("utf-8")
            )
        except InvalidTag as exc:
            raise ResourceNotFound(
                f"api key for config {config_id} cannot be decrypted"
            ) from exc

        return SecretStr(plaintext.decode("utf-8"))

    async def delete_key(self, *, config_id: str) -> None:
        """Delete an API key from PostgreSQL.

        Args:
            config_id: The model config ID
        """
        import psycopg

        async with await psycopg.AsyncConnection.connect(self._dsn) as conn:
            query = (
                f"UPDATE {self._schema}.model_configs"
                f" SET api_key_ciphertext = ''::bytea,"
                f" api_key_nonce = ''::bytea"
                f" WHERE config_id = %s"
            )
            await conn.execute(query, (config_id,))  # pyright: ignore[reportArgumentType]

        logger.debug(f"Deleted API key for config {config_id}")

    async def resolve_key_material(
        self, *, config_id: str
    ) -> tuple[bytes, bytes]:
        """Return the raw (ciphertext, nonce) for ``config_id``.

        This is used to capture an immutable snapshot of the encrypted
        key at binding time — the plaintext is never materialised. The
        snapshot is tied to the current row's version; later key
        rotation writes new ciphertext to the source row but does not
        affect already-captured snapshots (they live in
        ``run_model_config_snapshots``).
        """
        import psycopg

        async with await psycopg.AsyncConnection.connect(self._dsn) as conn:
            query = (
                f"SELECT api_key_ciphertext, api_key_nonce"
                f" FROM {self._schema}.model_configs"
                f" WHERE config_id = %s"
            )
            cur = await conn.execute(query, (config_id,))  # pyright: ignore[reportArgumentType]
            row = await cur.fetchone()

        if row is None:
            raise ResourceNotFound(
                f"api key for config {config_id} not found"
            )

        ciphertext, nonce = row
        if not ciphertext or not nonce:
            raise ResourceNotFound(
                f"api key for config {config_id} not found"
            )
        return bytes(ciphertext), bytes(nonce)
