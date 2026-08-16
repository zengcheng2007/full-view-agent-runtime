# pyright: reportArgumentType=false, reportCallIssue=false
"""Run-scoped model configuration persistence.

Stores which model config (by ``config_id`` + ``config_version``) a Run
is bound to, so that:
1. The binding survives process restart (when backed by DB).
2. The plaintext API key is never persisted — only the config_id reference.
3. Old Runs keep their original config even after the runtime default changes.

The repository stores only ``config_id`` (a stable identifier) and the
``config_version`` the Run bound to, not the full ``ModelConfigWithKey``.
When the binding is loaded, the repository returns the immutable snapshot
row for that (config_id, config_version) — including the encrypted API
key material captured at snapshot time — so that subsequent overwrites
of the source ``model_configs`` row (higher version, new key) cannot
silently change what an already-bound Run sees. This is the
"fail-closed / immutable versions" guarantee: either the exact snapshot
row exists, or ``load_binding`` reports nothing and the Run fails
rather than silently picking up a newer config.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

from full_view_agent.domain.capability import ModelConfigWithKey, ModelReasoningCapability

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ModelConfigSnapshot:
    """Immutable view of a (config_id, config_version) at binding time.

    Contains the encrypted API key material captured when the Run first
    bound to this config. The plaintext key is materialised on demand
    by the key store; ``api_key_ciphertext`` / ``api_key_nonce`` are the
    AES-GCM outputs, never the plaintext.
    """

    config_id: str
    config_version: int
    name: str
    api_base_url: str
    model_name: str
    protocol: str
    timeout_seconds: int
    max_output_tokens: int
    max_retries: int
    api_key_ciphertext: bytes
    api_key_nonce: bytes
    reasoning_capability: ModelReasoningCapability = field(
        default_factory=ModelReasoningCapability
    )

    def materialise_with_key(self, *, plaintext_key: str) -> ModelConfigWithKey:
        """Build a ``ModelConfigWithKey`` using an already-resolved plaintext key.

        The key is passed in rather than decrypted here so that the
        decryption key (a process-secret) stays out of this dataclass.
        """
        return ModelConfigWithKey(
            config_id=self.config_id,
            name=self.name,
            api_base_url=self.api_base_url,
            api_key_secret=plaintext_key,
            model_name=self.model_name,
            protocol=self.protocol,
            timeout_seconds=self.timeout_seconds,
            max_output_tokens=self.max_output_tokens,
            max_retries=self.max_retries,
            reasoning_capability=self.reasoning_capability,
            is_enabled=True,
        )


@dataclass(frozen=True)
class RunModelBinding:
    """(config_id, config_version) a Run is bound to."""

    run_id: str
    config_id: str
    config_version: int
    bound_at: datetime


class RunModelBindingRepository(Protocol):
    """Persist Run -> model config bindings."""

    async def store_binding(
        self,
        run_id: str,
        snapshot: ModelConfigSnapshot,
    ) -> RunModelBinding:
        """Record that ``run_id`` is bound to ``snapshot``.

        Atomic first-write-wins: if a binding already exists for
        ``run_id``, it is NOT overwritten. The snapshot row for
        (config_id, config_version) is inserted idempotently so that
        repeated calls for the same (run, config, version) do not fail.

        Returns the winning binding — either the one just inserted or
        the one already present — so callers can observe which
        (config_id, config_version) the Run is authoritatively bound
        to even under concurrent inserts.
        """
        ...

    async def load_binding(self, run_id: str) -> RunModelBinding | None:
        """Return the binding for ``run_id``, or None if not bound."""
        ...

    async def load_snapshot(
        self, *, config_id: str, config_version: int
    ) -> ModelConfigSnapshot | None:
        """Return the immutable snapshot for (config_id, config_version).

        Returns None when no such snapshot exists — the caller must fail
        closed rather than substitute a newer config.
        """
        ...

    async def store_snapshot(self, snapshot: ModelConfigSnapshot) -> None: ...

    async def promote_binding(
        self,
        run_id: str,
        snapshot: ModelConfigSnapshot,
    ) -> RunModelBinding:
        """Persist the provider that successfully replaced a failed candidate."""
        ...


class InMemoryRunModelBindingRepository:
    """In-memory implementation for tests and single-process deployments.

    This does NOT survive process restart. A production deployment should
    use a DB-backed implementation (PostgreSQL tables
    ``run_model_bindings`` and ``run_model_config_snapshots``).
    """

    def __init__(self) -> None:
        self._bindings: dict[str, RunModelBinding] = {}
        self._snapshots: dict[tuple[str, int], ModelConfigSnapshot] = {}

    async def store_binding(
        self,
        run_id: str,
        snapshot: ModelConfigSnapshot,
    ) -> RunModelBinding:
        existing = self._bindings.get(run_id)
        if existing is not None:
            return existing
        binding = RunModelBinding(
            run_id=run_id,
            config_id=snapshot.config_id,
            config_version=snapshot.config_version,
            bound_at=datetime.now(UTC),
        )
        self._bindings[run_id] = binding
        key = (snapshot.config_id, snapshot.config_version)
        self._snapshots.setdefault(key, snapshot)
        return binding

    async def load_binding(self, run_id: str) -> RunModelBinding | None:
        return self._bindings.get(run_id)

    async def load_snapshot(
        self, *, config_id: str, config_version: int
    ) -> ModelConfigSnapshot | None:
        return self._snapshots.get((config_id, config_version))

    async def store_snapshot(self, snapshot: ModelConfigSnapshot) -> None:
        self._snapshots.setdefault(
            (snapshot.config_id, snapshot.config_version), snapshot
        )

    async def promote_binding(
        self,
        run_id: str,
        snapshot: ModelConfigSnapshot,
    ) -> RunModelBinding:
        await self.store_snapshot(snapshot)
        binding = RunModelBinding(
            run_id=run_id,
            config_id=snapshot.config_id,
            config_version=snapshot.config_version,
            bound_at=datetime.now(UTC),
        )
        self._bindings[run_id] = binding
        return binding


class PostgresRunModelBindingRepository:
    """PostgreSQL-backed Run -> model config binding repository.

    Uses tables ``run_model_bindings`` and ``run_model_config_snapshots``
    (migration V012). All writes are first-write-wins per (run_id) or
    idempotent per (config_id, config_version). The plaintext API key is
    never written; only the ciphertext captured at snapshot time.
    """

    def __init__(
        self,
        *,
        dsn: str,
        schema: str = "full_view_agent",
    ) -> None:
        self._dsn = dsn
        self._schema = schema

    async def store_binding(
        self,
        run_id: str,
        snapshot: ModelConfigSnapshot,
    ) -> RunModelBinding:
        import psycopg

        async with await psycopg.AsyncConnection.connect(self._dsn) as conn:
            # 1. Insert the snapshot row idempotently. The ON CONFLICT
            #    DO NOTHING preserves the original ciphertext / nonce
            #    even if the source row's key has since been rotated.
            await conn.execute(
                f"""
                INSERT INTO {self._schema}.run_model_config_snapshots (
                    config_id, config_version,
                    name, api_base_url, model_name, protocol,
                    timeout_seconds, max_output_tokens, max_retries,
                    reasoning_capability,
                    api_key_ciphertext, api_key_nonce
                ) VALUES (
                    %(config_id)s, %(config_version)s,
                    %(name)s, %(api_base_url)s, %(model_name)s, %(protocol)s,
                    %(timeout_seconds)s, %(max_output_tokens)s, %(max_retries)s,
                    %(reasoning_capability)s::jsonb,
                    %(api_key_ciphertext)s, %(api_key_nonce)s
                )
                ON CONFLICT (config_id, config_version) DO NOTHING
                """,
                {
                    "config_id": snapshot.config_id,
                    "config_version": snapshot.config_version,
                    "name": snapshot.name,
                    "api_base_url": snapshot.api_base_url,
                    "model_name": snapshot.model_name,
                    "protocol": snapshot.protocol,
                    "timeout_seconds": snapshot.timeout_seconds,
                    "max_output_tokens": snapshot.max_output_tokens,
                    "max_retries": snapshot.max_retries,
                    "reasoning_capability": snapshot.reasoning_capability.model_dump_json(),
                    "api_key_ciphertext": snapshot.api_key_ciphertext,
                    "api_key_nonce": snapshot.api_key_nonce,
                },
            )
            # 2. Insert the binding first-write-wins per run_id. Use
            #    RETURNING so we can tell whether we won the race.
            cursor = await conn.execute(
                f"""
                INSERT INTO {self._schema}.run_model_bindings (
                    run_id, config_id, config_version
                ) VALUES (
                    %(run_id)s, %(config_id)s, %(config_version)s
                )
                ON CONFLICT (run_id) DO NOTHING
                RETURNING run_id, config_id, config_version, bound_at
                """,
                {
                    "run_id": run_id,
                    "config_id": snapshot.config_id,
                    "config_version": snapshot.config_version,
                },
            )
            row = await cursor.fetchone()
        if row is not None:
            return RunModelBinding(
                run_id=row[0],
                config_id=row[1],
                config_version=row[2],
                bound_at=row[3],
            )
        # Lost the race — read the existing binding so the caller can
        # observe the authoritative (config_id, config_version).
        winner = await self.load_binding(run_id)
        if winner is None:  # pragma: no cover - race with delete
            return RunModelBinding(
                run_id=run_id,
                config_id=snapshot.config_id,
                config_version=snapshot.config_version,
                bound_at=datetime.now(UTC),
            )
        return winner

    async def store_snapshot(self, snapshot: ModelConfigSnapshot) -> None:
        import psycopg

        async with await psycopg.AsyncConnection.connect(self._dsn) as conn:
            await conn.execute(
                f"""
                INSERT INTO {self._schema}.run_model_config_snapshots (
                    config_id, config_version,
                    name, api_base_url, model_name, protocol,
                    timeout_seconds, max_output_tokens, max_retries,
                    reasoning_capability,
                    api_key_ciphertext, api_key_nonce
                ) VALUES (
                    %(config_id)s, %(config_version)s,
                    %(name)s, %(api_base_url)s, %(model_name)s, %(protocol)s,
                    %(timeout_seconds)s, %(max_output_tokens)s, %(max_retries)s,
                    %(reasoning_capability)s::jsonb,
                    %(api_key_ciphertext)s, %(api_key_nonce)s
                )
                ON CONFLICT (config_id, config_version) DO NOTHING
                """,
                {
                    **snapshot.__dict__,
                    "reasoning_capability": snapshot.reasoning_capability.model_dump_json(),
                },
            )

    async def promote_binding(
        self,
        run_id: str,
        snapshot: ModelConfigSnapshot,
    ) -> RunModelBinding:
        await self.store_snapshot(snapshot)
        import psycopg

        async with await psycopg.AsyncConnection.connect(self._dsn) as conn:
            cursor = await conn.execute(
                f"""
                UPDATE {self._schema}.run_model_bindings
                   SET config_id = %(config_id)s,
                       config_version = %(config_version)s,
                       bound_at = CURRENT_TIMESTAMP
                 WHERE run_id = %(run_id)s
                RETURNING run_id, config_id, config_version, bound_at
                """,
                {
                    "run_id": run_id,
                    "config_id": snapshot.config_id,
                    "config_version": snapshot.config_version,
                },
            )
            row = await cursor.fetchone()
        if row is None:
            return await self.store_binding(run_id, snapshot)
        return RunModelBinding(
            run_id=row[0],
            config_id=row[1],
            config_version=row[2],
            bound_at=row[3],
        )

    async def load_binding(self, run_id: str) -> RunModelBinding | None:
        import psycopg

        async with await psycopg.AsyncConnection.connect(self._dsn) as conn:
            cursor = await conn.execute(
                f"""
                SELECT run_id, config_id, config_version, bound_at
                  FROM {self._schema}.run_model_bindings
                 WHERE run_id = %s
                """,
                (run_id,),
            )
            row = await cursor.fetchone()
        if row is None:
            return None
        return RunModelBinding(
            run_id=row[0],
            config_id=row[1],
            config_version=row[2],
            bound_at=row[3],
        )

    async def load_snapshot(
        self, *, config_id: str, config_version: int
    ) -> ModelConfigSnapshot | None:
        import psycopg

        async with await psycopg.AsyncConnection.connect(self._dsn) as conn:
            cursor = await conn.execute(
                f"""
                SELECT config_id, config_version,
                       name, api_base_url, model_name, protocol,
                       timeout_seconds, max_output_tokens, max_retries,
                       reasoning_capability,
                       api_key_ciphertext, api_key_nonce
                  FROM {self._schema}.run_model_config_snapshots
                 WHERE config_id = %s AND config_version = %s
                """,
                (config_id, config_version),
            )
            row = await cursor.fetchone()
        if row is None:
            return None
        return ModelConfigSnapshot(
            config_id=row[0],
            config_version=row[1],
            name=row[2],
            api_base_url=row[3],
            model_name=row[4],
            protocol=row[5],
            timeout_seconds=row[6],
            max_output_tokens=row[7],
            max_retries=row[8],
            reasoning_capability=ModelReasoningCapability.model_validate(row[9] or {}),
            api_key_ciphertext=bytes(row[10]) if row[10] is not None else b"",
            api_key_nonce=bytes(row[11]) if row[11] is not None else b"",
        )
