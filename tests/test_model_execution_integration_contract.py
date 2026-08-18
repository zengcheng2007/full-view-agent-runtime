"""Vertical contracts from model resources through Agent releases and Runs."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import pytest

from full_view_agent.application.errors import ModelProviderTimeout, RunStateConflict
from full_view_agent.application.model_config_repository import (
    InMemoryRunModelBindingRepository,
    ModelConfigSnapshot,
)
from full_view_agent.application.model_planner import _RunFailoverModelProvider
from full_view_agent.application.model_provider import ModelRequest, ModelResponse
from full_view_agent.domain.agent_definition import (
    AgentDefinition,
    AgentExecutionPolicy,
    AgentModelPolicy,
    AgentModelVersionRef,
    AgentReleaseSnapshot,
    AgentVersion,
)
from full_view_agent.domain.application import AgentApplicationDefinition
from full_view_agent.domain.capability import (
    ModelConfig,
    ModelReasoningCapability,
    ModelReasoningProfile,
)
from tests.test_agent_application_assembly import _service


async def _draft_agent(
    service,
    *,
    execution_policy: AgentExecutionPolicy | None = None,
) -> None:
    await service.create_agent(
        AgentDefinition(
            app_id="full_information_view",
            agent_id="governance_general_agent",
            name="治理问答智能体",
        )
    )
    await service.create_version(
        AgentVersion(
            app_id="full_information_view",
            agent_id="governance_general_agent",
            version="1.0.0",
            execution_policy=execution_policy or AgentExecutionPolicy(),
        )
    )
    await service.set_model_policy(
        app_id="full_information_view",
        agent_id="governance_general_agent",
        version="1.0.0",
        policy=AgentModelPolicy(primary_model_config_id="primary-model"),
    )


@dataclass
class _EligibleVersion:
    config: ModelConfig


@dataclass
class _EligibilityGate:
    """Model centre seam used to prove Agent publication asks for exact eligibility."""

    config: ModelConfig
    failure: Exception | None = None
    checks: list[tuple[str, int]] = field(default_factory=list)

    async def get_config(self, config_id: str) -> ModelConfig:
        assert config_id == self.config.config_id
        return self.config

    async def assert_agent_eligible(self, config_id: str, exact_version: int):
        self.checks.append((config_id, exact_version))
        if self.failure is not None:
            raise self.failure
        return _EligibleVersion(config=self.config)


@pytest.mark.asyncio
async def test_agent_validation_rejects_model_version_not_published_and_tested() -> None:
    service, _, _, _, _ = await _service()
    await _draft_agent(service)
    gate = _EligibilityGate(
        config=ModelConfig(
            config_id="primary-model",
            name="Primary",
            api_base_url="https://models.example/v1",
            model_name="primary",
            version=7,
            is_enabled=True,
        ),
        failure=RunStateConflict("model version is not published with required tests"),
    )
    service._models = gate  # type: ignore[assignment]

    report = await service.validate_version(
        app_id="full_information_view",
        agent_id="governance_general_agent",
        version="1.0.0",
    )

    assert gate.checks == [("primary-model", 7)]
    assert report.is_valid is False
    assert [issue.code for issue in report.issues] == ["MODEL_VERSION_NOT_ELIGIBLE"]


@pytest.mark.asyncio
async def test_agent_model_modes_must_match_every_selected_model() -> None:
    service, _, _, _, _ = await _service()
    await _draft_agent(
        service,
        execution_policy=AgentExecutionPolicy(
            default_inference_mode="auto",
            allowed_inference_modes=("fast", "auto", "deep"),
        ),
    )
    gate = _EligibilityGate(
        config=ModelConfig(
            config_id="primary-model",
            name="Fast only",
            api_base_url="https://models.example/v1",
            model_name="fast-only",
            version=3,
            is_enabled=True,
            reasoning_capability=ModelReasoningCapability(mode="unsupported"),
        )
    )
    service._models = gate  # type: ignore[assignment]

    report = await service.validate_version(
        app_id="full_information_view",
        agent_id="governance_general_agent",
        version="1.0.0",
    )

    assert report.is_valid is False
    assert [issue.code for issue in report.issues] == [
        "MODEL_INFERENCE_MODE_UNSUPPORTED"
    ]


@pytest.mark.asyncio
async def test_auto_mode_requires_model_that_can_execute_fast_and_deep_paths() -> None:
    service, _, _, _, _ = await _service()
    await _draft_agent(
        service,
        execution_policy=AgentExecutionPolicy(
            default_inference_mode="auto",
            allowed_inference_modes=("auto",),
        ),
    )
    gate = _EligibilityGate(
        config=ModelConfig(
            config_id="primary-model",
            name="Reasoning only",
            api_base_url="https://models.example/v1",
            model_name="reasoning-only",
            version=5,
            is_enabled=True,
            reasoning_capability=ModelReasoningCapability(
                mode="reasoning_only",
                deep_profile=ModelReasoningProfile(enable_thinking=True),
            ),
        )
    )
    service._models = gate  # type: ignore[assignment]

    report = await service.validate_version(
        app_id="full_information_view",
        agent_id="governance_general_agent",
        version="1.0.0",
    )

    assert report.is_valid is False
    assert [issue.code for issue in report.issues] == [
        "MODEL_INFERENCE_MODE_UNSUPPORTED"
    ]


@pytest.mark.asyncio
async def test_legacy_agent_never_infers_even_a_single_pool_model() -> None:
    service, _, _, models, _ = await _service()
    await service.create_agent(
        AgentDefinition(
            app_id="full_information_view",
            agent_id="governance_general_agent",
            name="治理问答智能体",
        )
    )
    await models.save(
        ModelConfig(
            config_id="only-published-model",
            name="Only model",
            api_base_url="https://models.example/v1",
            model_name="only-model",
            is_enabled=True,
        )
    )

    release = await service.ensure_legacy_baseline_release(
        app_id="full_information_view",
        agent_id="governance_general_agent",
    )

    assert release is None


@pytest.mark.asyncio
async def test_legacy_agent_uses_only_explicit_legacy_default() -> None:
    service, _, _, _, _ = await _service()
    await service.create_agent(
        AgentDefinition(
            app_id="full_information_view",
            agent_id="governance_general_agent",
            name="治理问答智能体",
        )
    )
    config = ModelConfig(
        config_id="explicit-legacy-model",
        name="Explicit legacy",
        api_base_url="https://models.example/v1",
        model_name="explicit-legacy",
        version=9,
        is_enabled=True,
        reasoning_capability=ModelReasoningCapability(
            mode="hybrid",
            fast_profile=ModelReasoningProfile(enable_thinking=False),
            deep_profile=ModelReasoningProfile(enable_thinking=True),
        ),
    )

    class _ExplicitLegacyGate(_EligibilityGate):
        async def get_legacy_default(self):
            return self.config

        async def list_configs(self):
            raise AssertionError("legacy selection must not scan the public pool")

    gate = _ExplicitLegacyGate(config=config)
    service._models = gate  # type: ignore[assignment]

    release = await service.ensure_legacy_baseline_release(
        app_id="full_information_view",
        agent_id="governance_general_agent",
    )

    assert release is not None
    assert release.model_refs[0].model_config_id == "explicit-legacy-model"
    assert gate.checks
    assert set(gate.checks) == {("explicit-legacy-model", 9)}


@pytest.mark.asyncio
async def test_model_usage_is_reverse_resolved_by_exact_version_across_apps() -> None:
    service, repository, applications, _, _ = await _service()
    await applications.save_application(
        AgentApplicationDefinition(
            app_id="regional_command",
            name="区域指挥",
            default_agent_id="regional_agent",
            identity_adapter_id="identity.regional",
        )
    )
    for app_id, agent_id, agent_name, model_version, role in (
        (
            "full_information_view",
            "governance_general_agent",
            "治理问答智能体",
            2,
            "primary",
        ),
        ("regional_command", "regional_agent", "区域指挥智能体", 3, "fallback"),
    ):
        agent = AgentDefinition(
            app_id=app_id,
            agent_id=agent_id,
            name=agent_name,
        )
        await service.create_agent(agent)
        version = AgentVersion(
            app_id=app_id,
            agent_id=agent_id,
            version="1.0.0",
        )
        await repository.save_version(version)
        await repository.publish(
            version.model_copy(update={"status": "published"}),
            AgentReleaseSnapshot(
                release_id=f"release-{agent_id}",
                app_id=app_id,
                agent_id=agent_id,
                agent_version="1.0.0",
                model_refs=(
                    AgentModelVersionRef(
                        model_config_id="shared-model",
                        config_version=model_version,
                        role=role,
                        order=0 if role == "primary" else 1,
                    ),
                ),
                published_by="admin",
                reason="tested",
            ),
        )

    usages = await service.list_model_usages(
        config_id="shared-model",
        config_version=2,
    )

    assert [
        (
            usage.app_id,
            usage.agent_id,
            usage.agent_version,
            usage.config_version,
            usage.role,
            usage.fallback_order,
        )
        for usage in usages
    ] == [
        (
            "full_information_view",
            "governance_general_agent",
            "1.0.0",
            2,
            "primary",
            None,
        )
    ]


@pytest.mark.asyncio
async def test_concurrent_turns_share_one_failover_winner_for_the_run() -> None:
    class _SlowUnavailable:
        calls = 0

        async def complete(self, request: ModelRequest) -> ModelResponse:
            del request
            self.calls += 1
            await asyncio.sleep(0.02)
            raise ModelProviderTimeout("primary timed out")

    class _HealthyFallback:
        calls = 0

        async def complete(self, request: ModelRequest) -> ModelResponse:
            del request
            self.calls += 1
            return ModelResponse(
                content=f"fallback-{self.calls}",
                tool_calls=(),
                finish_reason="stop",
            )

    def _snapshot(config_id: str) -> ModelConfigSnapshot:
        return ModelConfigSnapshot(
            config_id=config_id,
            config_version=1,
            name=config_id,
            api_base_url="https://models.example/v1",
            model_name=config_id,
            protocol="openai_compatible",
            timeout_seconds=30,
            max_output_tokens=1000,
            max_retries=0,
            api_key_ciphertext=b"ciphertext",
            api_key_nonce=b"nonce",
        )

    primary = _SlowUnavailable()
    fallback = _HealthyFallback()
    repository = InMemoryRunModelBindingRepository()
    primary_snapshot = _snapshot("primary-model")
    fallback_snapshot = _snapshot("fallback-model")
    await repository.store_binding("run-concurrent", primary_snapshot)
    await repository.store_snapshot(fallback_snapshot)
    provider = _RunFailoverModelProvider(
        (
            (primary, primary_snapshot),
            (fallback, fallback_snapshot),
        ),
        run_id="run-concurrent",
        repository=repository,
    )

    responses = await asyncio.gather(
        provider.complete(ModelRequest(messages=())),
        provider.complete(ModelRequest(messages=())),
    )

    assert [item.content for item in responses] == ["fallback-1", "fallback-2"]
    assert primary.calls == 1
    assert fallback.calls == 2
    binding = await repository.load_binding("run-concurrent")
    assert binding is not None
    assert binding.config_id == "fallback-model"
