import importlib.util
import os
from uuid import uuid4

import pytest


def test_redis_event_notifier_module_is_available() -> None:
    assert (
        importlib.util.find_spec(
            "full_view_agent.infrastructure.redis_event_notifier"
        )
        is not None
    )


def test_redis_event_notifier_class_is_available() -> None:
    from full_view_agent.infrastructure import redis_event_notifier

    assert hasattr(redis_event_notifier, "RedisEventNotifier")


def test_redis_event_notifier_exposes_publish_wait_and_close_contract() -> None:
    import inspect

    from full_view_agent.infrastructure.redis_event_notifier import (
        RedisEventNotifier,
    )

    parameters = inspect.signature(RedisEventNotifier).parameters

    assert "url" in parameters
    assert "channel_prefix" in parameters
    assert hasattr(RedisEventNotifier, "publish")
    assert hasattr(RedisEventNotifier, "wait")
    assert hasattr(RedisEventNotifier, "close")


@pytest.mark.asyncio
@pytest.mark.redis
async def test_redis_notifier_wakes_a_different_instance() -> None:
    import asyncio

    from full_view_agent.infrastructure.redis_event_notifier import (
        RedisEventNotifier,
    )

    redis_url = os.getenv("FULL_VIEW_TEST_REDIS_URL")
    if not redis_url:
        pytest.skip("FULL_VIEW_TEST_REDIS_URL is not configured")
    prefix = f"fva-test:{uuid4().hex}"
    publisher = RedisEventNotifier(url=redis_url, channel_prefix=prefix)
    subscriber = RedisEventNotifier(url=redis_url, channel_prefix=prefix)
    try:
        waiting = asyncio.create_task(
            subscriber.wait(run_id="run-cross-instance", timeout_seconds=1)
        )
        await asyncio.sleep(0.05)
        await publisher.publish(
            run_id="run-cross-instance",
            event_id="evt-cross-instance",
        )

        assert await waiting == "evt-cross-instance"
    finally:
        await publisher.close()
        await subscriber.close()
