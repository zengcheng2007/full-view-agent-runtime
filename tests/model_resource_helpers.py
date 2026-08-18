"""Test builders for real model-resource lifecycle fixtures."""

from __future__ import annotations

from full_view_agent.application.model_config_service import ModelConfigService
from full_view_agent.domain.capability import (
    ModelCapabilityDeclaration,
    ModelConfigMasked,
    ModelReasoningCapability,
    ModelReasoningProfile,
)


def hybrid_reasoning() -> ModelReasoningCapability:
    return ModelReasoningCapability(
        mode="hybrid",
        fast_profile=ModelReasoningProfile(enable_thinking=False),
        deep_profile=ModelReasoningProfile(enable_thinking=True),
    )


async def publish_tested_model(
    service: ModelConfigService,
    *,
    name: str,
    model_name: str | None = None,
    api_base_url: str = "https://models.example/v1",
    api_key: str | None = None,
    legacy_default: bool = False,
) -> ModelConfigMasked:
    reasoning = hybrid_reasoning()
    created = await service.create_config(
        name=name,
        api_base_url=api_base_url,
        api_key=api_key or f"key-{name}",
        model_name=model_name or name,
        reasoning_capability=reasoning,
        capabilities=ModelCapabilityDeclaration(reasoning=reasoning),
        created_by="test-admin",
    )
    for kind in ("connection", "chat", "reasoning"):
        await service.record_test_result(
            config_id=created.config_id,
            kind=kind,
            profile="deep" if kind == "reasoning" else None,
            success=True,
            tested_by="test-admin",
        )
    tested = await service.get_config(created.config_id)
    published = await service.publish_config(
        config_id=created.config_id,
        expected_etag=tested.etag,
        actor="test-admin",
        reason="test fixture passed required checks",
    )
    if legacy_default:
        published = await service.set_legacy_default(
            config_id=published.config_id,
            expected_etag=published.etag,
            actor="test-admin",
            reason="explicit legacy test fixture",
        )
    return published
