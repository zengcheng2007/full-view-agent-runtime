from __future__ import annotations

from pathlib import Path

from full_view_agent.api.app import RuntimeContainer, create_app


def test_model_resource_api_exposes_complete_management_surface() -> None:
    paths = create_app(runtime=RuntimeContainer()).openapi()["paths"]
    prefix = "/capability-api/v1/model-configs"
    expected = {
        f"{prefix}/legacy-default",
        f"{prefix}/environment-candidate",
        f"{prefix}/import-environment",
        f"{prefix}/{{config_id}}/versions",
        f"{prefix}/{{config_id}}/versions/{{version}}",
        f"{prefix}/{{config_id}}/versions/{{version}}/diff",
        f"{prefix}/{{config_id}}/tests/{{kind}}",
        f"{prefix}/{{config_id}}/test-records",
        f"{prefix}/{{config_id}}/publish",
        f"{prefix}/{{config_id}}/disable",
        f"{prefix}/{{config_id}}/rollback",
        f"{prefix}/{{config_id}}/audit-events",
        f"{prefix}/{{config_id}}/usage",
    }
    assert expected <= set(paths)


def test_v031_migration_persists_model_versions_tests_audit_and_default() -> None:
    root = Path(__file__).parents[1]
    migration = root / "scripts" / "migrations" / "V031_model_resource_center.sql"
    assert migration.exists()
    sql = migration.read_text(encoding="utf-8")
    for required in (
        "model_config_versions",
        "model_test_records",
        "model_audit_events",
        "model_legacy_default",
        "provider_type",
        "lifecycle",
        "parameter_profiles",
        "capabilities",
        "etag",
    ):
        assert required in sql

    from full_view_agent.infrastructure.postgres_persistence import (
        PostgresAgentPersistence,
    )

    persistence = PostgresAgentPersistence(dsn="postgresql://unused")
    statements = persistence._p2_migration_statements()  # noqa: SLF001
    assert any("model_config_versions" in statement for statement in statements)
