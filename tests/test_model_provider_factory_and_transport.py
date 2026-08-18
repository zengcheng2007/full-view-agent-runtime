from __future__ import annotations

import json
import ssl
from collections.abc import Awaitable, Callable
from pathlib import Path

import httpx
import pytest

from full_view_agent.application.model_config_repository import ModelConfigSnapshot
from full_view_agent.application.model_provider import (
    ModelInferenceOptions,
    ModelMessage,
    ModelRequest,
)
from full_view_agent.domain.capability import (
    ModelConfigWithKey,
    ModelParameterProfile,
    ModelParameterProfiles,
)
from full_view_agent.infrastructure.model_provider_factory import build_model_provider
from full_view_agent.infrastructure.secure_model_transport import (
    ModelEndpointResolver,
    ModelEndpointSecurityError,
    PinnedAsyncNetworkBackend,
    ResolvedModelEndpoint,
    SecureModelHttpTransport,
)


def _openai_response(content: str = "OK") -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [
                {"finish_reason": "stop", "message": {"content": content}}
            ],
            "usage": {
                "prompt_tokens": 2,
                "completion_tokens": 1,
                "total_tokens": 3,
            },
        },
    )


def _config(
    provider_type: str,
    *,
    profiles: ModelParameterProfiles | None = None,
) -> ModelConfigWithKey:
    return ModelConfigWithKey(
        config_id=f"{provider_type}-model",
        name=provider_type,
        api_base_url="https://models.example/v1",
        api_key_secret="secret",
        model_name="model-name",
        protocol="openai_compatible",
        provider_type=provider_type,
        timeout_seconds=30,
        max_output_tokens=4096,
        max_retries=0,
        parameter_profiles=profiles or ModelParameterProfiles(),
        is_enabled=True,
    )


def test_snapshot_materialisation_preserves_provider_and_parameter_profiles() -> None:
    profiles = ModelParameterProfiles(
        fast=ModelParameterProfile(
            temperature=0.1,
            provider_options={"enable_thinking": False},
        )
    )
    snapshot = ModelConfigSnapshot(
        config_id="bailian",
        config_version=3,
        name="Bailian",
        api_base_url="https://models.example/v1",
        model_name="qwen",
        protocol="openai_compatible",
        provider_type="aliyun_bailian",
        parameter_profiles=profiles,
        timeout_seconds=30,
        max_output_tokens=4096,
        max_retries=0,
        api_key_ciphertext=b"cipher",
        api_key_nonce=b"nonce",
    )

    restored = snapshot.materialise_with_key(plaintext_key="plain")

    assert restored.provider_type == "aliyun_bailian"
    assert restored.parameter_profiles == profiles


def test_snapshot_migration_persists_provider_and_parameter_profiles() -> None:
    migration = (
        Path(__file__).parents[1]
        / "scripts"
        / "migrations"
        / "V032_model_reasoning_test_profile.sql"
    ).read_text(encoding="utf-8")

    assert "ALTER TABLE full_view_agent.run_model_config_snapshots" in migration
    assert "provider_type" in migration
    assert "parameter_profiles" in migration


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider_type", "provider_options", "expected"),
    [
        (
            "aliyun_bailian",
            {"enable_thinking": True, "thinking_budget": 2048},
            {"enable_thinking": True, "thinking_budget": 2048},
        ),
        (
            "openai",
            {"reasoning_effort": "high", "verbosity": "low"},
            {"reasoning_effort": "high", "verbosity": "low"},
        ),
        (
            "openai_compatible",
            {"enable_thinking": True, "parallel_tool_calls": False},
            {"enable_thinking": True, "parallel_tool_calls": False},
        ),
    ],
)
async def test_factory_maps_selected_profile_to_openai_family_request(
    provider_type: str,
    provider_options: dict[str, object],
    expected: dict[str, object],
) -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _openai_response()

    profiles = ModelParameterProfiles(
        deep=ModelParameterProfile(
            temperature=0.2,
            top_p=0.8,
            max_output_tokens=512,
            provider_options=provider_options,
        )
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = build_model_provider(
            _config(provider_type, profiles=profiles),
            client=client,
        )
        await provider.complete(
            ModelRequest(
                messages=(ModelMessage(role="user", content="analyse"),),
                max_output_tokens=1024,
                inference=ModelInferenceOptions(
                    requested_mode="deep",
                    effective_mode="deep",
                ),
            )
        )

    payload = json.loads(captured[0].content)
    assert payload["temperature"] == 0.2
    assert payload["top_p"] == 0.8
    assert payload["max_tokens"] == 512
    assert {key: payload[key] for key in expected} == expected


@pytest.mark.asyncio
async def test_factory_builds_anthropic_request_and_parses_response() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            json={
                "content": [{"type": "text", "text": "done"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 5, "output_tokens": 2},
            },
        )

    profiles = ModelParameterProfiles(
        deep=ModelParameterProfile(
            temperature=0.3,
            provider_options={"thinking_budget": 4096},
        )
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = build_model_provider(
            _config("anthropic", profiles=profiles),
            client=client,
        )
        response = await provider.complete(
            ModelRequest(
                messages=(
                    ModelMessage(role="system", content="system rule"),
                    ModelMessage(role="user", content="analyse"),
                ),
                inference=ModelInferenceOptions(
                    requested_mode="deep",
                    effective_mode="deep",
                ),
            )
        )

    payload = json.loads(captured[0].content)
    assert captured[0].url.path == "/v1/messages"
    assert captured[0].headers["x-api-key"] == "secret"
    assert captured[0].headers["anthropic-version"] == "2023-06-01"
    assert payload["system"] == "system rule"
    assert payload["thinking"] == {"type": "enabled", "budget_tokens": 4096}
    assert response.content == "done"
    assert response.usage.total_tokens == 7


@pytest.mark.asyncio
async def test_endpoint_is_resolved_again_and_private_rebinding_fails_closed() -> None:
    answers = iter(
        [
            ("93.184.216.34",),
            ("127.0.0.1",),
        ]
    )
    calls = 0

    async def resolve(_host: str, _port: int) -> tuple[str, ...]:
        nonlocal calls
        calls += 1
        return next(answers)

    resolver = ModelEndpointResolver(resolve_host=resolve)
    first = await resolver.resolve("https://models.example/v1/chat/completions")
    assert first.pinned_ip == "93.184.216.34"

    with pytest.raises(ModelEndpointSecurityError, match="non-public"):
        await resolver.resolve("https://models.example/v1/chat/completions")

    assert calls == 2


@pytest.mark.asyncio
async def test_secure_transport_resolves_and_pins_each_request() -> None:
    answers = iter([("93.184.216.34",), ("93.184.216.35",)])

    async def resolve(_host: str, _port: int) -> tuple[str, ...]:
        return next(answers)

    pinned: list[str] = []

    def client_factory(endpoint: ResolvedModelEndpoint) -> httpx.AsyncClient:
        pinned.append(endpoint.pinned_ip)
        return httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _request: _openai_response())
        )

    transport = SecureModelHttpTransport(
        resolver=ModelEndpointResolver(resolve_host=resolve),
        client_factory=client_factory,
    )
    for _ in range(2):
        await transport.post(
            "https://models.example/v1/chat/completions",
            json={"model": "m"},
            headers={},
            timeout=3,
        )

    assert pinned == ["93.184.216.34", "93.184.216.35"]


@pytest.mark.asyncio
async def test_secure_transport_refuses_redirect_without_following_it() -> None:
    requests: list[httpx.Request] = []

    def client_factory(_endpoint: ResolvedModelEndpoint) -> httpx.AsyncClient:
        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(307, headers={"location": "https://other.example"})

        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    transport = SecureModelHttpTransport(
        resolver=ModelEndpointResolver(
            resolve_host=_static_resolver("93.184.216.34")
        ),
        client_factory=client_factory,
    )

    with pytest.raises(ModelEndpointSecurityError, match="redirects"):
        await transport.post(
            "https://models.example/v1/chat/completions",
            json={"model": "m"},
            headers={},
            timeout=3,
        )

    assert len(requests) == 1


class _RecordingStream:
    def __init__(self) -> None:
        self.tls_hostnames: list[str | None] = []

    async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        del max_bytes, timeout
        return b""

    async def write(self, buffer: bytes, timeout: float | None = None) -> None:
        del buffer, timeout

    async def aclose(self) -> None:
        return None

    async def start_tls(
        self,
        ssl_context: ssl.SSLContext,
        server_hostname: str | None = None,
        timeout: float | None = None,
    ) -> _RecordingStream:
        del ssl_context, timeout
        self.tls_hostnames.append(server_hostname)
        return self

    def get_extra_info(self, info: str) -> object:
        del info
        return None


class _RecordingBackend:
    def __init__(self) -> None:
        self.hosts: list[str] = []
        self.stream = _RecordingStream()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: object = None,
    ) -> _RecordingStream:
        del port, timeout, local_address, socket_options
        self.hosts.append(host)
        return self.stream

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: object = None,
    ) -> _RecordingStream:
        del path, timeout, socket_options
        raise AssertionError("unix sockets are not valid model endpoints")

    async def sleep(self, seconds: float) -> None:
        del seconds


@pytest.mark.asyncio
async def test_pinned_backend_connects_to_validated_ip_without_changing_tls_hostname() -> None:
    delegate = _RecordingBackend()
    endpoint = await ModelEndpointResolver(
        resolve_host=_static_resolver("93.184.216.34")
    ).resolve("https://models.example/v1/chat/completions")
    backend = PinnedAsyncNetworkBackend(endpoint=endpoint, delegate=delegate)

    stream = await backend.connect_tcp("models.example", 443)
    await stream.start_tls(ssl.create_default_context(), "models.example")

    assert delegate.hosts == ["93.184.216.34"]
    assert delegate.stream.tls_hostnames == ["models.example"]


def _static_resolver(
    address: str,
) -> Callable[[str, int], Awaitable[tuple[str, ...]]]:
    async def resolve(_host: str, _port: int) -> tuple[str, ...]:
        return (address,)

    return resolve
