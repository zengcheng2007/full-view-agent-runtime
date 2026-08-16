"""Resolve user/Agent inference intent into typed provider options."""

from __future__ import annotations

import json
import os
from typing import Literal

from pydantic import ValidationError

from full_view_agent.application.model_provider import ModelInferenceOptions
from full_view_agent.domain.agent_definition import AgentExecutionPolicy
from full_view_agent.domain.capability import (
    ModelConfig,
    ModelConfigWithKey,
    ModelReasoningCapability,
    ModelReasoningProfile,
)

InferenceMode = Literal["fast", "auto", "deep"]

_DEEP_ANALYSIS_TERMS = (
    "分析原因",
    "原因与对策",
    "风险与对策",
    "综合分析",
    "趋势研判",
    "影响机制",
    "证据限制",
    "多方案比较",
)


def model_reasoning_capability_from_environment() -> ModelReasoningCapability:
    """Load the environment-model reasoning contract without provider guessing."""

    variable = "FULL_VIEW_MODEL_REASONING_CAPABILITY"
    raw = os.getenv(variable, "").strip()
    if not raw:
        return ModelReasoningCapability()
    try:
        payload = json.loads(raw)
        return ModelReasoningCapability.model_validate(payload)
    except (json.JSONDecodeError, ValidationError, TypeError) as exc:
        raise ValueError(f"{variable} must contain a valid reasoning capability") from exc


def resolve_model_inference_options(
    *,
    requested_mode: InferenceMode | None,
    latest_user_text: str,
    execution_policy: AgentExecutionPolicy,
    model_config: ModelConfig | ModelConfigWithKey | None = None,
    reasoning_capability: ModelReasoningCapability | None = None,
) -> ModelInferenceOptions:
    requested_mode = requested_mode or execution_policy.default_inference_mode
    if requested_mode not in execution_policy.allowed_inference_modes:
        raise ValueError(f"inference mode {requested_mode} is not allowed by this Agent")
    selected = (
        _resolve_auto_mode(latest_user_text)
        if requested_mode == "auto"
        else requested_mode
    )
    if model_config is not None and reasoning_capability is not None:
        raise ValueError("provide model_config or reasoning_capability, not both")
    capability = (
        model_config.reasoning_capability
        if model_config is not None
        else reasoning_capability or ModelReasoningCapability()
    )
    if selected == "fast":
        if capability.mode == "reasoning_only":
            raise ValueError("configured model does not support fast mode")
        profile = capability.fast_profile
    else:
        if capability.mode == "unsupported":
            raise ValueError("configured model does not support deep mode")
        profile = capability.deep_profile
    return _options(requested_mode=requested_mode, selected=selected, profile=profile)


def _resolve_auto_mode(latest_user_text: str) -> Literal["fast", "deep"]:
    normalized = "".join(latest_user_text.split())
    return "deep" if any(term in normalized for term in _DEEP_ANALYSIS_TERMS) else "fast"


def _options(
    *,
    requested_mode: InferenceMode,
    selected: Literal["fast", "deep"],
    profile: ModelReasoningProfile | None,
) -> ModelInferenceOptions:
    return ModelInferenceOptions(
        requested_mode=requested_mode,
        effective_mode=selected,
        enable_thinking=profile.enable_thinking if profile is not None else None,
        reasoning_effort=profile.reasoning_effort if profile is not None else None,
    )
