import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from pydantic import BaseModel

from full_view_agent.application.authorization_scope import extract_area_scope
from full_view_agent.application.fingerprints import canonical_fingerprint
from full_view_agent.domain.models import (
    AuthContext,
    GetObjectProfileInput,
    InternalToolManifest,
    PolicyDecision,
)


@dataclass(frozen=True)
class _DenialEntry:
    scope_fingerprint: str
    reason_code: str
    expires_at: datetime


class InMemoryDenialLedger:
    def __init__(self, *, ttl_seconds: int = 300) -> None:
        self._ttl = timedelta(seconds=ttl_seconds)
        self._lock = asyncio.Lock()
        self._entries: dict[str, list[_DenialEntry]] = {}

    async def contains(
        self,
        *,
        manifest: InternalToolManifest,
        arguments: BaseModel,
        auth_context: AuthContext,
    ) -> bool:
        now = datetime.now(UTC)
        fingerprint = _scope_fingerprint(manifest, arguments)
        async with self._lock:
            active = [
                entry
                for entry in self._entries.get(auth_context.run_id, [])
                if entry.expires_at > now
            ]
            self._entries[auth_context.run_id] = active
            return any(entry.scope_fingerprint == fingerprint for entry in active)

    async def record(
        self,
        *,
        manifest: InternalToolManifest,
        arguments: BaseModel,
        auth_context: AuthContext,
        decision: PolicyDecision,
    ) -> None:
        entry = _DenialEntry(
            scope_fingerprint=_scope_fingerprint(manifest, arguments),
            reason_code=(decision.reason_codes[0] if decision.reason_codes else "DENIED"),
            expires_at=min(auth_context.expires_at, datetime.now(UTC) + self._ttl),
        )
        async with self._lock:
            entries = self._entries.setdefault(auth_context.run_id, [])
            if not any(item.scope_fingerprint == entry.scope_fingerprint for item in entries):
                entries.append(entry)


def _scope_fingerprint(
    manifest: InternalToolManifest,
    arguments: BaseModel,
) -> str:
    area_codes: list[str] = []
    object_ref: dict[str, str] | None = None
    area_scope = extract_area_scope(arguments)
    if area_scope is not None:
        area_codes = [area_scope.area_code]
    if isinstance(arguments, GetObjectProfileInput):
        object_ref = arguments.object_ref.model_dump(mode="json")
    return canonical_fingerprint(
        domain="denial-scope:1.1",
        value={
            "denial_scope": manifest.policy.denial_scope,
            "datasets": [manifest.dataset_id],
            "area_codes": area_codes,
            "object_ref": object_ref,
        },
    )
