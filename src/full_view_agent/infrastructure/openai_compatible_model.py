import json
import logging

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

logger = logging.getLogger(__name__)
_MAX_CONTRACT_ATTEMPTS = 2
_RETRYABLE_CONTRACT_REASONS = frozenset({"invalid_tool_arguments_json"})


class OpenAICompatibleModelProvider:
    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: SecretStr | None = None,
        timeout_seconds: float = 60.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._api_key = api_key
        self._timeout_seconds = timeout_seconds
        self._client = client

    async def complete(self, request: ModelRequest) -> ModelResponse:
        tool_names = {
            _model_tool_name(tool.tool_id): tool.tool_id for tool in request.tools
        }
        payload: dict[str, object] = {
            "model": self._model,
            "messages": [
                _serialize_message(message) for message in request.messages
            ],
        }
        if request.max_output_tokens is not None:
            payload["max_tokens"] = request.max_output_tokens
        if request.tools:
            payload.update(
                {
                    "tools": [
                        {
                            "type": "function",
                            "function": {
                                "name": _model_tool_name(tool.tool_id),
                                "description": tool.description,
                                "parameters": tool.input_schema,
                            },
                        }
                        for tool in request.tools
                    ],
                    "tool_choice": "auto",
                    "parallel_tool_calls": False,
                }
            )
        headers = (
            {"Authorization": f"Bearer {self._api_key.get_secret_value()}"}
            if self._api_key is not None
            else {}
        )
        for attempt in range(_MAX_CONTRACT_ATTEMPTS):
            try:
                if self._client is None:
                    async with httpx.AsyncClient() as client:
                        response = await client.post(
                            f"{self._base_url}/chat/completions",
                            json=payload,
                            headers=headers,
                            timeout=self._timeout_seconds,
                        )
                else:
                    response = await self._client.post(
                        f"{self._base_url}/chat/completions",
                        json=payload,
                        headers=headers,
                        timeout=self._timeout_seconds,
                    )
                response.raise_for_status()
            except httpx.TimeoutException as exc:
                raise ModelProviderTimeout("model request timed out") from exc
            except httpx.HTTPError as exc:
                raise ModelProviderUnavailable("model provider is unavailable") from exc
            try:
                return _parse_response(response, tool_names=tool_names)
            except ModelContractError as exc:
                reason = _contract_failure_reason(exc)
                if (
                    reason in _RETRYABLE_CONTRACT_REASONS
                    and attempt + 1 < _MAX_CONTRACT_ATTEMPTS
                ):
                    logger.warning(
                        "model response contract violation; retrying once (reason=%s)",
                        reason,
                    )
                    continue
                raise
        raise RuntimeError("unreachable model contract retry state")


def _parse_response(
    response: httpx.Response,
    *,
    tool_names: dict[str, str],
) -> ModelResponse:
    try:
        body = response.json()
        choice = body["choices"][0]
        message = choice["message"]
        parsed_calls: list[ModelToolCall] = []
        raw_tool_calls = message.get("tool_calls")
        if raw_tool_calls is None:
            raw_tool_calls = []
        elif not isinstance(raw_tool_calls, list):
            raise TypeError("tool_calls must be a list or null")
        for item in raw_tool_calls:
            raw_arguments = item["function"]["arguments"]
            arguments = (
                dict(raw_arguments)
                if isinstance(raw_arguments, dict)
                else json.loads(raw_arguments)
            )
            if not isinstance(arguments, dict):
                raise TypeError("tool arguments must be an object")
            parsed_calls.append(
                ModelToolCall(
                    tool_id=tool_names[item["function"]["name"]],
                    arguments=arguments,
                )
            )
        usage = body.get("usage")
        if not isinstance(usage, dict):
            raise ModelContractError("model response omitted valid token usage")
        prompt_tokens = usage.get("prompt_tokens")
        completion_tokens = usage.get("completion_tokens")
        total_tokens = usage.get("total_tokens")
        if (
            not isinstance(prompt_tokens, int)
            or not isinstance(completion_tokens, int)
            or not isinstance(total_tokens, int)
            or prompt_tokens < 0
            or completion_tokens < 0
            or total_tokens <= 0
        ):
            raise ModelContractError("model response omitted valid token usage")
        return ModelResponse(
            content=message.get("content"),
            tool_calls=tuple(parsed_calls),
            finish_reason=choice.get("finish_reason", "unknown"),
            usage=ModelUsage(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
            ),
        )
    except ModelContractError:
        raise
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise ModelContractError("model response violated the provider contract") from exc


def _contract_failure_reason(exc: ModelContractError) -> str:
    if "token usage" in str(exc):
        return "invalid_token_usage"
    cause = exc.__cause__
    if isinstance(cause, json.JSONDecodeError):
        return "invalid_tool_arguments_json"
    if isinstance(cause, KeyError):
        return "missing_or_unknown_response_field"
    if isinstance(cause, IndexError):
        return "missing_choice"
    if isinstance(cause, TypeError):
        return "invalid_response_field_type"
    if isinstance(cause, ValueError):
        return "invalid_response_value"
    return "invalid_response_contract"


def _serialize_message(message: ModelMessage) -> dict[str, object]:
    """Serialize a ModelMessage to OpenAI-compatible format."""
    if message.role == "tool":
        return {
            "role": "tool",
            "tool_call_id": message.tool_call_id or "",
            "content": message.content or "",
        }
    if message.role == "assistant" and message.tool_calls:
        return {
            "role": "assistant",
            "content": message.content,
            "tool_calls": [
                {
                    "id": tc.call_id or f"call_{i}",
                    "type": "function",
                    "function": {
                        "name": _model_tool_name(tc.tool_id),
                        "arguments": json.dumps(
                            tc.arguments, ensure_ascii=False,
                        ),
                    },
                }
                for i, tc in enumerate(message.tool_calls)
            ],
        }
    return {"role": message.role, "content": message.content or ""}


def _model_tool_name(tool_id: str) -> str:
    return tool_id.replace(".", "__")
