import os
import re
import subprocess
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from full_view_agent.evaluation.runner import EvalEnvironment, EvalRunner
from full_view_agent.infrastructure.openai_compatible_model import (
    OpenAICompatibleModelProvider,
)

_SAFE_RUNTIME_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}")


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


def resolve_runtime_version(*, repo_root: Path | None = None) -> str:
    explicit_version = os.environ.get("FULL_VIEW_RUNTIME_VERSION", "").strip()
    if explicit_version:
        if _SAFE_RUNTIME_VERSION.fullmatch(explicit_version) is None:
            raise RuntimeError(
                "FULL_VIEW_RUNTIME_VERSION must be a safe version identifier"
            )
        return explicit_version

    root = repo_root or Path(__file__).resolve().parents[3]
    try:
        head_result = subprocess.run(
            ["git", "rev-parse", "--short=12", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
            timeout=2,
        )
        short_sha = head_result.stdout.strip().lower()
        if (
            head_result.returncode != 0
            or re.fullmatch(r"[0-9a-f]{7,40}", short_sha) is None
        ):
            return "unknown"
        status_result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
            timeout=2,
        )
        if status_result.returncode != 0:
            return "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"

    suffix = "-dirty" if status_result.stdout.strip() else ""
    return f"{short_sha}{suffix}"


def build_live_eval_runner(
    env_file: Path,
    *,
    environment: EvalEnvironment | None = None,
    orchestrator: Literal["native", "langgraph"] = "native",
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
        orchestrator=orchestrator,
        runtime_version=resolve_runtime_version(),
    )
