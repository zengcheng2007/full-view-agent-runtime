from pathlib import Path
from typing import Any, Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from full_view_agent.evaluation.runner import EvalEnvironment, EvalRunner
from full_view_agent.infrastructure.openai_compatible_model import (
    OpenAICompatibleModelProvider,
)


class LiveModelSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="FULL_VIEW_MODEL_",
        extra="ignore",
    )

    provider: Literal["openai_compatible"]
    base_url: str = Field(min_length=1)
    name: str = Field(min_length=1)
    api_key: SecretStr
    timeout_seconds: float = Field(default=60.0, gt=0)
    token_budget: int = Field(default=32_000, gt=0)


def build_live_eval_runner(
    env_file: Path,
    *,
    environment: EvalEnvironment | None = None,
) -> EvalRunner:
    settings_factory: Any = LiveModelSettings
    settings: LiveModelSettings = settings_factory(
        _env_file=env_file,
        _env_file_encoding="utf-8",
    )
    provider = OpenAICompatibleModelProvider(
        base_url=settings.base_url,
        model=settings.name,
        api_key=settings.api_key,
        timeout_seconds=settings.timeout_seconds,
    )
    return EvalRunner(
        provider=provider,
        model_provider=settings.provider,
        model_name=settings.name,
        max_total_tokens=settings.token_budget,
        environment=environment,
    )
