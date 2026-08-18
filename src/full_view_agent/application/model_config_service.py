"""P2-2 model configuration service.

Manages simplified model provider config with encrypted API keys.
Several configs may be active in the public pool. Legacy execution must
have an unambiguous default; Agent releases select configs explicitly.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import os
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal, Protocol, cast
from urllib.parse import urlsplit

from pydantic import JsonValue, SecretStr

from full_view_agent.application.errors import ResourceNotFound, RunStateConflict
from full_view_agent.application.model_provider import (
    ModelInferenceOptions,
    ModelMessage,
    ModelRequest,
    ModelToolDefinition,
)
from full_view_agent.application.session_run_service import new_id
from full_view_agent.domain.capability import (
    ConnectionTestResult,
    ModelAuditAction,
    ModelAuditEvent,
    ModelCapabilityDeclaration,
    ModelConfig,
    ModelConfigMasked,
    ModelConfigVersion,
    ModelConfigWithKey,
    ModelParameterProfiles,
    ModelProviderType,
    ModelReasoningCapability,
    ModelTestKind,
    ModelTestRecord,
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
    async def save_version(self, version: ModelConfigVersion) -> None: ...
    async def save_version_key_material(
        self, *, config_id: str, version: int, ciphertext: bytes, nonce: bytes
    ) -> None: ...
    async def load_version_key_material(
        self, *, config_id: str, version: int
    ) -> tuple[bytes, bytes] | None: ...
    async def list_versions(self, config_id: str) -> list[ModelConfigVersion]: ...
    async def save_test_record(self, record: ModelTestRecord) -> None: ...
    async def list_test_records(self, config_id: str) -> list[ModelTestRecord]: ...
    async def save_audit_event(self, event: ModelAuditEvent) -> None: ...
    async def list_audit_events(self, config_id: str) -> list[ModelAuditEvent]: ...


class AgentReleaseModelReferenceReader(Protocol):
    async def is_model_referenced(self, config_id: str) -> bool: ...


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
        agent_release_reader: AgentReleaseModelReferenceReader | None = None,
    ) -> None:
        self._repo = repository
        self._keys = key_store
        self._agent_release_reader = agent_release_reader
        self._historical_snapshots: dict[tuple[str, int], ModelConfigSnapshot] = {}

    async def _save_version(
        self, config: ModelConfig, *, replace_draft: bool = False
    ) -> None:
        method = getattr(self._repo, "save_version", None)
        if method is None:
            return
        await method(
            ModelConfigVersion(
                config_id=config.config_id,
                version=config.version,
                lifecycle=config.lifecycle,
                config=_mask_config(config),
                created_by=config.updated_by,
                created_at=config.updated_at,
            )
        )
        save_material = getattr(self._repo, "save_version_key_material", None)
        if save_material is not None:
            ciphertext, nonce = await self._keys.resolve_key_material(
                config_id=config.config_id
            )
            await save_material(
                config_id=config.config_id,
                version=config.version,
                ciphertext=ciphertext,
                nonce=nonce,
            )

    async def _save_audit(
        self,
        *,
        config: ModelConfig,
        action: ModelAuditAction,
        actor: str,
        reason: str,
        previous_etag: int | None,
        changed_fields: list[str],
    ) -> None:
        method = getattr(self._repo, "save_audit_event", None)
        if method is None:
            return
        await method(
            ModelAuditEvent(
                event_id=new_id("maudit"),
                config_id=config.config_id,
                version=config.version,
                action=action,
                actor=actor,
                reason=reason,
                previous_etag=previous_etag,
                new_etag=config.etag,
                changed_fields=changed_fields,
            )
        )

    async def create_config(
        self,
        *,
        name: str,
        api_base_url: str,
        api_key: str,
        model_name: str,
        protocol: str = "openai_compatible",
        provider_type: ModelProviderType = "openai_compatible",
        timeout_seconds: int = 60,
        max_output_tokens: int = 32000,
        max_retries: int = 1,
        notes: str = "",
        reasoning_capability: ModelReasoningCapability | None = None,
        capabilities: ModelCapabilityDeclaration | None = None,
        parameter_profiles: ModelParameterProfiles | None = None,
        created_by: str = "system",
    ) -> ModelConfigMasked:
        _validate_model_endpoint(api_base_url)
        declared_reasoning = reasoning_capability or ModelReasoningCapability()
        config = ModelConfig(
            config_id=new_id("mconf"),
            name=name,
            api_base_url=api_base_url,
            model_name=model_name,
            protocol=protocol,
            provider_type=provider_type,
            timeout_seconds=timeout_seconds,
            max_output_tokens=max_output_tokens,
            max_retries=max_retries,
            reasoning_capability=declared_reasoning,
            capabilities=capabilities or ModelCapabilityDeclaration(
                reasoning=declared_reasoning
            ),
            parameter_profiles=parameter_profiles or ModelParameterProfiles(),
            lifecycle="draft",
            is_enabled=False,
            notes=notes,
            created_by=created_by,
            updated_by=created_by,
            etag=1,
        )
        await self._repo.save(config)
        await self._keys.store_key(
            config_id=config.config_id, api_key=SecretStr(api_key)
        )
        await self._save_version(config)
        await self._save_audit(
            config=config,
            action="create",
            actor=created_by,
            reason="create model resource",
            previous_etag=None,
            changed_fields=["*"],
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

    async def list_versions(self, config_id: str) -> list[ModelConfigVersion]:
        if await self._repo.get(config_id) is None:
            raise ResourceNotFound("model config not found")
        method = getattr(self._repo, "list_versions", None)
        if method is None:
            return []
        return await method(config_id)

    async def get_version(
        self, config_id: str, version: int
    ) -> ModelConfigVersion:
        item = next(
            (entry for entry in await self.list_versions(config_id) if entry.version == version),
            None,
        )
        if item is None:
            raise ResourceNotFound("model config version not found")
        return item

    async def diff_versions(
        self, config_id: str, version: int, against_version: int
    ) -> dict[str, object]:
        target = await self.get_version(config_id, version)
        baseline = await self.get_version(config_id, against_version)
        left = baseline.config.model_dump(mode="json", exclude={"api_key_masked"})
        right = target.config.model_dump(mode="json", exclude={"api_key_masked"})
        fields = sorted(set(left) | set(right))
        changes = {
            field: {"from": left.get(field), "to": right.get(field)}
            for field in fields
            if left.get(field) != right.get(field)
        }
        return {
            "config_id": config_id,
            "from_version": against_version,
            "to_version": version,
            "changes": changes,
        }

    async def create_version(
        self,
        *,
        config_id: str,
        expected_etag: int,
        actor: str,
        reason: str,
        changes: dict[str, object] | None = None,
        from_version: int | None = None,
        api_key: str | None = None,
    ) -> ModelConfigMasked:
        current = await self._repo.get(config_id)
        if current is None:
            raise ResourceNotFound("model config not found")
        if current.etag != expected_etag:
            raise RunStateConflict("model config etag mismatch")
        if not reason.strip():
            raise RunStateConflict("new model version reason is required")
        historical_snapshot = await self.capture_snapshot_by_id(
            current.config_id, current.version
        )
        if historical_snapshot is not None:
            self._historical_snapshots.setdefault(
                (current.config_id, current.version), historical_snapshot
            )
        versions = await self.list_versions(config_id)
        source = current
        if from_version is not None:
            selected = next((v for v in versions if v.version == from_version), None)
            if selected is None:
                raise ResourceNotFound("model config version not found")
            source = ModelConfig.model_validate(
                selected.config.model_dump(exclude={"api_key_masked"})
            )
        allowed = {
            "name", "api_base_url", "model_name", "protocol", "provider_type",
            "timeout_seconds", "max_output_tokens", "max_retries", "notes",
            "reasoning_capability", "capabilities", "parameter_profiles",
        }
        requested = changes or {}
        unknown = set(requested) - allowed
        if unknown:
            raise RunStateConflict(f"unsupported model version fields: {sorted(unknown)}")
        if "api_base_url" in requested:
            _validate_model_endpoint(str(requested["api_base_url"]))
        connection_changed = any(
            field in requested and requested[field] != getattr(source, field)
            for field in ("api_base_url", "provider_type")
        )
        if connection_changed and api_key is None:
            raise RunStateConflict(
                "changing model endpoint or provider requires a new api key"
            )
        next_version = max((item.version for item in versions), default=current.version) + 1
        draft = source.model_copy(
            update={
                **requested,
                "config_id": config_id,
                "version": next_version,
                "lifecycle": "draft",
                "is_enabled": False,
                "legacy_default": False,
                "etag": current.etag + 1,
                "updated_by": actor,
                "updated_at": datetime.now(UTC),
            }
        )
        # Re-validate provider option whitelists after applying dictionary changes.
        draft = ModelConfig.model_validate(draft.model_dump())
        if api_key is not None:
            await self._keys.store_key(
                config_id=config_id, api_key=SecretStr(api_key)
            )
        await self._repo.save(draft)
        await self._save_version(draft)
        await self._save_audit(
            config=draft,
            action="new_version",
            actor=actor,
            reason=reason,
            previous_etag=current.etag,
            changed_fields=sorted(requested),
        )
        return _mask_config(draft)

    async def rollback_config(
        self,
        *,
        config_id: str,
        to_version: int,
        expected_etag: int,
        actor: str,
        reason: str,
    ) -> ModelConfigMasked:
        target = await self.get_version(config_id, to_version)
        draft = await self.create_version(
            config_id=config_id,
            expected_etag=expected_etag,
            actor=actor,
            reason=reason,
            from_version=target.version,
        )
        current = await self._repo.get(config_id)
        assert current is not None
        await self._save_audit(
            config=current,
            action="rollback",
            actor=actor,
            reason=reason,
            previous_etag=expected_etag,
            changed_fields=["version"],
        )
        return draft

    async def list_audit_events(self, config_id: str) -> list[ModelAuditEvent]:
        if await self._repo.get(config_id) is None:
            raise ResourceNotFound("model config not found")
        method = getattr(self._repo, "list_audit_events", None)
        if method is None:
            return []
        return await method(config_id)

    async def update_config(
        self,
        *,
        config_id: str,
        updated_by: str,
        expected_etag: int | None = None,
        reason: str = "legacy update",
        name: str | None = None,
        api_base_url: str | None = None,
        api_key: str | None = None,
        model_name: str | None = None,
        protocol: str | None = None,
        provider_type: ModelProviderType | None = None,
        timeout_seconds: int | None = None,
        max_output_tokens: int | None = None,
        max_retries: int | None = None,
        notes: str | None = None,
        reasoning_capability: ModelReasoningCapability | None = None,
        capabilities: ModelCapabilityDeclaration | None = None,
        parameter_profiles: ModelParameterProfiles | None = None,
    ) -> ModelConfigMasked:
        config = await self._repo.get(config_id)
        if config is None:
            raise ResourceNotFound("model config not found")
        if expected_etag is not None and expected_etag != config.etag:
            raise RunStateConflict("model config etag mismatch")
        if not reason.strip():
            raise RunStateConflict("model config change reason is required")
        if config.lifecycle != "draft":
            raise RunStateConflict("published model versions are immutable")
        previous_snapshot = await self.capture_snapshot_by_id(
            config.config_id, config.version
        )
        if previous_snapshot is not None:
            self._historical_snapshots.setdefault(
                (config.config_id, config.version), previous_snapshot
            )
        updates: dict[str, object] = {
            "updated_at": datetime.now(UTC),
            "updated_by": updated_by,
            "etag": config.etag + 1,
        }
        if name is not None:
            updates["name"] = name
        if api_base_url is not None:
            _validate_model_endpoint(api_base_url)
            updates["api_base_url"] = api_base_url
        if model_name is not None:
            updates["model_name"] = model_name
        if protocol is not None:
            updates["protocol"] = protocol
        if provider_type is not None:
            updates["provider_type"] = provider_type
        if timeout_seconds is not None:
            updates["timeout_seconds"] = timeout_seconds
        if max_output_tokens is not None:
            updates["max_output_tokens"] = max_output_tokens
        if max_retries is not None:
            updates["max_retries"] = max_retries
        if notes is not None:
            updates["notes"] = notes
        if reasoning_capability is not None:
            updates["reasoning_capability"] = reasoning_capability
        if capabilities is not None:
            updates["capabilities"] = capabilities
        if parameter_profiles is not None:
            updates["parameter_profiles"] = parameter_profiles
        updated = config.model_copy(update=updates)
        await self._repo.save(updated)
        if api_key is not None:
            await self._keys.store_key(
                config_id=config_id, api_key=SecretStr(api_key)
            )
        await self._save_version(updated, replace_draft=True)
        await self._save_audit(
            config=updated,
            action="update",
            actor=updated_by,
            reason=reason,
            previous_etag=config.etag,
            changed_fields=sorted(updates),
        )
        return _mask_config(updated)

    async def enable_config(self, *, config_id: str) -> None:
        config = await self._repo.get(config_id)
        if config is None:
            raise ResourceNotFound("model config not found")
        records = await self.list_test_records(config_id)
        missing = sorted(_missing_required_tests(config, records))
        if missing:
            raise RunStateConflict(
                f"required model tests have not passed: {', '.join(missing)}"
            )
        if config.is_enabled and config.lifecycle == "published":
            return
        configs = await self._repo.list_all()
        has_legacy_default = any(item.legacy_default for item in configs)
        updated = config.model_copy(
            update={
                "is_enabled": True,
                "lifecycle": "published",
                # Deprecated compatibility operation: the first explicit
                # enable becomes the old runtime default. New publish does not.
                "legacy_default": not has_legacy_default,
                "updated_at": datetime.now(UTC),
                "etag": config.etag + 1,
            }
        )
        await self._repo.save(updated)
        await self._save_version(updated)

    async def get_legacy_default(self) -> ModelConfigMasked | None:
        defaults = [item for item in await self._repo.list_all() if item.legacy_default]
        if not defaults:
            return None
        if len(defaults) != 1:
            raise RunStateConflict("legacy runtime requires one explicit legacy default")
        return _mask_config(defaults[0])

    async def set_legacy_default(
        self,
        *,
        config_id: str,
        expected_etag: int,
        actor: str,
        reason: str,
    ) -> ModelConfigMasked:
        config = await self._repo.get(config_id)
        if config is None:
            raise ResourceNotFound("model config not found")
        if config.etag != expected_etag:
            raise RunStateConflict("model config etag mismatch")
        if config.lifecycle != "published":
            raise RunStateConflict("legacy default must be a published model")
        if not reason.strip():
            raise RunStateConflict("legacy default change reason is required")
        for item in await self._repo.list_all():
            if item.legacy_default and item.config_id != config_id:
                await self._repo.save(item.model_copy(update={"legacy_default": False}))
        updated = config.model_copy(
            update={
                "legacy_default": True,
                "etag": config.etag + 1,
                "updated_by": actor,
                "updated_at": datetime.now(UTC),
            }
        )
        await self._repo.save(updated)
        await self._save_audit(
            config=updated,
            action="set_legacy_default",
            actor=actor,
            reason=reason,
            previous_etag=config.etag,
            changed_fields=["legacy_default"],
        )
        return _mask_config(updated)

    async def disable_config(
        self,
        *,
        config_id: str,
        expected_etag: int | None = None,
        actor: str = "system",
        reason: str = "legacy disable",
    ) -> ModelConfigMasked:
        config = await self._repo.get(config_id)
        if config is None:
            raise ResourceNotFound("model config not found")
        if expected_etag is not None and config.etag != expected_etag:
            raise RunStateConflict("model config etag mismatch")
        if not reason.strip():
            raise RunStateConflict("model disable reason is required")
        if not config.is_enabled:
            return _mask_config(config)
        updated = config.model_copy(
            update={
                "is_enabled": False,
                "lifecycle": "disabled",
                "legacy_default": False,
                "updated_at": datetime.now(UTC),
                "etag": config.etag + 1,
                "updated_by": actor,
            }
        )
        await self._repo.save(updated)
        # The published version row is immutable. Disabled is resource-head
        # state, not a rewrite of the content that existing Runs pinned.
        await self._save_audit(
            config=updated,
            action="disable",
            actor=actor,
            reason=reason,
            previous_etag=config.etag,
            changed_fields=["lifecycle", "is_enabled", "legacy_default"],
        )
        return _mask_config(updated)

    async def record_test_result(
        self,
        *,
        config_id: str,
        kind: ModelTestKind,
        success: bool,
        tested_by: str,
        profile: Literal["fast", "standard", "deep"] | None = None,
        latency_ms: int | None = None,
        actual_parameters: dict[str, object] | None = None,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
        reasoning_tokens: int | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> ModelTestRecord:
        config = await self._repo.get(config_id)
        if config is None:
            raise ResourceNotFound("model config not found")
        record = ModelTestRecord(
            test_id=new_id("mtest"),
            config_id=config_id,
            version=config.version,
            kind=kind,
            profile=profile,
            success=success,
            latency_ms=latency_ms,
            actual_parameters=cast(
                dict[str, JsonValue], _redact_parameters(actual_parameters or {})
            ),
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            reasoning_tokens=reasoning_tokens,
            error_code=error_code,
            error_message=error_message,
            tested_by=tested_by,
        )
        method = getattr(self._repo, "save_test_record", None)
        if method is None:
            raise RunStateConflict("model test repository is unavailable")
        await method(record)
        audit_config = config
        changed_fields: list[str] = []
        records = await self.list_test_records(config_id)
        missing = _missing_required_tests(config, records)
        if config.lifecycle == "draft" and not missing:
            audit_config = config.model_copy(
                update={
                    "lifecycle": "tested",
                    "etag": config.etag + 1,
                    "updated_by": tested_by,
                    "updated_at": datetime.now(UTC),
                }
            )
            await self._repo.save(audit_config)
            await self._save_version(audit_config)
            changed_fields = ["lifecycle"]
        await self._save_audit(
            config=audit_config,
            action="test",
            actor=tested_by,
            reason=f"{kind} test {'passed' if success else 'failed'}",
            previous_etag=config.etag,
            changed_fields=changed_fields,
        )
        return record

    async def list_test_records(self, config_id: str) -> list[ModelTestRecord]:
        if await self._repo.get(config_id) is None:
            raise ResourceNotFound("model config not found")
        method = getattr(self._repo, "list_test_records", None)
        if method is None:
            return []
        return await method(config_id)

    async def publish_config(
        self,
        *,
        config_id: str,
        expected_etag: int,
        actor: str,
        reason: str,
    ) -> ModelConfigMasked:
        config = await self._repo.get(config_id)
        if config is None:
            raise ResourceNotFound("model config not found")
        if config.etag != expected_etag:
            raise RunStateConflict("model config etag mismatch")
        if not reason.strip():
            raise RunStateConflict("model publish reason is required")
        records = await self.list_test_records(config_id)
        missing = sorted(_missing_required_tests(config, records))
        if missing:
            raise RunStateConflict(
                f"required model tests have not passed: {', '.join(missing)}"
            )
        updated = config.model_copy(
            update={
                "lifecycle": "published",
                "is_enabled": True,
                "etag": config.etag + 1,
                "updated_at": datetime.now(UTC),
                "updated_by": actor,
            }
        )
        await self._repo.save(updated)
        await self._save_version(updated)
        await self._save_audit(
            config=updated,
            action="publish",
            actor=actor,
            reason=reason,
            previous_etag=config.etag,
            changed_fields=["lifecycle", "is_enabled"],
        )
        return _mask_config(updated)

    async def assert_agent_eligible(
        self, config_id: str, exact_version: int
    ) -> ModelConfigVersion:
        config = await self._repo.get(config_id)
        if config is None:
            raise ResourceNotFound("model config not found")
        if not config.is_enabled or config.lifecycle != "published":
            raise RunStateConflict("model resource is disabled or not published")
        versions = await self.list_versions(config_id)
        version = next((item for item in versions if item.version == exact_version), None)
        if version is None:
            raise ResourceNotFound("model config version not found")
        if version.lifecycle != "published":
            raise RunStateConflict("model config version is not published")
        records = await self.list_test_records(config_id)
        missing = sorted(_missing_required_tests(version.config, records))
        if missing:
            raise RunStateConflict(
                f"required model tests have not passed: {', '.join(missing)}"
            )
        return version

    async def delete_config(self, *, config_id: str) -> None:
        config = await self._repo.get(config_id)
        if config is None:
            raise ResourceNotFound("model config not found")
        if config.is_enabled:
            raise RunStateConflict("cannot delete an enabled config; disable first")
        if (
            self._agent_release_reader is not None
            and await self._agent_release_reader.is_model_referenced(config_id)
        ):
            raise RunStateConflict(
                "model resource is referenced by an Agent release and cannot be deleted"
            )
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

    async def run_test(
        self,
        *,
        config_id: str,
        kind: ModelTestKind,
        profile: Literal["fast", "standard", "deep"] | None,
        tested_by: str,
    ) -> ModelTestRecord:
        config = await self._repo.get(config_id)
        if config is None:
            raise ResourceNotFound("model config not found")
        _validate_model_endpoint(config.api_base_url)
        api_key = await self._keys.resolve_key(config_id=config_id)
        selected_profile = getattr(
            config.parameter_profiles, profile or "standard"
        )
        recorded_profile = (profile or "deep") if kind == "reasoning" else profile
        actual_parameters: dict[str, object] = dict(selected_profile.provider_options)
        if selected_profile.temperature is not None:
            actual_parameters["temperature"] = selected_profile.temperature
        if selected_profile.top_p is not None:
            actual_parameters["top_p"] = selected_profile.top_p
        max_output_tokens = selected_profile.max_output_tokens or 16
        actual_parameters["max_output_tokens"] = max_output_tokens
        user_content = "Reply with OK."
        tools: tuple[ModelToolDefinition, ...] = ()
        if kind == "connection":
            max_output_tokens = 1
            actual_parameters["max_output_tokens"] = 1
        elif kind == "tool_calling":
            user_content = "Call health_check."
            tools = (
                ModelToolDefinition(
                    tool_id="health_check",
                    description="Return service health",
                    input_schema={"type": "object", "properties": {}},
                ),
            )
        elif kind == "structured_output":
            user_content = 'Return only the JSON object {"ok":true}.'
        elif kind == "reasoning":
            if config.capabilities.reasoning.mode == "unsupported":
                return await self.record_test_result(
                    config_id=config_id, kind=kind, profile=recorded_profile,
                    success=False,
                    tested_by=tested_by, actual_parameters=actual_parameters,
                    error_code="reasoning_unsupported",
                    error_message="model does not declare reasoning support",
                )
            reasoning = (
                config.capabilities.reasoning.deep_profile
                if (profile or "deep") == "deep"
                else config.capabilities.reasoning.fast_profile
            )
            if reasoning is not None:
                if reasoning.enable_thinking is not None:
                    actual_parameters["enable_thinking"] = reasoning.enable_thinking
                if reasoning.reasoning_effort is not None:
                    actual_parameters["reasoning_effort"] = reasoning.reasoning_effort
        effective_mode: Literal["fast", "deep"] = (
            "deep" if recorded_profile == "deep" else "fast"
        )
        inference = ModelInferenceOptions(
            requested_mode=effective_mode,
            effective_mode=effective_mode,
            enable_thinking=cast(bool | None, actual_parameters.get("enable_thinking")),
            reasoning_effort=cast(
                Literal["high", "max", "xhigh"] | None,
                actual_parameters.get("reasoning_effort"),
            ),
        )
        runtime_config = ModelConfigWithKey(
            config_id=config.config_id,
            name=config.name,
            api_base_url=config.api_base_url,
            api_key_secret=api_key.get_secret_value(),
            model_name=config.model_name,
            protocol=config.protocol,
            provider_type=config.provider_type,
            parameter_profiles=config.parameter_profiles,
            timeout_seconds=config.timeout_seconds,
            max_output_tokens=config.max_output_tokens,
            max_retries=config.max_retries,
            reasoning_capability=config.reasoning_capability,
            is_enabled=config.is_enabled,
        )
        from full_view_agent.infrastructure.model_provider_factory import (
            build_model_provider,
        )

        provider = build_model_provider(runtime_config)
        request = ModelRequest(
            messages=(ModelMessage(role="user", content=user_content),),
            tools=tools,
            max_output_tokens=max_output_tokens,
            inference=inference,
        )
        start = datetime.now(UTC)
        try:
            response = await provider.complete(request)
            latency = int((datetime.now(UTC) - start).total_seconds() * 1000)
            semantic_success = bool(response.content or response.tool_calls)
            if kind == "tool_calling":
                semantic_success = bool(response.tool_calls)
            elif kind == "structured_output":
                try:
                    semantic_success = isinstance(
                        json.loads(response.content or ""), dict
                    )
                except (TypeError, json.JSONDecodeError):
                    semantic_success = False
            return await self.record_test_result(
                config_id=config_id, kind=kind, profile=recorded_profile,
                success=semantic_success,
                tested_by=tested_by, latency_ms=latency,
                actual_parameters=actual_parameters,
                error_code=None if semantic_success else "contract_failed",
                error_message=None if semantic_success else "model response failed test contract",
                prompt_tokens=response.usage.prompt_tokens,
                completion_tokens=response.usage.completion_tokens,
                reasoning_tokens=response.usage.reasoning_tokens,
            )
        except Exception as exc:
            return await self.record_test_result(
                config_id=config_id, kind=kind, profile=recorded_profile,
                success=False,
                tested_by=tested_by,
                latency_ms=int((datetime.now(UTC) - start).total_seconds() * 1000),
                actual_parameters=actual_parameters,
                error_code="model_test_failed", error_message=str(exc)[:500],
            )

    async def resolve_for_runtime(
        self,
        *,
        required: bool = True,
    ) -> ModelConfigWithKey | None:
        """Resolve the currently enabled config with plaintext key.
        Only for runtime use; never expose via API.

        ``required=False`` is reserved for control-plane startup.  It lets an
        administrator reach the model centre before choosing an explicit
        legacy default.  Data-plane callers keep the fail-closed default.
        """
        configs = await self._repo.list_all()
        enabled_configs = [
            config
            for config in configs
            if config.is_enabled and config.legacy_default
        ]
        if not enabled_configs:
            published = [config for config in configs if config.is_enabled]
            if published:
                if not required:
                    return None
                raise RunStateConflict(
                    "legacy runtime requires an explicit legacy default"
                )
            return None
        if len(enabled_configs) != 1:
            raise RunStateConflict(
                "legacy runtime requires one explicit legacy default"
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
            provider_type=enabled.provider_type,
            parameter_profiles=enabled.parameter_profiles,
            timeout_seconds=enabled.timeout_seconds,
            max_output_tokens=enabled.max_output_tokens,
            max_retries=enabled.max_retries,
            reasoning_capability=enabled.reasoning_capability,
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
            provider_type=config.provider_type,
            parameter_profiles=config.parameter_profiles,
            timeout_seconds=config.timeout_seconds,
            max_output_tokens=config.max_output_tokens,
            max_retries=config.max_retries,
            reasoning_capability=config.reasoning_capability,
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
        enabled_configs = [
            config
            for config in configs
            if config.is_enabled and config.legacy_default
        ]
        if not enabled_configs:
            if any(config.is_enabled for config in configs):
                raise RunStateConflict(
                    "legacy runtime requires an explicit legacy default model"
                )
            return None
        if len(enabled_configs) != 1:
            raise RunStateConflict(
                "legacy runtime requires one explicit legacy default model"
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
            reasoning_capability=enabled.reasoning_capability,
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
        versions = await self.list_versions(config_id)
        exact = next(
            (item for item in versions if item.version == expected_version), None
        )
        if exact is None:
            return self._historical_snapshots.get((config_id, expected_version))
        load_material = getattr(self._repo, "load_version_key_material", None)
        material = (
            await load_material(config_id=config_id, version=expected_version)
            if load_material is not None
            else None
        )
        if material is None:
            return self._historical_snapshots.get((config_id, expected_version))
        ciphertext, nonce = material
        version_config = exact.config
        return ModelConfigSnapshot(
            config_id=version_config.config_id,
            config_version=exact.version,
            name=version_config.name,
            api_base_url=version_config.api_base_url,
            model_name=version_config.model_name,
            protocol=version_config.protocol,
            provider_type=version_config.provider_type,
            parameter_profiles=version_config.parameter_profiles,
            timeout_seconds=version_config.timeout_seconds,
            max_output_tokens=version_config.max_output_tokens,
            max_retries=version_config.max_retries,
            reasoning_capability=version_config.reasoning_capability,
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
        provider_type=config.provider_type,
        timeout_seconds=config.timeout_seconds,
        max_output_tokens=config.max_output_tokens,
        max_retries=config.max_retries,
        reasoning_capability=config.reasoning_capability,
        capabilities=config.capabilities,
        parameter_profiles=config.parameter_profiles,
        lifecycle=config.lifecycle,
        is_enabled=config.is_enabled,
        legacy_default=config.legacy_default,
        notes=config.notes,
        created_at=config.created_at,
        updated_at=config.updated_at,
        created_by=config.created_by,
        updated_by=config.updated_by,
        version=config.version,
        etag=config.etag,
    )


def _validate_model_endpoint(api_base_url: str) -> None:
    """Reject dangerous model endpoints before any request is possible."""

    parsed = urlsplit(api_base_url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise RunStateConflict("model endpoint failed SSRF validation: HTTPS required")
    if parsed.username or parsed.password:
        raise RunStateConflict("model endpoint failed SSRF validation: credentials forbidden")
    hostname = parsed.hostname.lower().rstrip(".")
    if hostname == "localhost" or hostname.endswith(".localhost"):
        raise RunStateConflict("model endpoint failed SSRF validation: localhost blocked")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return
    if not address.is_global:
        raise RunStateConflict("model endpoint failed SSRF validation: non-public IP blocked")


def _redact_parameters(parameters: dict[str, object]) -> dict[str, object]:
    sensitive_fragments = (
        "api_key", "access_key", "secret", "access_token", "password",
        "authorization",
    )
    return {
        key: value
        for key, value in parameters.items()
        if not any(fragment in key.lower() for fragment in sensitive_fragments)
    }


def _required_tests(config: ModelConfig | ModelConfigMasked) -> set[str]:
    required = {"connection", "chat"}
    if config.capabilities.tool_calling:
        required.add("tool_calling")
    if config.capabilities.structured_output:
        required.add("structured_output")
    if config.capabilities.reasoning.mode != "unsupported":
        required.add("reasoning")
    return required


def _missing_required_tests(
    config: ModelConfig | ModelConfigMasked,
    records: list[ModelTestRecord],
) -> set[str]:
    """Return publish blockers, including an explicit deep-profile proof."""

    passed = {
        item.kind
        for item in records
        if item.version == config.version and item.success
    }
    missing = set(_required_tests(config)) - passed
    if config.capabilities.reasoning.mode != "unsupported" and not any(
        item.version == config.version
        and item.kind == "reasoning"
        and item.profile == "deep"
        and item.success
        for item in records
    ):
        missing.discard("reasoning")
        missing.add("reasoning:deep")
    return missing
