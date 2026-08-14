"""P2-2 model configuration service.

Manages simplified model provider config with encrypted API keys.
Several configs may be active in the public pool. Legacy execution must
have an unambiguous default; Agent releases select configs explicitly.
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol

from pydantic import SecretStr

from full_view_agent.application.errors import ResourceNotFound, RunStateConflict
from full_view_agent.application.session_run_service import new_id
from full_view_agent.domain.capability import (
    ConnectionTestResult,
    ModelConfig,
    ModelConfigMasked,
    ModelConfigWithKey,
)

if TYPE_CHECKING:
    from full_view_agent.application.model_config_repository import (
        ModelConfigSnapshot,
    )


class ModelConfigKeyStore(Protocol):
    """Encrypted storage for model API keys, separate from config metadata."""

    async def store_key(
        self, *, config_id: str, api_key: SecretStr
    ) -> None: ...
    async def resolve_key(self, *, config_id: str) -> SecretStr: ...
    async def delete_key(self, *, config_id: str) -> None: ...

    async def resolve_key_material(
        self, *, config_id: str
    ) -> tuple[bytes, bytes]:
        """Return the raw (ciphertext, nonce) for ``config_id``.

        Used to capture an immutable snapshot of the encrypted key at
        binding time. The plaintext is never materialised. Default
        implementation raises — only stores that keep the key material
        on durable storage (e.g. Postgres) support snapshot capture.
        """
        ...


class ModelConfigRepository(Protocol):
    async def save(self, config: ModelConfig) -> None: ...
    async def get(self, config_id: str) -> ModelConfig | None: ...
    async def list_all(self) -> list[ModelConfig]: ...
    async def delete(self, config_id: str) -> None: ...
    async def disable_all(self) -> None: ...


class InMemoryModelConfigKeyStore:
    """In-memory key store for testing.  NOT for production."""

    def __init__(self) -> None:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        self._keys: dict[str, SecretStr] = {}
        self._cipher = AESGCM(AESGCM.generate_key(bit_length=256))
        self._materials: dict[str, tuple[bytes, bytes]] = {}
        self._lock = asyncio.Lock()

    async def store_key(
        self, *, config_id: str, api_key: SecretStr
    ) -> None:
        nonce = os.urandom(12)
        ciphertext = self._cipher.encrypt(
            nonce,
            api_key.get_secret_value().encode("utf-8"),
            config_id.encode("utf-8"),
        )
        async with self._lock:
            self._keys[config_id] = api_key
            self._materials[config_id] = (ciphertext, nonce)

    async def resolve_key(self, *, config_id: str) -> SecretStr:
        key = self._keys.get(config_id)
        if key is None:
            raise ResourceNotFound(f"api key for config {config_id} not found")
        return key

    async def delete_key(self, *, config_id: str) -> None:
        async with self._lock:
            self._keys.pop(config_id, None)
            self._materials.pop(config_id, None)

    async def resolve_key_material(
        self, *, config_id: str
    ) -> tuple[bytes, bytes]:
        """Return process-local encrypted material for immutable dev snapshots.

        The generated encryption key is intentionally ephemeral, so this store
        supports development and tests only. Production restart durability is
        provided by the database-backed key store.
        """
        async with self._lock:
            material = self._materials.get(config_id)
        if material is None:
            raise ResourceNotFound(f"api key for config {config_id} not found")
        return material


class EncryptedModelConfigKeyStore:
    """AES-GCM encrypted key store using the existing credential pattern."""

    def __init__(self, *, encryption_key: bytes) -> None:

        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        if len(encryption_key) not in {16, 24, 32}:
            raise ValueError("encryption key must be 16, 24, or 32 bytes")
        self._cipher = AESGCM(encryption_key)
        self._store: dict[str, tuple[bytes, bytes]] = {}
        self._lock = asyncio.Lock()

    async def store_key(
        self, *, config_id: str, api_key: SecretStr
    ) -> None:
        nonce = os.urandom(12)
        ciphertext = self._cipher.encrypt(
            nonce,
            api_key.get_secret_value().encode("utf-8"),
            config_id.encode("utf-8"),
        )
        async with self._lock:
            self._store[config_id] = (nonce, ciphertext)

    async def resolve_key(self, *, config_id: str) -> SecretStr:
        stored = self._store.get(config_id)
        if stored is None:
            raise ResourceNotFound(f"api key for config {config_id} not found")
        nonce, ciphertext = stored
        from cryptography.exceptions import InvalidTag

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
        async with self._lock:
            self._store.pop(config_id, None)

    async def resolve_key_material(
        self, *, config_id: str
    ) -> tuple[bytes, bytes]:
        async with self._lock:
            stored = self._store.get(config_id)
        if stored is None:
            raise ResourceNotFound(
                f"api key for config {config_id} not found"
            )
        nonce, ciphertext = stored
        return ciphertext, nonce


class ModelConfigService:
    """Manages model provider configurations with encrypted API keys."""

    def __init__(
        self,
        *,
        repository: ModelConfigRepository,
        key_store: ModelConfigKeyStore,
    ) -> None:
        self._repo = repository
        self._keys = key_store
        self._historical_snapshots: dict[tuple[str, int], ModelConfigSnapshot] = {}

    async def create_config(
        self,
        *,
        name: str,
        api_base_url: str,
        api_key: str,
        model_name: str,
        protocol: str = "openai_compatible",
        timeout_seconds: int = 60,
        max_output_tokens: int = 32000,
        max_retries: int = 1,
        notes: str = "",
        created_by: str = "system",
    ) -> ModelConfigMasked:
        config = ModelConfig(
            config_id=new_id("mconf"),
            name=name,
            api_base_url=api_base_url,
            model_name=model_name,
            protocol=protocol,
            timeout_seconds=timeout_seconds,
            max_output_tokens=max_output_tokens,
            max_retries=max_retries,
            is_enabled=False,
            notes=notes,
            created_by=created_by,
        )
        await self._repo.save(config)
        await self._keys.store_key(
            config_id=config.config_id, api_key=SecretStr(api_key)
        )
        return _mask_config(config)

    async def get_config(self, config_id: str) -> ModelConfigMasked:
        config = await self._repo.get(config_id)
        if config is None:
            raise ResourceNotFound("model config not found")
        return _mask_config(config)

    async def list_configs(self) -> list[ModelConfigMasked]:
        configs = await self._repo.list_all()
        return [_mask_config(c) for c in configs]

    async def update_config(
        self,
        *,
        config_id: str,
        updated_by: str,
        name: str | None = None,
        api_base_url: str | None = None,
        api_key: str | None = None,
        model_name: str | None = None,
        protocol: str | None = None,
        timeout_seconds: int | None = None,
        max_output_tokens: int | None = None,
        max_retries: int | None = None,
        notes: str | None = None,
    ) -> ModelConfigMasked:
        config = await self._repo.get(config_id)
        if config is None:
            raise ResourceNotFound("model config not found")
        previous_snapshot = await self.capture_snapshot_by_id(
            config.config_id, config.version
        )
        if previous_snapshot is not None:
            self._historical_snapshots.setdefault(
                (config.config_id, config.version), previous_snapshot
            )
        updates: dict[str, object] = {
            "updated_at": datetime.now(UTC),
            "version": config.version + 1,
        }
        if name is not None:
            updates["name"] = name
        if api_base_url is not None:
            updates["api_base_url"] = api_base_url
        if model_name is not None:
            updates["model_name"] = model_name
        if protocol is not None:
            updates["protocol"] = protocol
        if timeout_seconds is not None:
            updates["timeout_seconds"] = timeout_seconds
        if max_output_tokens is not None:
            updates["max_output_tokens"] = max_output_tokens
        if max_retries is not None:
            updates["max_retries"] = max_retries
        if notes is not None:
            updates["notes"] = notes
        updated = config.model_copy(update=updates)
        await self._repo.save(updated)
        if api_key is not None:
            await self._keys.store_key(
                config_id=config_id, api_key=SecretStr(api_key)
            )
        return _mask_config(updated)

    async def enable_config(self, *, config_id: str) -> None:
        config = await self._repo.get(config_id)
        if config is None:
            raise ResourceNotFound("model config not found")
        if config.is_enabled:
            return
        updated = config.model_copy(
            update={"is_enabled": True, "updated_at": datetime.now(UTC)}
        )
        await self._repo.save(updated)

    async def disable_config(self, *, config_id: str) -> None:
        config = await self._repo.get(config_id)
        if config is None:
            raise ResourceNotFound("model config not found")
        if not config.is_enabled:
            return
        updated = config.model_copy(
            update={
                "is_enabled": False,
                "updated_at": datetime.now(UTC),
            }
        )
        await self._repo.save(updated)

    async def delete_config(self, *, config_id: str) -> None:
        config = await self._repo.get(config_id)
        if config is None:
            raise ResourceNotFound("model config not found")
        if config.is_enabled:
            raise RunStateConflict("cannot delete an enabled config; disable first")
        await self._repo.delete(config_id)
        await self._keys.delete_key(config_id=config_id)

    async def test_connection(
        self, *, config_id: str
    ) -> ConnectionTestResult:
        config = await self._repo.get(config_id)
        if config is None:
            raise ResourceNotFound("model config not found")
        api_key = await self._keys.resolve_key(config_id=config_id)
        try:
            import httpx

            start = datetime.now(UTC)
            async with httpx.AsyncClient(timeout=config.timeout_seconds) as client:
                response = await client.post(
                    f"{config.api_base_url.rstrip('/')}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {api_key.get_secret_value()}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": config.model_name,
                        "messages": [{"role": "user", "content": "ping"}],
                        "max_tokens": 1,
                    },
                )
                latency = int(
                    (datetime.now(UTC) - start).total_seconds() * 1000
                )
            if response.status_code < 400:
                return ConnectionTestResult(
                    success=True,
                    latency_ms=latency,
                    model_responded=config.model_name,
                )
            error_body = response.text[:200] if response.text else ""
            return ConnectionTestResult(
                success=False,
                latency_ms=latency,
                error_code=f"http_{response.status_code}",
                error_message=error_body,
            )
        except Exception as exc:
            return ConnectionTestResult(
                success=False,
                error_code="connection_failed",
                error_message=str(exc)[:200],
            )

    async def resolve_for_runtime(
        self,
    ) -> ModelConfigWithKey | None:
        """Resolve the currently enabled config with plaintext key.
        Only for runtime use; never expose via API."""
        configs = await self._repo.list_all()
        enabled_configs = [config for config in configs if config.is_enabled]
        if not enabled_configs:
            return None
        if len(enabled_configs) != 1:
            raise RunStateConflict(
                "legacy runtime requires an explicit default model"
            )
        enabled = enabled_configs[0]
        api_key = await self._keys.resolve_key(config_id=enabled.config_id)
        return ModelConfigWithKey(
            config_id=enabled.config_id,
            name=enabled.name,
            api_base_url=enabled.api_base_url,
            api_key_secret=api_key.get_secret_value(),
            model_name=enabled.model_name,
            protocol=enabled.protocol,
            timeout_seconds=enabled.timeout_seconds,
            max_output_tokens=enabled.max_output_tokens,
            max_retries=enabled.max_retries,
            is_enabled=enabled.is_enabled,
        )

    async def load_by_id(
        self, config_id: str
    ) -> ModelConfigWithKey | None:
        """Resolve the config identified by ``config_id`` with its plaintext key.

        Used to resume an old Run with its original config, even when the
        runtime default has moved on. Returns ``None`` when the config no
        longer exists (e.g. deleted) or its key cannot be resolved — the
        caller is expected to fail closed rather than silently substitute
        a different config.

        Note: this reads the *current* row for ``config_id`` — if the
        config has been overwritten since the Run was bound, the returned
        metadata reflects the new version. Callers that need the exact
        version a Run bound to should go through the binding repository
        (``RunBoundModelPlannerFactory``) instead.
        """
        config = await self._repo.get(config_id)
        if config is None:
            return None
        try:
            api_key = await self._keys.resolve_key(config_id=config.config_id)
        except ResourceNotFound:
            return None
        return ModelConfigWithKey(
            config_id=config.config_id,
            name=config.name,
            api_base_url=config.api_base_url,
            api_key_secret=api_key.get_secret_value(),
            model_name=config.model_name,
            protocol=config.protocol,
            timeout_seconds=config.timeout_seconds,
            max_output_tokens=config.max_output_tokens,
            max_retries=config.max_retries,
            is_enabled=config.is_enabled,
        )

    async def capture_snapshot_for_runtime(
        self,
    ) -> ModelConfigSnapshot | None:
        """Capture the currently enabled config as an immutable snapshot.

        The snapshot includes the encrypted key material (ciphertext +
        nonce) from the source row, so later key rotation on the source
        row does not affect already-bound Runs. Returns ``None`` if no
        config is currently enabled, or if the key store does not expose
        its raw ciphertext (``resolve_key_material`` unsupported).

        The plaintext key is NEVER materialised here — only the
        ciphertext captured from the source row.
        """
        from full_view_agent.application.model_config_repository import (
            ModelConfigSnapshot,
        )
        configs = await self._repo.list_all()
        enabled_configs = [config for config in configs if config.is_enabled]
        if not enabled_configs:
            return None
        if len(enabled_configs) != 1:
            raise RunStateConflict(
                "legacy runtime requires an explicit default model"
            )
        enabled = enabled_configs[0]
        try:
            ciphertext, nonce = await self._keys.resolve_key_material(
                config_id=enabled.config_id
            )
        except ResourceNotFound:
            # Key store does not support raw material capture (e.g. the
            # in-memory store). Without durable ciphertext we cannot
            # produce a snapshot that survives source-row rotation —
            # fail closed rather than silently bind to a non-durable
            # snapshot.
            return None
        return ModelConfigSnapshot(
            config_id=enabled.config_id,
            config_version=enabled.version,
            name=enabled.name,
            api_base_url=enabled.api_base_url,
            model_name=enabled.model_name,
            protocol=enabled.protocol,
            timeout_seconds=enabled.timeout_seconds,
            max_output_tokens=enabled.max_output_tokens,
            max_retries=enabled.max_retries,
            api_key_ciphertext=ciphertext,
            api_key_nonce=nonce,
        )

    async def capture_snapshot_by_id(
        self, config_id: str, expected_version: int
    ) -> ModelConfigSnapshot | None:
        """Capture exactly the model version referenced by an Agent release."""
        from full_view_agent.application.model_config_repository import (
            ModelConfigSnapshot,
        )

        config = await self._repo.get(config_id)
        if config is None:
            return None
        if config.version == expected_version and not config.is_enabled:
            return None
        if config.version != expected_version:
            return self._historical_snapshots.get((config_id, expected_version))
        try:
            ciphertext, nonce = await self._keys.resolve_key_material(
                config_id=config_id
            )
        except ResourceNotFound:
            return None
        return ModelConfigSnapshot(
            config_id=config.config_id,
            config_version=config.version,
            name=config.name,
            api_base_url=config.api_base_url,
            model_name=config.model_name,
            protocol=config.protocol,
            timeout_seconds=config.timeout_seconds,
            max_output_tokens=config.max_output_tokens,
            max_retries=config.max_retries,
            api_key_ciphertext=ciphertext,
            api_key_nonce=nonce,
        )

    def materialise_snapshot(
        self, snapshot: ModelConfigSnapshot
    ) -> ModelConfigWithKey:
        """Decrypt ``snapshot``'s key material and return a ``ModelConfigWithKey``.

        The decryption key (a process-secret) is held by this service's
        ``ModelConfigKeyStore``; the snapshot only carries ciphertext.
        """
        plaintext_bytes = self._decrypt(
            ciphertext=snapshot.api_key_ciphertext,
            nonce=snapshot.api_key_nonce,
            associated_data=snapshot.config_id,
        )
        return snapshot.materialise_with_key(
            plaintext_key=plaintext_bytes.decode("utf-8")
        )

    def _decrypt(
        self, *, ciphertext: bytes, nonce: bytes, associated_data: str
    ) -> bytes:
        """Decrypt AES-GCM ciphertext using this service's key store cipher.

        ``InMemoryModelConfigKeyStore`` has no AES cipher — this helper
        raises for that case. ``EncryptedModelConfigKeyStore`` and
        ``PostgresModelConfigKeyStore`` both hold an AESGCM instance
        under ``_cipher``; we delegate to that instance so the
        encryption key (a process-secret) never leaves the key store.
        """
        cipher = getattr(self._keys, "_cipher", None)
        if cipher is None:
            raise ResourceNotFound(
                "key store does not support decryption (in-memory store)"
            )
        from cryptography.exceptions import InvalidTag

        try:
            return cipher.decrypt(nonce, ciphertext, associated_data.encode("utf-8"))
        except InvalidTag as exc:
            raise ResourceNotFound(
                "snapshot for config cannot be decrypted"
            ) from exc


def _mask_config(config: ModelConfig) -> ModelConfigMasked:
    return ModelConfigMasked(
        config_id=config.config_id,
        name=config.name,
        api_base_url=config.api_base_url,
        model_name=config.model_name,
        protocol=config.protocol,
        timeout_seconds=config.timeout_seconds,
        max_output_tokens=config.max_output_tokens,
        max_retries=config.max_retries,
        is_enabled=config.is_enabled,
        notes=config.notes,
        created_at=config.created_at,
        updated_at=config.updated_at,
        created_by=config.created_by,
        version=config.version,
    )
