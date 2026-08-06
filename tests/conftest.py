"""Pytest configuration for tests that need real PostgreSQL on Windows."""

import asyncio
import contextlib
import sys


def pytest_configure(config):
    """Set event loop policy before any tests run."""
    if sys.platform == "win32":
        with contextlib.suppress(Exception):
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
