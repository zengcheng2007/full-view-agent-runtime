import asyncio

import uvicorn


def selector_loop_factory() -> asyncio.AbstractEventLoop:
    return asyncio.SelectorEventLoop()


def main() -> None:
    uvicorn.run(
        "full_view_agent.api.app:app",
        host="0.0.0.0",
        port=8000,
        loop="full_view_agent.server:selector_loop_factory",
        env_file=".env",
    )


if __name__ == "__main__":
    main()
