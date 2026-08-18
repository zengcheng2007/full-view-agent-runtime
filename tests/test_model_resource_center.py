"""Model resource centre contract and lifecycle tests.

These tests intentionally exercise the public service surface rather than HTTP
implementation details.  API tests cover the transport mapping separately.
"""

from __future__ import annotations

import pytest

from full_view_agent.application.errors import RunStateConflict
from full_view_agent.application.model_config_service import (
    InMemoryModelConfigKeyStore,
    ModelConfigService,
)
from full_view_agent.domain import capability as capability_domain
from full_view_agent.domain.capability import ModelParameterProfile, ModelParameterProfiles
from full_view_agent.infrastructure.capability_repository import (
    InMemoryModelConfigRepository,
)


def _service() -> ModelConfigService:
    return ModelConfigService(
        repository=InMemoryModelConfigRepository(),
        key_store=InMemoryModelConfigKeyStore(),
    )


def test_model_domain_exposes_resource_centre_contracts() -> None:
    """The domain must name lifecycle, capabilities, versions, tests and audit."""

    required = {
        "ModelLifecycleStatus",
        "ModelCapabilityDeclaration",
        "ModelParameterProfile",
        "ModelParameterProfiles",
        "ModelConfigVersion",
        "ModelTestRecord",
        "ModelAuditEvent",
        "ModelUsage",
    }

    missing = sorted(name for name in required if not hasattr(capability_domain, name))
    assert not missing, f"missing model resource contracts: {missing}"


@pytest.mark.asyncio
async def test_new_model_is_a_versioned_draft_with_safe_capabilities() -> None:
    service = _service()

    result = await service.create_config(
        name="Bailian DeepSeek",
        api_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        api_key="secret",
        model_name="deepseek-v4",
        provider_type="aliyun_bailian",
        created_by="admin",
    )

    assert result.lifecycle == "draft"
    assert result.version == 1
    assert result.etag == 1
    assert result.provider_type == "aliyun_bailian"
    assert result.capabilities.tool_calling is False
    assert result.api_key_masked == "***"


@pytest.mark.asyncio
async def test_publish_requires_persisted_required_tests() -> None:
    service = _service()
    created = await service.create_config(
        name="Untested",
        api_base_url="https://example.com/v1",
        api_key="secret",
        model_name="model",
        created_by="admin",
    )

    with pytest.raises(RunStateConflict, match="required model tests"):
        await service.publish_config(
            config_id=created.config_id,
            expected_etag=created.etag,
            actor="admin",
            reason="production rollout",
        )


@pytest.mark.asyncio
async def test_required_tests_promote_draft_to_tested_lifecycle() -> None:
    service = _service()
    created = await service.create_config(
        name="Verified",
        api_base_url="https://example.com/v1",
        api_key="secret",
        model_name="model",
        created_by="admin",
    )
    await service.record_test_result(
        config_id=created.config_id,
        kind="connection",
        success=True,
        tested_by="admin",
    )
    await service.record_test_result(
        config_id=created.config_id,
        kind="chat",
        success=True,
        tested_by="admin",
    )

    tested = await service.get_config(created.config_id)
    assert tested.lifecycle == "tested"
    assert tested.etag > created.etag


@pytest.mark.asyncio
async def test_update_requires_matching_etag_and_reason() -> None:
    service = _service()
    created = await service.create_config(
        name="Draft",
        api_base_url="https://example.com/v1",
        api_key="secret",
        model_name="model",
        created_by="admin",
    )

    with pytest.raises(RunStateConflict, match="etag"):
        await service.update_config(
            config_id=created.config_id,
            updated_by="admin",
            expected_etag=created.etag + 1,
            reason="correct display name",
            name="Changed",
        )


@pytest.mark.asyncio
async def test_legacy_runtime_never_chooses_first_published_model() -> None:
    service = _service()
    first = await service.create_config(
        name="First",
        api_base_url="https://first.example/v1",
        api_key="first",
        model_name="first",
        created_by="admin",
    )
    second = await service.create_config(
        name="Second",
        api_base_url="https://second.example/v1",
        api_key="second",
        model_name="second",
        created_by="admin",
    )

    for config in (first, second):
        for kind in ("connection", "chat"):
            await service.record_test_result(
                config_id=config.config_id,
                kind=kind,
                success=True,
                tested_by="admin",
            )
        tested = await service.get_config(config.config_id)
        await service.publish_config(
            config_id=config.config_id,
            expected_etag=tested.etag,
            actor="admin",
            reason="publish verified model",
        )

    with pytest.raises(RunStateConflict, match="explicit legacy default"):
        await service.resolve_for_runtime()


@pytest.mark.asyncio
async def test_private_model_endpoint_is_rejected_fail_closed() -> None:
    service = _service()

    with pytest.raises(RunStateConflict, match="SSRF"):
        await service.create_config(
            name="Metadata",
            api_base_url="http://169.254.169.254/latest",
            api_key="secret",
            model_name="metadata",
            created_by="admin",
        )


@pytest.mark.asyncio
async def test_published_version_is_immutable_and_new_version_is_a_draft() -> None:
    service = _service()
    created = await service.create_config(
        name="Versioned",
        api_base_url="https://example.com/v1",
        api_key="secret",
        model_name="model-v1",
        created_by="admin",
    )
    for kind in ("connection", "chat"):
        await service.record_test_result(
            config_id=created.config_id, kind=kind, success=True, tested_by="admin"
        )
    tested = await service.get_config(created.config_id)
    published = await service.publish_config(
        config_id=created.config_id,
        expected_etag=tested.etag,
        actor="admin",
        reason="approved",
    )

    with pytest.raises(RunStateConflict, match="immutable"):
        await service.update_config(
            config_id=created.config_id,
            updated_by="admin",
            expected_etag=published.etag,
            reason="must not overwrite",
            model_name="model-v2",
        )

    draft = await service.create_version(
        config_id=created.config_id,
        expected_etag=published.etag,
        actor="admin",
        reason="upgrade model",
        changes={"model_name": "model-v2"},
    )
    assert draft.version == 2
    assert draft.lifecycle == "draft"
    assert draft.model_name == "model-v2"
    versions = await service.list_versions(created.config_id)
    assert [(item.version, item.lifecycle) for item in versions] == [
        (1, "published"),
        (2, "draft"),
    ]


@pytest.mark.asyncio
async def test_test_record_redacts_secrets_and_audit_is_persisted() -> None:
    service = _service()
    created = await service.create_config(
        name="Audited",
        api_base_url="https://example.com/v1",
        api_key="secret",
        model_name="model",
        created_by="admin",
    )

    record = await service.record_test_result(
        config_id=created.config_id,
        kind="connection",
        success=True,
        tested_by="admin",
        actual_parameters={"api_key": "must-not-leak", "max_tokens": 1},
    )

    assert "api_key" not in record.actual_parameters
    assert record.actual_parameters == {"max_tokens": 1}
    events = await service.list_audit_events(created.config_id)
    assert [event.action for event in events] == ["create", "test"]


def test_provider_options_are_controlled_per_provider() -> None:
    with pytest.raises(ValueError, match="unsupported aliyun_bailian"):
        capability_domain.ModelConfig(
            config_id="mconf_bad",
            name="Bad options",
            api_base_url="https://example.com/v1",
            model_name="model",
            provider_type="aliyun_bailian",
            parameter_profiles=ModelParameterProfiles(
                fast=ModelParameterProfile(
                    provider_options={"arbitrary_header": "secret"}
                )
            ),
        )
