import json

import httpx
import pytest
from pydantic import SecretStr

from full_view_agent.application import errors
from full_view_agent.application.answer_claims import (
    FINISH_TOOL_DESCRIPTION,
    FINISH_TOOL_ID,
    FINISH_TOOL_INPUT_SCHEMA,
)
from full_view_agent.application.model_provider import (
    ModelMessage,
    ModelRequest,
    ModelToolDefinition,
)
from full_view_agent.infrastructure.openai_compatible_model import (
    OpenAICompatibleModelProvider,
)


@pytest.mark.asyncio
async def test_openai_compatible_provider_parses_a_structured_tool_call() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call-01",
                                    "type": "function",
                                    "function": {
                                        "name": "governance__resolve_area",
                                        "arguments": json.dumps(
                                            {"query": "西湖区"},
                                            ensure_ascii=False,
                                        ),
                                    },
                                }
                            ],
                        },
                    }
                ],
                "usage": {
                    "prompt_tokens": 120,
                    "completion_tokens": 20,
                    "total_tokens": 140,
                },
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = OpenAICompatibleModelProvider(
            base_url="http://model.test/v1",
            model="qwen-test",
            api_key=SecretStr("model-secret"),
            client=client,
        )
        response = await provider.complete(
            ModelRequest(
                messages=(ModelMessage(role="user", content="西湖区独居老人数量"),),
                max_output_tokens=256,
                tools=(
                    ModelToolDefinition(
                        tool_id="governance.resolve_area",
                        description="解析标准区划",
                        input_schema={
                            "type": "object",
                            "properties": {"query": {"type": "string"}},
                            "required": ["query"],
                        },
                        server_arguments={
                            "catalog_fingerprint": "server-secret-fingerprint"
                        },
                    ),
                ),
            )
        )

    payload = json.loads(captured[0].content)
    assert captured[0].url.path == "/v1/chat/completions"
    assert captured[0].headers["authorization"] == "Bearer model-secret"
    assert payload["parallel_tool_calls"] is False
    assert payload["max_tokens"] == 256
    assert payload["tools"][0]["function"]["name"] == "governance__resolve_area"
    assert "server-secret-fingerprint" not in captured[0].content.decode("utf-8")
    assert response.tool_calls[0].tool_id == "governance.resolve_area"
    assert response.tool_calls[0].arguments == {"query": "西湖区"}
    assert response.usage.total_tokens == 140


@pytest.mark.asyncio
async def test_openai_provider_round_trips_reserved_finish_tool() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "finish-01",
                                    "type": "function",
                                    "function": {
                                        "name": "full_view__finish_answer",
                                        "arguments": json.dumps(
                                            {
                                                "kind": "reference_only",
                                                "summary": "查询完成",
                                                "claims": [],
                                            },
                                            ensure_ascii=False,
                                        ),
                                    },
                                }
                            ],
                        },
                    }
                ],
                "usage": {
                    "prompt_tokens": 20,
                    "completion_tokens": 5,
                    "total_tokens": 25,
                },
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        response = await OpenAICompatibleModelProvider(
            base_url="http://model.test/v1", model="qwen-test", client=client
        ).complete(
            ModelRequest(
                messages=(ModelMessage(role="user", content="展示结果"),),
                tools=(
                    ModelToolDefinition(
                        tool_id=FINISH_TOOL_ID,
                        description=FINISH_TOOL_DESCRIPTION,
                        input_schema=FINISH_TOOL_INPUT_SCHEMA,
                    ),
                ),
            )
        )

    payload = json.loads(captured[0].content)
    assert payload["tools"][0]["function"]["name"] == "full_view__finish_answer"
    assert response.tool_calls[0].tool_id == FINISH_TOOL_ID
    assert response.tool_calls[0].arguments["kind"] == "reference_only"


@pytest.mark.asyncio
async def test_openai_compatible_provider_rejects_malformed_tool_arguments() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "content": None,
                            "tool_calls": [
                                {
                                    "type": "function",
                                    "function": {
                                        "name": "governance__resolve_area",
                                        "arguments": "{not-json",
                                    },
                                }
                            ],
                        },
                    }
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = OpenAICompatibleModelProvider(
            base_url="http://model.test/v1",
            model="qwen-test",
            client=client,
        )
        request = ModelRequest(
            messages=(ModelMessage(role="user", content="西湖区"),),
            tools=(
                ModelToolDefinition(
                    tool_id="governance.resolve_area",
                    description="解析标准区划",
                    input_schema={"type": "object"},
                ),
            ),
        )

        with pytest.raises(errors.ApplicationError) as exc_info:
            await provider.complete(request)

    assert exc_info.value.code == "model_contract_error"


@pytest.mark.asyncio
async def test_openai_compatible_provider_normalizes_timeout_without_retrying() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ReadTimeout("model timed out", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = OpenAICompatibleModelProvider(
            base_url="http://model.test/v1",
            model="qwen-test",
            client=client,
        )

        with pytest.raises(errors.ApplicationError) as exc_info:
            await provider.complete(
                ModelRequest(
                    messages=(ModelMessage(role="user", content="查询独居老人"),)
                )
            )

    assert exc_info.value.code == "model_timeout"
    assert attempts == 1


@pytest.mark.asyncio
async def test_openai_compatible_provider_applies_timeout_and_parses_text() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": "分析已完成"},
                    }
                ],
                "usage": {
                    "prompt_tokens": 20,
                    "completion_tokens": 5,
                    "total_tokens": 25,
                },
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = OpenAICompatibleModelProvider(
            base_url="http://model.test/v1",
            model="qwen-test",
            timeout_seconds=12.5,
            client=client,
        )
        response = await provider.complete(
            ModelRequest(
                messages=(ModelMessage(role="user", content="查询独居老人"),)
            )
        )

    assert captured[0].extensions["timeout"]["read"] == 12.5
    assert response.content == "分析已完成"
    assert response.tool_calls == ()


@pytest.mark.asyncio
async def test_openai_compatible_provider_parses_text_with_null_tool_calls() -> None:
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": "分析已完成",
                            "tool_calls": None,
                        },
                    }
                ],
                "usage": {
                    "prompt_tokens": 20,
                    "completion_tokens": 5,
                    "total_tokens": 25,
                },
            },
        )
    )
    async with httpx.AsyncClient(transport=transport) as client:
        provider = OpenAICompatibleModelProvider(
            base_url="http://model.test/v1",
            model="qwen-test",
            client=client,
        )

        response = await provider.complete(
            ModelRequest(messages=(ModelMessage(role="user", content="分析住房数据"),))
        )

    assert response.content == "分析已完成"
    assert response.tool_calls == ()


@pytest.mark.parametrize("malformed_tool_calls", [{}, "not-a-list"])
@pytest.mark.asyncio
async def test_openai_compatible_provider_rejects_non_list_tool_calls(
    malformed_tool_calls: object,
) -> None:
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": "分析已完成",
                            "tool_calls": malformed_tool_calls,
                        },
                    }
                ],
                "usage": {
                    "prompt_tokens": 20,
                    "completion_tokens": 5,
                    "total_tokens": 25,
                },
            },
        )
    )
    async with httpx.AsyncClient(transport=transport) as client:
        provider = OpenAICompatibleModelProvider(
            base_url="http://model.test/v1",
            model="qwen-test",
            client=client,
        )

        with pytest.raises(errors.ModelContractError):
            await provider.complete(
                ModelRequest(
                    messages=(ModelMessage(role="user", content="分析住房数据"),)
                )
            )


@pytest.mark.asyncio
async def test_openai_compatible_provider_rejects_responses_without_token_usage() -> None:
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": "分析已完成"},
                    }
                ]
            },
        )
    )
    async with httpx.AsyncClient(transport=transport) as client:
        provider = OpenAICompatibleModelProvider(
            base_url="http://model.test/v1",
            model="qwen-test",
            client=client,
        )

        with pytest.raises(errors.ModelContractError, match="token usage"):
            await provider.complete(
                ModelRequest(messages=(ModelMessage(role="user", content="查询独居老人"),))
            )
