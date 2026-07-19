"""Full Information View agent runtime."""

import asyncio
import sys
import warnings

# psycopg async I/O requires a selector loop on Windows. Configure it before
# FastAPI, pytest-asyncio, or a CLI runner creates the process event loop.
if sys.platform == "win32":
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        selector_policy = getattr(asyncio, "WindowsSelectorEventLoopPolicy", None)
        if selector_policy is not None:
            asyncio.set_event_loop_policy(selector_policy())
