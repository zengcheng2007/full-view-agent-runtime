from __future__ import annotations

from threading import RLock

from full_view_agent.domain.prompt_template import RuntimePromptSnapshot


class RuntimePromptRegistry:
    """Thread-safe application-scoped published prompt pointers."""

    def __init__(self, current: RuntimePromptSnapshot | None = None) -> None:
        self._current_by_app = {current.app_id: current} if current else {}
        self._lock = RLock()

    def activate(
        self,
        snapshot: RuntimePromptSnapshot | None,
        *,
        app_id: str | None = None,
    ) -> None:
        with self._lock:
            if snapshot is None:
                if app_id is None:
                    self._current_by_app.clear()
                else:
                    self._current_by_app.pop(app_id, None)
                return
            if app_id is not None and snapshot.app_id != app_id:
                raise ValueError("prompt snapshot does not belong to the application")
            self._current_by_app[snapshot.app_id] = snapshot

    def snapshot(self, *, app_id: str | None = None) -> RuntimePromptSnapshot | None:
        with self._lock:
            if app_id is not None:
                current = self._current_by_app.get(app_id)
            elif not self._current_by_app:
                current = None
            elif len(self._current_by_app) == 1:
                current = next(iter(self._current_by_app.values()))
            else:
                raise RuntimeError(
                    "runtime prompt lookup must be application-scoped"
                )
            return current.model_copy(deep=True) if current else None
