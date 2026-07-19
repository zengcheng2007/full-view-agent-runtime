"""Redis wake-up adapter for cross-instance event streams."""

import asyncio

from redis.asyncio import Redis


class RedisEventNotifier:
    def __init__(self, *, url: str, channel_prefix: str = "full-view-agent:events") -> None:
        self._client = Redis.from_url(url, decode_responses=True)
        self._channel_prefix = channel_prefix

    async def publish(self, *, run_id: str, event_id: str) -> None:
        await self._client.publish(self._channel(run_id), event_id)

    async def wait(self, *, run_id: str, timeout_seconds: float) -> str | None:
        pubsub = self._client.pubsub()
        channel = self._channel(run_id)
        await pubsub.subscribe(channel)
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        try:
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    return None
                message = await pubsub.get_message(
                    ignore_subscribe_messages=True,
                    timeout=remaining,
                )
                if message is not None and message.get("type") == "message":
                    return str(message["data"])
        finally:
            await pubsub.unsubscribe(channel)
            await pubsub.aclose()

    async def close(self) -> None:
        await self._client.aclose()

    async def health_check(self) -> None:
        await self._client.ping()  # type: ignore[misc]

    def _channel(self, run_id: str) -> str:
        return f"{self._channel_prefix}:{run_id}"
