from __future__ import annotations

from threading import RLock

from full_view_agent.domain.prompt_template import RuntimePromptSnapshot


class RuntimePromptRegistry:
    """Thread-safe published prompt pointer; snapshots are immutable values."""

    def __init__(self, current: RuntimePromptSnapshot | None = None) -> None:
        self._current = current
        self._lock = RLock()

    def activate(self, snapshot: RuntimePromptSnapshot | None) -> None:
        with self._lock:
            self._current = snapshot

    def snapshot(self) -> RuntimePromptSnapshot | None:
        with self._lock:
            return self._current.model_copy(deep=True) if self._current else None
