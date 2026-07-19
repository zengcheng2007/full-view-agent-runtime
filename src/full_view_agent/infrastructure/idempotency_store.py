import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TypeVar

from full_view_agent.application.errors import IdempotencyConflict

T = TypeVar("T")


@dataclass(frozen=True)
class IdempotencyRecord:
    request_fingerprint: str
    result: object


class InMemoryIdempotencyStore:
    def __init__(self) -> None:
        self._locks_guard = asyncio.Lock()
        self._key_locks: dict[tuple[str, str, str], asyncio.Lock] = {}
        self._records: dict[tuple[str, str, str], IdempotencyRecord] = {}

    async def execute(
        self,
        *,
        user_id: str,
        scope: str,
        key: str,
        request_fingerprint: str,
        operation: Callable[[], Awaitable[T]],
    ) -> tuple[T, bool]:
        record_key = (user_id, scope, key)
        async with self._locks_guard:
            key_lock = self._key_locks.setdefault(record_key, asyncio.Lock())
        async with key_lock:
            existing = self._records.get(record_key)
            if existing is not None:
                if existing.request_fingerprint != request_fingerprint:
                    raise IdempotencyConflict(
                        "idempotency key was already used with a different request"
                    )
                return existing.result, True  # type: ignore[return-value]

            result = await operation()
            self._records[record_key] = IdempotencyRecord(
                request_fingerprint=request_fingerprint,
                result=result,
            )
            return result, False
