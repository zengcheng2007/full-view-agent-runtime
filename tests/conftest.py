"""Pytest configuration.

Env isolation
-------------

Production environment variables (``FULL_VIEW_DATABASE_URL``,
``FULL_VIEW_REDIS_URL``, ``FULL_VIEW_CREDENTIAL_KEY``, governance URLs,
model provider URLs, etc.) are cleared in ``pytest_configure`` so that
the default test run cannot accidentally touch the shared
``full_view_agent`` PostgreSQL schema or a developer's live Redis.

Tests that need a real PostgreSQL instance opt in via
``FULL_VIEW_TEST_DATABASE_URL``. The ``pg_schema`` fixture creates a
throwaway ``fva_test_<uuid>`` schema, runs the V001..V017 migrations,
and drops the schema in ``finally``. A hard guardrail fails the
fixture if the resolved DSN matches the production DSN or if the
target schema is ``full_view_agent`` — the shared catalog is never
written to from the test suite.

Mark organisation
-----------------

* ``db``     — tests that require ``FULL_VIEW_TEST_DATABASE_URL``.
* ``redis``  — tests that require ``FULL_VIEW_TEST_REDIS_URL``.

``strict-markers`` is enabled so that typos fail loudly instead of
silently running un-marked.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import os
import sys
import uuid

import psycopg
import pytest

# ── Windows event-loop policy ─────────────────────────────────────────
# Must happen before anything creates a loop.


def pytest_configure(config: pytest.Config) -> None:
    """Clear production env vars + set Windows event-loop policy."""
    if sys.platform == "win32":
        with contextlib.suppress(Exception):
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    # Strip anything that would let the code-under-test reach the
    # shared ``full_view_agent`` catalog or a live Redis. Individual
    # tests that need a DB/Redis inject their own values via
    # ``monkeypatch`` or the ``pg_schema`` fixture below.
    for var in _PRODUCTION_ENV_VARS:
        os.environ.pop(var, None)

    # ``FULL_VIEW_TEST_*`` are the ONLY env vars the test suite honours
    # for external services. They are kept as-is so that fixtures can
    # read them; tests themselves never see ``FULL_VIEW_DATABASE_URL``
    # unless the fixture explicitly sets it.


_PRODUCTION_ENV_VARS = (
    # PostgreSQL (shared catalog guard)
    "FULL_VIEW_DATABASE_URL",
    "FULL_VIEW_POSTGRES_SCHEMA",
    "FULL_VIEW_LANGGRAPH_POSTGRES_SCHEMA",
    "FULL_VIEW_CREDENTIAL_KEY",
    "FULL_VIEW_CURSOR_KEY",
    # Redis
    "FULL_VIEW_REDIS_URL",
    "FULL_VIEW_REDIS_EVENT_PREFIX",
    # Business adapter / identity
    "FULL_VIEW_GOVERNANCE_ADAPTER",
    "FULL_VIEW_GOVERNANCE_BASE_URL",
    "FULL_VIEW_LEGACY_GATEWAY_URL",
    # Model provider
    "FULL_VIEW_MODEL_PROVIDER",
    "FULL_VIEW_MODEL_BASE_URL",
    "FULL_VIEW_MODEL_NAME",
    "FULL_VIEW_MODEL_API_KEY",
    "FULL_VIEW_MODEL_TIMEOUT_SECONDS",
    "FULL_VIEW_MODEL_TOKEN_BUDGET",
)


# ── PostgreSQL schema fixture ─────────────────────────────────────────


_PRODUCTION_DSN_GUARD = os.environ.get("FULL_VIEW_PRODUCTION_DATABASE_URL")
_PRODUCTION_SCHEMA = "full_view_agent"


@pytest.fixture()
async def pg_schema():
    """Per-test throwaway ``fva_test_<uuid>`` PostgreSQL schema.

    * Reads the test DSN from ``FULL_VIEW_TEST_DATABASE_URL``.
    * Creates a throwaway schema named ``fva_test_<uuid>``.
    * Runs migrations V001..V019 (the full agent schema) after
      substituting ``full_view_agent`` with the throwaway schema name.
    * Sets ``FULL_VIEW_DATABASE_URL`` / ``FULL_VIEW_POSTGRES_SCHEMA`` /
      ``FULL_VIEW_CREDENTIAL_KEY`` for the duration of the test via
      ``os.environ`` (most code reads ``os.getenv`` at call time).
    * Drops the schema in ``finally``.
    * **Fails fast** if the resolved DSN matches the production DSN
      (as advertised via ``FULL_VIEW_PRODUCTION_DATABASE_URL``) or if
      the schema name equals ``full_view_agent``.
    """
    test_dsn = os.environ.get("FULL_VIEW_TEST_DATABASE_URL")
    if not test_dsn:
        pytest.skip("FULL_VIEW_TEST_DATABASE_URL not set")

    # ── Safety: never write to the shared catalog ────────────────────
    if _PRODUCTION_DSN_GUARD and test_dsn == _PRODUCTION_DSN_GUARD:
        pytest.fail(
            "FULL_VIEW_TEST_DATABASE_URL must NOT equal the production"
            " FULL_VIEW_DATABASE_URL"
        )
    schema_name = f"fva_test_{uuid.uuid4().hex}"
    if schema_name == _PRODUCTION_SCHEMA:
        pytest.fail("refusing to use the production schema name")

    credential_key_b64 = os.environ.get(
        "FULL_VIEW_TEST_CREDENTIAL_KEY",
        # Fallback: a deterministic test-only key, never the production
        # one. Tests that need a specific key set
        # FULL_VIEW_TEST_CREDENTIAL_KEY explicitly.
        "dGVzdC1vbmx5LWtleS0xMjM0NTY3ODkwMTIzNDU2Nzg5MA==",
    )

    # Create the throwaway schema + run migrations.
    async with await psycopg.AsyncConnection.connect(test_dsn) as conn:
        await conn.execute(f'CREATE SCHEMA "{schema_name}"')
    try:
        for migration in _MIGRATIONS_ALL:
            rendered = migration.replace(
                "full_view_agent", schema_name
            )
            async with await psycopg.AsyncConnection.connect(test_dsn) as conn:
                await conn.execute(rendered)

        # Expose the test DSN to code under test via env.
        os.environ["FULL_VIEW_DATABASE_URL"] = test_dsn
        os.environ["FULL_VIEW_POSTGRES_SCHEMA"] = schema_name
        os.environ["FULL_VIEW_CREDENTIAL_KEY"] = credential_key_b64
        os.environ["FULL_VIEW_RUNTIME_PROFILE"] = "test"

        yield {
            "dsn": test_dsn,
            "schema": schema_name,
            "credential_key_b64": credential_key_b64,
        }
    finally:
        # Remove the env vars we set so subsequent tests see a clean slate.
        for var in (
            "FULL_VIEW_DATABASE_URL",
            "FULL_VIEW_POSTGRES_SCHEMA",
            "FULL_VIEW_CREDENTIAL_KEY",
            "FULL_VIEW_RUNTIME_PROFILE",
        ):
            os.environ.pop(var, None)
        # Drop the throwaway schema. Best-effort: if the DB is already
        # gone or the connection fails we don't want to mask the real
        # test failure with a cleanup error.
        try:
            async with await psycopg.AsyncConnection.connect(test_dsn) as conn:
                await conn.execute(f'DROP SCHEMA "{schema_name}" CASCADE')
        except psycopg.OperationalError:
            pass


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Function]
) -> None:
    """Auto-mark tests that call ``postgres_test_dsn`` or check
    ``FULL_VIEW_TEST_DATABASE_URL`` at runtime.

    This catches tests that aren't yet explicitly marked with
    ``@pytest.mark.db`` but clearly need PostgreSQL. Combined with
    the explicit markers added to each file, the full set of Postgres
    tests ends up under the ``db`` marker.
    """
    for item in items:
        # Already marked — leave alone.
        if "db" in item.keywords or "redis" in item.keywords:
            continue
        # Read the source and check for the tell-tale patterns.
        try:
            source, _ = inspect.getsourcelines(item.function)  # type: ignore[arg-type]
        except (OSError, TypeError):
            continue
        text = "".join(source)
        if "FULL_VIEW_TEST_DATABASE_URL" in text or "postgres_test_dsn" in text:
            item.add_marker(pytest.mark.db)
        elif "FULL_VIEW_TEST_REDIS_URL" in text:
            item.add_marker(pytest.mark.redis)


@pytest.fixture(autouse=True)
def _isolate_db_env_for_db_tests(request: pytest.FixtureRequest) -> None:
    """Auto-apply the ``pg_schema`` fixture for any test marked ``db``.

    This ensures tests that construct ``RuntimeContainer`` or other
    DB-aware components via environment variables see the isolated
    throwaway schema, not the shared ``full_view_agent`` catalog.

    The fixture is function-scoped so each test gets its own schema.
    Tests that already explicitly request ``pg_schema`` are unaffected
    (pytest deduplicates fixture invocations).
    """
    if "db" not in getattr(request, "keywords", ()):
        return
    # Skip if the test already requested pg_schema explicitly.
    if "pg_schema" in request.fixturenames:
        return
    # Request pg_schema — pytest resolves it and sets env vars.
    request.getfixturevalue("pg_schema")


# ── Migration payloads (V010..V013) ──────────────────────────────────


def _load_migration(name: str) -> str:
    import pathlib

    path = (
        pathlib.Path(__file__).resolve().parent.parent
        / "scripts"
        / "migrations"
        / name
    )
    return path.read_text(encoding="utf-8")


# Order matters: each migration assumes its predecessors have run.
# We inline the text at import time so tests don't depend on the
# working directory. V001..V019 cover the full agent schema.
try:
    _MIGRATIONS_ALL = tuple(
        _load_migration(f"V{idx:03d}_{name}.sql")
        for idx, name in (
            (1, "initial_schema"),
            (2, "checkpoint_mapping"),
            (3, "analysis_plans"),
            (4, "analysis_run_bindings"),
            (5, "analysis_step_ledger"),
            (6, "analysis_step_outcomes"),
            (7, "analysis_plan_discovery"),
            (8, "analysis_intent_handoffs"),
            (9, "checkpoint_graph_kind"),
            (10, "capability_center"),
            (11, "seed_system_capabilities"),
            (12, "run_scoped_bindings"),
            (13, "application_registry"),
            (14, "session_application_isolation"),
            (15, "application_lifecycle"),
            (16, "seed_full_view_capabilities"),
            (17, "connector_management"),
            (18, "event_trend_capability"),
            (19, "event_category_capability"),
            (20, "knowledge_bases"),
            (21, "prompt_templates"),
            (22, "seed_knowledge_search"),
            (23, "enterprise_industry_distribution"),
            (24, "application_agents"),
            (25, "seed_governance_power"),
            (26, "runtime_observability_indexes"),
            (27, "tool_semantic_contracts"),
        )
    )
except FileNotFoundError:
    # When running from a sdist that doesn't ship migrations, tests
    # that need the DB will be skipped by the fixture itself.
    _MIGRATIONS_ALL = ()
