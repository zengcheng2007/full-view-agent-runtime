"""Single construction path for tested and business model providers."""

from __future__ import annotations

import httpx
from pydantic import SecretStr

from full_view_agent.application.model_provider import ModelProvider
from full_view_agent.domain.capability import ModelConfigWithKey
from full_view_agent.infrastructure.anthropic_model import AnthropicModelProvider
from full_view_agent.infrastructure.openai_compatible_model import (
    OpenAICompatibleModelProvider,
)
from full_view_agent.infrastructure.secure_model_transport import SecureModelHttpTransport


def build_model_provider(
    config: ModelConfigWithKey,
    *,
    client: httpx.AsyncClient | None = None,
    secure_transport: SecureModelHttpTransport | None = None,
) -> ModelProvider:
    """Build the provider declared by an immutable model config version."""
    api_key = SecretStr(config.api_key_secret)
    if config.provider_type == "anthropic":
        return AnthropicModelProvider(
            base_url=config.api_base_url,
            model=config.model_name,
            api_key=api_key,
            timeout_seconds=float(config.timeout_seconds),
            max_output_tokens=config.max_output_tokens,
            parameter_profiles=config.parameter_profiles,
            client=client,
            secure_transport=secure_transport,
        )
    return OpenAICompatibleModelProvider(
        base_url=config.api_base_url,
        model=config.model_name,
        api_key=api_key,
        timeout_seconds=float(config.timeout_seconds),
        provider_type=config.provider_type,
        parameter_profiles=config.parameter_profiles,
        client=client,
        secure_transport=secure_transport,
    )
