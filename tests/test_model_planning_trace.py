from __future__ import annotations

import json

import pytest

from full_view_agent.application.answer_claims import FINISH_TOOL_ID
from full_view_agent.application.errors import ModelProviderUnavailable
from full_view_agent.application.harness import FinishAction, HarnessState
from full_view_agent.application.model_planner import ModelPlanner, ModelPlannerFactory
from full_view_agent.application.model_provider import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ModelToolCall,
    ModelToolDefinition,
    ModelUsage,
)
from full_view_agent.application.tool_registry import ToolRegistry

from .test_policy import population_auth_context


class ContextBuilder:
    async def build(self, **_kwargs: object) -> ModelRequest:
        return ModelRequest(
            messages=(ModelMessage(role="user", content="敏感用户原话"),),
            tools=(
                ModelToolDefinition(
                    tool_id="governance.semantic_query",
                    description="包含内部业务说明，不得写入轨迹正文",
                    input_schema={"type": "object", "properties": {"secret": {}}},
                    server_arguments={
                        "catalog_version": "catalog-v3",
                        "catalog_fingerprint": "sha256:catalog-safe",
                    },
                ),
            ),
            prompt_version="prompt-v7",
        )

    def for_registry(self, _registry: ToolRegistry) -> ContextBuilder:
        return self


class Provider:
    async def complete(self, _request: ModelRequest) -> ModelResponse:
        return ModelResponse(
            content="模型敏感正文",
            tool_calls=(
                ModelToolCall(
                    tool_id=FINISH_TOOL_ID,
                    arguments={"kind": "answer", "summary": "完成"},
                ),
            ),
            finish_reason="tool_calls",
            usage=ModelUsage(prompt_tokens=10, completion_tokens=4, total_tokens=14),
        )


class RecordingEvents:
    def __init__(self) -> None:
        self.records: list[dict[str, object]] = []

    async def publish(self, **kwargs: object) -> object:
        self.records.append(dict(kwargs))
        return object()


class FailingProvider:
    async def complete(self, _request: ModelRequest) -> ModelResponse:
        raise ModelProviderUnavailable("上游返回的敏感错误正文")


@pytest.mark.asyncio
async def test_model_planner_persists_only_redacted_request_and_response_metadata() -> None:
    events = RecordingEvents()
    auth_context = population_auth_context()
    planner = ModelPlanner(
        provider=Provider(),
        context_builder=ContextBuilder(),
        user_id="user-01",
        auth_context=auth_context,
        event_publisher=events,
    )

    action = await planner.decide(HarnessState())

    assert isinstance(action, FinishAction)
    assert [record["event_type"] for record in events.records] == [
        "model.requested",
        "model.responded",
    ]
    request_data = events.records[0]["data"]
    response_data = events.records[1]["data"]
    assert isinstance(request_data, dict)
    assert request_data == {
        "model_turn": 1,
        "prompt_version": "prompt-v7",
        "message_count": 1,
        "tool_ids": ["governance.semantic_query", FINISH_TOOL_ID],
        "tool_descriptor_fingerprint": request_data["tool_descriptor_fingerprint"],
        "catalog_version": "catalog-v3",
        "catalog_fingerprint": "sha256:catalog-safe",
    }
    assert str(request_data["tool_descriptor_fingerprint"]).startswith("sha256:")
    assert response_data == {
        "model_turn": 1,
        "finish_reason": "tool_calls",
        "selected_tool_ids": [FINISH_TOOL_ID],
        "content_present": True,
        "usage": {
            "prompt_tokens": 10,
            "completion_tokens": 4,
            "total_tokens": 14,
            "reasoning_tokens": 0,
        },
    }
    serialized = json.dumps(events.records, ensure_ascii=False)
    for forbidden in (
        "敏感用户原话",
        "模型敏感正文",
        "包含内部业务说明",
        '"arguments"',
        '"messages"',
        '"input_schema"',
    ):
        assert forbidden not in serialized
    assert all(record["session_id"] == auth_context.session_id for record in events.records)
    assert all(record["run_id"] == auth_context.run_id for record in events.records)


@pytest.mark.asyncio
async def test_model_planner_records_a_normalized_failure_without_provider_message() -> None:
    events = RecordingEvents()
    planner = ModelPlanner(
        provider=FailingProvider(),
        context_builder=ContextBuilder(),
        user_id="user-01",
        auth_context=population_auth_context(),
        event_publisher=events,
    )

    with pytest.raises(ModelProviderUnavailable):
        await planner.decide(HarnessState(model_turns=4))

    assert events.records[-1]["event_type"] == "model.failed"
    assert events.records[-1]["data"] == {
        "model_turn": 5,
        "error_code": "model_provider_unavailable",
    }
    assert "敏感错误正文" not in json.dumps(events.records, ensure_ascii=False)


@pytest.mark.asyncio
async def test_registry_rebinding_preserves_the_model_trace_publisher() -> None:
    events = RecordingEvents()
    planner = ModelPlannerFactory(
        provider=Provider(),
        context_builder=ContextBuilder(),  # type: ignore[arg-type]
        event_publisher=events,
    ).for_registry(ToolRegistry(manifests=[], descriptors=[])).create(
        user_id="user-01",
        auth_context=population_auth_context(),
    )

    await planner.decide(HarnessState())

    assert [record["event_type"] for record in events.records] == [
        "model.requested",
        "model.responded",
    ]
