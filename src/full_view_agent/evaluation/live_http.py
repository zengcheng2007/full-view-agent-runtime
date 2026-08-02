import os
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from full_view_agent.application.deployment_capabilities import (
    parse_housing_next_area_enabled,
)
from full_view_agent.evaluation.http_environment import HttpEvalEnvironment
from full_view_agent.evaluation.live import build_live_eval_runner
from full_view_agent.evaluation.runner import EvalRunner


class LiveHttpEvalSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="FULL_VIEW_",
        extra="ignore",
    )

    legacy_gateway_url: str = Field(default="http://127.0.0.1:9666", min_length=1)
    governance_base_url: str = Field(
        default="http://127.0.0.1:9666/geo-qxst",
        min_length=1,
    )
    p0_allowed_user_ids: str = ""
    housing_next_area_enabled: str = "false"


def build_live_http_eval_runner(
    env_file: Path,
    *,
    orchestrator: Literal["native", "langgraph"] = "native",
) -> EvalRunner:
    token = os.environ.get("FULL_VIEW_EVAL_GEO_TOKEN", "").strip()
    if not token:
        raise RuntimeError(
            "FULL_VIEW_EVAL_GEO_TOKEN must be set in the current process environment"
        )

    settings_factory: Any = LiveHttpEvalSettings
    settings: LiveHttpEvalSettings = settings_factory(
        _env_file=env_file,
        _env_file_encoding="utf-8",
    )
    allowed_user_ids = {
        user_id.strip()
        for user_id in settings.p0_allowed_user_ids.split(",")
        if user_id.strip()
    }
    if not allowed_user_ids:
        raise RuntimeError(
            "FULL_VIEW_P0_ALLOWED_USER_IDS must explicitly allow the evaluation user"
        )

    environment = HttpEvalEnvironment(
        raw_token=SecretStr(token),
        legacy_gateway_url=settings.legacy_gateway_url,
        governance_base_url=settings.governance_base_url,
        p0_allowed_user_ids=allowed_user_ids,
        housing_next_area_enabled=parse_housing_next_area_enabled(
            settings.housing_next_area_enabled
        ),
    )
    return build_live_eval_runner(
        env_file,
        environment=environment,
        orchestrator=orchestrator,
    )
