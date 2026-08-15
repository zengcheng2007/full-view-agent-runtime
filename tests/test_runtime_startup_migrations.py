from __future__ import annotations

import pytest

from full_view_agent.api.app import RuntimeContainer


@pytest.mark.asyncio
async def test_runtime_applies_postgres_migrations_before_loading_managed_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Managed repositories must never be queried before schema migration."""

    monkeypatch.delenv("FULL_VIEW_DATABASE_URL", raising=False)
    runtime = RuntimeContainer()
    calls: list[str] = []

    class PersistenceProbe:
        async def initialize(self) -> None:
            calls.append("migrate")

    class PromptServiceProbe:
        async def get_effective(self, *, app_id: str) -> None:
            assert app_id == "full_information_view"
            calls.append("prompt")
            assert calls[0] == "migrate"
            return None

    runtime.persistence = PersistenceProbe()  # type: ignore[assignment]
    runtime.prompt_template_service = PromptServiceProbe()  # type: ignore[assignment]
    runtime.runtime_prompt_registry.activate = lambda _value: None  # type: ignore[method-assign]
    await runtime.initialize()

    assert calls == ["migrate", "prompt"]


@pytest.mark.asyncio
async def test_runtime_backfills_legacy_default_release_idempotently_in_postgres(
    pg_schema,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FULL_VIEW_MODEL_PROVIDER", "deterministic")
    first = RuntimeContainer()
    assert first.persistence is not None
    assert first.model_config_service is not None
    await first.persistence.initialize()
    model = await first.model_config_service.create_config(
        name="Legacy baseline model",
        api_base_url="https://models.example/v1",
        api_key="test-only-secret",
        model_name="baseline-model",
    )
    await first.model_config_service.enable_config(config_id=model.config_id)

    await first.initialize()
    assert first.agent_management_service is not None
    release = await first.agent_management_service.get_active_release(
        "full_information_view", "governance_general_agent"
    )
    assert release.agent_version == "0.0.1"
    assert release.prompt_ref is None
    assert release.capability_refs
    assert release.model_refs[0].model_config_id == model.config_id

    restarted = RuntimeContainer()
    await restarted.initialize()
    assert restarted.agent_management_service is not None
    restored = await restarted.agent_management_service.get_active_release(
        "full_information_view", "governance_general_agent"
    )
    assert restored == release
