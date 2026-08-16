from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from full_view_agent.application.model_inference_policy import (
    model_reasoning_capability_from_environment,
    resolve_model_inference_options,
)
from full_view_agent.application.model_planner import (
    _redacted_model_request_trace,
    _redacted_model_response_trace,
)
from full_view_agent.application.model_provider import (
    ModelInferenceOptions,
    ModelMessage,
    ModelRequest,
)
from full_view_agent.domain.agent_definition import AgentExecutionPolicy
from full_view_agent.domain.capability import (
    ModelConfig,
    ModelReasoningCapability,
    ModelReasoningProfile,
)
from full_view_agent.domain.models import (
    ClientCapabilities,
    MessageInput,
    RunCreateRequest,
    TextContent,
)
from full_view_agent.infrastructure.openai_compatible_model import (
    OpenAICompatibleModelProvider,
)


def _model_config(*, capability: ModelReasoningCapability) -> ModelConfig:
    return ModelConfig(
        config_id="model_deepseek",
        name="DeepSeek V4 Flash",
        api_base_url="https://example.test/v1",
        model_name="deepseek-v4-flash-0731",
        reasoning_capability=capability,
    )


def test_agent_execution_policy_controls_allowed_and_default_modes() -> None:
    policy = AgentExecutionPolicy(
        default_inference_mode="auto",
        allowed_inference_modes=("fast", "auto", "deep"),
    )

    assert policy.default_inference_mode == "auto"
    assert policy.allowed_inference_modes == ("fast", "auto", "deep")

    with pytest.raises(ValueError, match="default inference mode"):
        AgentExecutionPolicy(
            default_inference_mode="deep",
            allowed_inference_modes=("fast", "auto"),
        )


def test_run_create_request_accepts_user_inference_mode() -> None:
    request = RunCreateRequest(
        input=MessageInput(
            client_message_id="msg_1",
            content=[TextContent(type="text", text="查询杭州市人口最少的街道")],
        ),
        client=ClientCapabilities(client_instance_id="browser_1"),
        inference_mode="fast",
    )

    assert request.inference_mode == "fast"

    omitted = RunCreateRequest(
        input=request.input,
        client=request.client,
    )
    assert omitted.inference_mode is None


def test_v030_migration_persists_model_and_run_reasoning_profiles() -> None:
    migration = (
        Path(__file__).parents[1]
        / "scripts"
        / "migrations"
        / "V030_model_reasoning_profiles.sql"
    ).read_text(encoding="utf-8")

    assert "ALTER TABLE full_view_agent.model_configs" in migration
    assert "ALTER TABLE full_view_agent.run_model_config_snapshots" in migration
    assert migration.count("ADD COLUMN IF NOT EXISTS reasoning_capability JSONB") == 2
    assert "SELECT 30" not in migration
    assert "VALUES (30)" in migration


def test_hybrid_model_resolves_fast_and_deep_provider_options() -> None:
    capability = ModelReasoningCapability(
        mode="hybrid",
        fast_profile=ModelReasoningProfile(enable_thinking=False),
        deep_profile=ModelReasoningProfile(
            enable_thinking=True,
            reasoning_effort="high",
        ),
    )
    config = _model_config(capability=capability)

    fast = resolve_model_inference_options(
        requested_mode="fast",
        latest_user_text="查询杭州市人口最少的街道",
        execution_policy=AgentExecutionPolicy(),
        model_config=config,
    )
    deep = resolve_model_inference_options(
        requested_mode="deep",
        latest_user_text="分析人口变化背后的可能原因并给出证据限制",
        execution_policy=AgentExecutionPolicy(),
        model_config=config,
    )

    assert fast == ModelInferenceOptions(
        requested_mode="fast",
        effective_mode="fast",
        enable_thinking=False,
    )
    assert deep == ModelInferenceOptions(
        requested_mode="deep",
        effective_mode="deep",
        enable_thinking=True,
        reasoning_effort="high",
    )


def test_auto_mode_uses_fast_for_query_and_deep_for_open_analysis() -> None:
    config = _model_config(
        capability=ModelReasoningCapability(
            mode="hybrid",
            fast_profile=ModelReasoningProfile(enable_thinking=False),
            deep_profile=ModelReasoningProfile(
                enable_thinking=True,
                reasoning_effort="high",
            ),
        )
    )
    policy = AgentExecutionPolicy()

    query = resolve_model_inference_options(
        requested_mode="auto",
        latest_user_text="查询杭州市人口最少的街道",
        execution_policy=policy,
        model_config=config,
    )
    analysis = resolve_model_inference_options(
        requested_mode="auto",
        latest_user_text="综合人口、房屋和企业数据分析原因、风险与对策",
        execution_policy=policy,
        model_config=config,
    )

    assert query.effective_mode == "fast"
    assert query.enable_thinking is False
    assert analysis.effective_mode == "deep"
    assert analysis.enable_thinking is True


def test_omitted_user_mode_uses_agent_default_inference_mode() -> None:
    config = _model_config(
        capability=ModelReasoningCapability(
            mode="hybrid",
            fast_profile=ModelReasoningProfile(enable_thinking=False),
            deep_profile=ModelReasoningProfile(
                enable_thinking=True,
                reasoning_effort="high",
            ),
        )
    )
    options = resolve_model_inference_options(
        requested_mode=None,
        latest_user_text="查询人口",
        execution_policy=AgentExecutionPolicy(
            default_inference_mode="deep",
            allowed_inference_modes=("fast", "auto", "deep"),
        ),
        model_config=config,
    )

    assert options.requested_mode == "deep"
    assert options.effective_mode == "deep"
    assert options.enable_thinking is True


def test_reasoning_only_model_rejects_fast_mode() -> None:
    config = _model_config(
        capability=ModelReasoningCapability(
            mode="reasoning_only",
            deep_profile=ModelReasoningProfile(enable_thinking=True),
        )
    )

    with pytest.raises(ValueError, match="does not support fast mode"):
        resolve_model_inference_options(
            requested_mode="fast",
            latest_user_text="查询人口",
            execution_policy=AgentExecutionPolicy(),
            model_config=config,
        )


def test_environment_reasoning_capability_is_explicit_and_typed(monkeypatch) -> None:
    monkeypatch.setenv(
        "FULL_VIEW_MODEL_REASONING_CAPABILITY",
        '{"mode":"hybrid","fast_profile":{"enable_thinking":false},'
        '"deep_profile":{"enable_thinking":true,"reasoning_effort":"high"}}',
    )

    capability = model_reasoning_capability_from_environment()

    assert capability.mode == "hybrid"
    assert capability.fast_profile is not None
    assert capability.fast_profile.enable_thinking is False
    assert capability.deep_profile is not None
    assert capability.deep_profile.reasoning_effort == "high"


def test_environment_reasoning_capability_rejects_invalid_json(monkeypatch) -> None:
    monkeypatch.setenv("FULL_VIEW_MODEL_REASONING_CAPABILITY", "not-json")

    with pytest.raises(ValueError, match="FULL_VIEW_MODEL_REASONING_CAPABILITY"):
        model_reasoning_capability_from_environment()


@pytest.mark.asyncio
async def test_openai_compatible_provider_serializes_only_typed_inference_options() -> None:
    seen_payloads: list[dict[str, object]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen_payloads.append(dict(httpx.Response(200, request=request).json()) if False else {})
        import json

        seen_payloads[-1] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"content": "ok", "tool_calls": []},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 2,
                    "total_tokens": 12,
                    "completion_tokens_details": {"reasoning_tokens": 0},
                },
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = OpenAICompatibleModelProvider(
            base_url="https://example.test/v1",
            model="deepseek-v4-flash-0731",
            client=client,
        )
        response = await provider.complete(
            ModelRequest(
                messages=(ModelMessage(role="user", content="hello"),),
                inference=ModelInferenceOptions(
                    requested_mode="deep",
                    effective_mode="deep",
                    enable_thinking=True,
                    reasoning_effort="high",
                ),
            )
        )

    assert seen_payloads == [
        {
            "model": "deepseek-v4-flash-0731",
            "messages": [{"role": "user", "content": "hello"}],
            "enable_thinking": True,
            "reasoning_effort": "high",
        }
    ]
    assert response.usage.reasoning_tokens == 0

    request_trace = _redacted_model_request_trace(
        ModelRequest(
            messages=(ModelMessage(role="user", content="hello"),),
            inference=ModelInferenceOptions(
                requested_mode="auto",
                effective_mode="fast",
                enable_thinking=False,
            ),
        ),
        model_turn=1,
    )
    response_trace = _redacted_model_response_trace(response, model_turn=1)
    assert request_trace["requested_inference_mode"] == "auto"
    assert request_trace["effective_inference_mode"] == "fast"
    assert request_trace["thinking_enabled"] is False
    assert response_trace["usage"]["reasoning_tokens"] == 0
