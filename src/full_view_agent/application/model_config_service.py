"""P2-2 model configuration service.

Manages simplified model provider config with encrypted API keys.
Only one config can be enabled at a time.  API keys are never returned
in plaintext via the API.
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from typing import Protocol

from pydantic import SecretStr

from full_view_agent.application.errors import ResourceNotFound, RunStateConflict
from full_view_agent.application.session_run_service import new_id
from full_view_agent.domain.capability import (
    ConnectionTestResult,
    ModelConfig,
    ModelConfigMasked,
    ModelConfigWithKey,
)


class ModelConfigKeyStore(Protocol):
    """Encrypted storage for model API keys, separate from config metadata."""

    async def store_key(
        self, *, config_id: str, api_key: SecretStr
    ) -> None: ...
    async def resolve_key(self, *, config_id: str) -> SecretStr: ...
    async def delete_key(self, *, config_id: str) -> None: ...


class ModelConfigRepository(Protocol):
    async def save(self, config: ModelConfig) -> None: ...
    async def get(self, config_id: str) -> ModelConfig | None: ...
    async def list_all(self) -> list[ModelConfig]: ...
    async def delete(self, config_id: str) -> None: ...
    async def disable_all(self) -> None: ...


class InMemoryModelConfigKeyStore:
    """In-memory key store for testing.  NOT for production."""

    def __init__(self) -> None:
        self._keys: dict[str, SecretStr] = {}
        self._lock = asyncio.Lock()

    async def store_key(
        self, *, config_id: str, api_key: SecretStr
    ) -> None:
        async with self._lock:
            self._keys[config_id] = api_key

    async def resolve_key(self, *, config_id: str) -> SecretStr:
        key = self._keys.get(config_id)
        if key is None:
            raise ResourceNotFound(f"api key for config {config_id} not found")
        return key

    async def delete_key(self, *, config_id: str) -> None:
        async with self._lock:
            self._keys.pop(config_id, None)


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
        await self._repo.disable_all()
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
            update={"is_enabled": False, "updated_at": datetime.now(UTC)}
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
        enabled = next((c for c in configs if c.is_enabled), None)
        if enabled is None:
            return None
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
