import importlib
from pathlib import Path

import pytest


def server_module():
    try:
        return importlib.import_module("full_view_agent.server")
    except ModuleNotFoundError:
        pytest.fail("full_view_agent.server is required for Windows-safe startup")


def test_server_exposes_selector_loop_factory() -> None:
    server = server_module()

    loop = server.selector_loop_factory()
    assert isinstance(loop, server.asyncio.SelectorEventLoop)
    loop.close()


def test_server_starts_uvicorn_with_selector_loop_factory(monkeypatch) -> None:
    server = server_module()
    calls = {}

    monkeypatch.setattr(server.uvicorn, "run", lambda *args, **kwargs: calls.update(kwargs))

    server.main()

    assert calls["loop"] == "full_view_agent.server:selector_loop_factory"


def test_readme_uses_the_windows_safe_server_entrypoint() -> None:
    readme = Path("README.md").read_text(encoding="utf-8")

    assert "uv run python -m full_view_agent.server" in readme
