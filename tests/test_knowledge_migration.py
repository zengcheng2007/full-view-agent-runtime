from pathlib import Path


def test_v020_uses_portable_tables_and_explicit_scope_keys() -> None:
    migration = (
        Path(__file__).parents[1] / "scripts" / "migrations" / "V020_knowledge_bases.sql"
    ).read_text(encoding="utf-8")
    lowered = migration.lower()

    assert "knowledge_bases" in lowered
    assert "knowledge_documents" in lowered
    assert "knowledge_data_sources" in lowered
    assert "knowledge_publications" in lowered
    assert "knowledge_chunks" in lowered
    assert "knowledge_index_status" in lowered
    assert "knowledge_audit_events" in lowered
    assert "tenant_id" in lowered
    assert "app_id" in lowered
    assert "knowledge_base_version" in lowered
    assert "application_binding_enabled" in lowered
    assert "public_within_app" in lowered
    assert "allowed_user_ids_json" in lowered
    assert "allowed_roles_json" in lowered
    assert "paragraph_start" in lowered
    assert "vector" not in lowered
    assert "extension" not in lowered
