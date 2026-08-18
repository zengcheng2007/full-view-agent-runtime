"""P1 contracts for immutable, provider-aware model execution."""

from __future__ import annotations

import pytest

from full_view_agent.application.errors import RunStateConflict
from full_view_agent.application.model_config_service import (
    EncryptedModelConfigKeyStore,
    ModelConfigService,
)
from full_view_agent.application.model_provider import ModelResponse, ModelUsage
from full_view_agent.domain.capability import (
    ModelCapabilityDeclaration,
    ModelParameterProfile,
    ModelParameterProfiles,
    ModelReasoningCapability,
    ModelReasoningProfile,
)
from full_view_agent.infrastructure.capability_repository import (
    InMemoryModelConfigRepository,
)


def _key_store() -> EncryptedModelConfigKeyStore:
    return EncryptedModelConfigKeyStore(encryption_key=b"k" * 32)


@pytest.mark.asyncio
async def test_exact_version_snapshot_survives_service_restart_and_key_rotation() -> None:
    repository = InMemoryModelConfigRepository()
    keys = _key_store()
    service = ModelConfigService(repository=repository, key_store=keys)
    created = await service.create_config(
        name="Bailian",
        api_base_url="https://dashscope.example/v1",
        api_key="key-v1",
        model_name="qwen-v1",
        provider_type="aliyun_bailian",
        capabilities=ModelCapabilityDeclaration(
            reasoning=ModelReasoningCapability(
                mode="hybrid",
                fast_profile=ModelReasoningProfile(enable_thinking=False),
                deep_profile=ModelReasoningProfile(enable_thinking=True),
            )
        ),
        parameter_profiles=ModelParameterProfiles(
            fast=ModelParameterProfile(provider_options={"enable_thinking": False}),
            deep=ModelParameterProfile(provider_options={"enable_thinking": True}),
        ),
    )
    version_two = await service.create_version(
        config_id=created.config_id,
        expected_etag=created.etag,
        actor="admin",
        reason="rotate endpoint and key",
        changes={"model_name": "qwen-v2"},
        api_key="key-v2",
    )
    assert version_two.version == 2

    restarted = ModelConfigService(repository=repository, key_store=keys)
    snapshot = await restarted.capture_snapshot_by_id(created.config_id, 1)

    assert snapshot is not None
    assert snapshot.provider_type == "aliyun_bailian"
    assert snapshot.parameter_profiles.deep.provider_options == {
        "enable_thinking": True
    }
    restored = restarted.materialise_snapshot(snapshot)
    assert restored.api_key_secret == "key-v1"
    assert restored.model_name == "qwen-v1"


@pytest.mark.asyncio
async def test_create_version_requires_new_key_when_provider_or_endpoint_changes() -> None:
    service = ModelConfigService(
        repository=InMemoryModelConfigRepository(), key_store=_key_store()
    )
    created = await service.create_config(
        name="Model",
        api_base_url="https://one.example/v1",
        api_key="key-v1",
        model_name="model",
    )

    with pytest.raises(
        RunStateConflict,
        match="changing model endpoint or provider requires a new api key",
    ):
        await service.create_version(
            config_id=created.config_id,
            expected_etag=created.etag,
            actor="admin",
            reason="move provider",
            changes={
                "provider_type": "anthropic",
                "api_base_url": "https://api.anthropic.com/v1",
            },
        )


class _ReferencedReleaseReader:
    async def is_model_referenced(self, config_id: str) -> bool:
        del config_id
        return True


@pytest.mark.asyncio
async def test_referenced_model_resource_cannot_be_physically_deleted() -> None:
    repository = InMemoryModelConfigRepository()
    keys = _key_store()
    service = ModelConfigService(
        repository=repository,
        key_store=keys,
        agent_release_reader=_ReferencedReleaseReader(),
    )
    created = await service.create_config(
        name="Referenced",
        api_base_url="https://model.example/v1",
        api_key="secret",
        model_name="model",
    )
    with pytest.raises(RunStateConflict, match="referenced by an Agent release"):
        await service.delete_config(config_id=created.config_id)


@pytest.mark.asyncio
async def test_deep_publish_requires_successful_deep_profile_reasoning_test() -> None:
    service = ModelConfigService(
        repository=InMemoryModelConfigRepository(), key_store=_key_store()
    )
    reasoning = ModelReasoningCapability(
        mode="hybrid",
        fast_profile=ModelReasoningProfile(enable_thinking=False),
        deep_profile=ModelReasoningProfile(enable_thinking=True),
    )
    created = await service.create_config(
        name="Reasoning",
        api_base_url="https://model.example/v1",
        api_key="secret",
        model_name="model",
        reasoning_capability=reasoning,
    )
    for kind in ("connection", "chat"):
        await service.record_test_result(
            config_id=created.config_id,
            kind=kind,
            success=True,
            tested_by="admin",
        )
    fast = await service.record_test_result(
        config_id=created.config_id,
        kind="reasoning",
        profile="fast",
        success=True,
        tested_by="admin",
        actual_parameters={"enable_thinking": False},
    )
    assert fast.profile == "fast"
    current = await service.get_config(created.config_id)

    with pytest.raises(RunStateConflict, match="reasoning:deep"):
        await service.publish_config(
            config_id=created.config_id,
            expected_etag=current.etag,
            actor="admin",
            reason="must prove deep mode",
        )


@pytest.mark.asyncio
async def test_reasoning_test_uses_business_provider_path_and_records_deep_profile(
    monkeypatch,
) -> None:
    service = ModelConfigService(
        repository=InMemoryModelConfigRepository(), key_store=_key_store()
    )
    reasoning = ModelReasoningCapability(
        mode="hybrid",
        fast_profile=ModelReasoningProfile(enable_thinking=False),
        deep_profile=ModelReasoningProfile(enable_thinking=True),
    )
    created = await service.create_config(
        name="Bailian",
        api_base_url="https://model.example/v1",
        api_key="secret",
        model_name="model",
        provider_type="aliyun_bailian",
        reasoning_capability=reasoning,
        parameter_profiles=ModelParameterProfiles(
            deep=ModelParameterProfile(
                provider_options={"enable_thinking": True, "thinking_budget": 2048}
            )
        ),
    )
    seen = {}

    class _Provider:
        async def complete(self, request):
            seen["request"] = request
            return ModelResponse(
                content="reasoned answer",
                tool_calls=(),
                finish_reason="stop",
                usage=ModelUsage(
                    prompt_tokens=3,
                    completion_tokens=4,
                    total_tokens=7,
                    reasoning_tokens=2,
                ),
            )

    def fake_builder(config):
        seen["config"] = config
        return _Provider()

    monkeypatch.setattr(
        "full_view_agent.infrastructure.model_provider_factory.build_model_provider",
        fake_builder,
    )

    record = await service.run_test(
        config_id=created.config_id,
        kind="reasoning",
        profile="deep",
        tested_by="admin",
    )

    assert record.success is True
    assert record.profile == "deep"
    assert record.actual_parameters["enable_thinking"] is True
    assert record.actual_parameters["thinking_budget"] == 2048
    assert seen["config"].provider_type == "aliyun_bailian"
    assert seen["request"].inference.effective_mode == "deep"
