import asyncio

from full_view_agent.application.errors import ResourceNotFound
from full_view_agent.domain.models import AuthContext


class InMemoryRunAuthContextStore:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._contexts: dict[str, AuthContext] = {}

    async def put(self, auth_context: AuthContext) -> AuthContext:
        async with self._lock:
            self._contexts[auth_context.run_id] = auth_context
            return auth_context

    async def get(self, *, user_id: str, run_id: str) -> AuthContext:
        async with self._lock:
            auth_context = self._contexts.get(run_id)
            if (
                auth_context is None
                or auth_context.principal.user_id != user_id
            ):
                raise ResourceNotFound("auth context not found")
            return auth_context

    async def delete(self, *, run_id: str) -> None:
        async with self._lock:
            self._contexts.pop(run_id, None)
