"""Anthropic Messages API adapter for the runtime model contract."""

from __future__ import annotations

import json

import httpx
from pydantic import SecretStr

from full_view_agent.application.errors import (
    ModelContractError,
    ModelProviderTimeout,
    ModelProviderUnavailable,
)
from full_view_agent.application.model_provider import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ModelToolCall,
    ModelUsage,
)
from full_view_agent.domain.capability import ModelParameterProfile, ModelParameterProfiles
from full_view_agent.infrastructure.secure_model_transport import SecureModelHttpTransport


class AnthropicModelProvider:
    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: SecretStr,
        timeout_seconds: float,
        max_output_tokens: int,
        parameter_profiles: ModelParameterProfiles,
        client: httpx.AsyncClient | None = None,
        secure_transport: SecureModelHttpTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._api_key = api_key
        self._timeout_seconds = timeout_seconds
        self._max_output_tokens = max_output_tokens
        self._profiles = parameter_profiles
        self._client = client
        self._secure_transport = secure_transport or SecureModelHttpTransport()

    async def complete(self, request: ModelRequest) -> ModelResponse:
        profile = self._profile_for(request)
        payload = self._payload(request, profile)
        headers = {
            "x-api-key": self._api_key.get_secret_value(),
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        try:
            if self._client is None:
                response = await self._secure_transport.post(
                    f"{self._base_url}/messages",
                    json=payload,
                    headers=headers,
                    timeout=self._timeout_seconds,
                )
            else:
                response = await self._client.post(
                    f"{self._base_url}/messages",
                    json=payload,
                    headers=headers,
                    timeout=self._timeout_seconds,
                )
            response.raise_for_status()
        except httpx.TimeoutException as exc:
            raise ModelProviderTimeout("model request timed out") from exc
        except httpx.HTTPError as exc:
            raise ModelProviderUnavailable("model provider is unavailable") from exc
        return _parse_anthropic_response(response, request)

    def _profile_for(self, request: ModelRequest) -> ModelParameterProfile:
        if request.inference is None:
            return self._profiles.standard
        if request.inference.effective_mode == "deep":
            return self._profiles.deep
        return self._profiles.fast

    def _payload(
        self,
        request: ModelRequest,
        profile: ModelParameterProfile,
    ) -> dict[str, object]:
        unknown = set(profile.provider_options) - {"thinking_budget", "tool_choice"}
        if unknown:
            raise ModelContractError(
                f"unsupported anthropic provider parameters: {sorted(unknown)}"
            )
        system = "\n\n".join(
            message.content or ""
            for message in request.messages
            if message.role == "system"
        )
        output_limits = [
            value
            for value in (
                request.max_output_tokens,
                profile.max_output_tokens,
                self._max_output_tokens,
            )
            if value is not None
        ]
        payload: dict[str, object] = {
            "model": self._model,
            "messages": [
                _serialize_anthropic_message(message)
                for message in request.messages
                if message.role != "system"
            ],
            "max_tokens": min(output_limits),
        }
        if system:
            payload["system"] = system
        if profile.temperature is not None:
            payload["temperature"] = profile.temperature
        if profile.top_p is not None:
            payload["top_p"] = profile.top_p
        thinking_budget = profile.provider_options.get("thinking_budget")
        if thinking_budget is not None:
            payload["thinking"] = {
                "type": "enabled",
                "budget_tokens": thinking_budget,
            }
        if request.tools:
            payload["tools"] = [
                {
                    "name": _model_tool_name(tool.tool_id),
                    "description": tool.description,
                    "input_schema": tool.input_schema,
                }
                for tool in request.tools
            ]
        tool_choice = profile.provider_options.get("tool_choice")
        if tool_choice is not None:
            payload["tool_choice"] = tool_choice
        return payload


def _serialize_anthropic_message(message: ModelMessage) -> dict[str, object]:
    if message.role == "tool":
        return {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": message.tool_call_id or "",
                    "content": message.content or "",
                }
            ],
        }
    if message.role == "assistant" and message.tool_calls:
        content: list[dict[str, object]] = []
        if message.content:
            content.append({"type": "text", "text": message.content})
        content.extend(
            {
                "type": "tool_use",
                "id": call.call_id or f"call_{index}",
                "name": _model_tool_name(call.tool_id),
                "input": call.arguments,
            }
            for index, call in enumerate(message.tool_calls)
        )
        return {"role": "assistant", "content": content}
    return {"role": message.role, "content": message.content or ""}


def _parse_anthropic_response(
    response: httpx.Response,
    request: ModelRequest,
) -> ModelResponse:
    tool_names = {
        _model_tool_name(tool.tool_id): tool.tool_id for tool in request.tools
    }
    try:
        body = response.json()
        raw_content = body["content"]
        usage = body["usage"]
        input_tokens = usage["input_tokens"]
        output_tokens = usage["output_tokens"]
        if not isinstance(raw_content, list):
            raise TypeError("content must be a list")
        texts: list[str] = []
        calls: list[ModelToolCall] = []
        for item in raw_content:
            if item.get("type") == "text":
                texts.append(str(item["text"]))
            elif item.get("type") == "tool_use":
                calls.append(
                    ModelToolCall(
                        tool_id=tool_names[item["name"]],
                        arguments=dict(item["input"]),
                        call_id=item.get("id"),
                    )
                )
        if not isinstance(input_tokens, int) or not isinstance(output_tokens, int):
            raise TypeError("usage values must be integers")
        return ModelResponse(
            content="\n".join(texts) or None,
            tool_calls=tuple(calls),
            finish_reason=str(body.get("stop_reason", "unknown")),
            usage=ModelUsage(
                prompt_tokens=input_tokens,
                completion_tokens=output_tokens,
                total_tokens=input_tokens + output_tokens,
            ),
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ModelContractError("model response violated the provider contract") from exc


def _model_tool_name(tool_id: str) -> str:
    return tool_id.replace(".", "__")
