import json

import pytest

from full_view_agent.application.context_builder import AgentContextBuilder
from full_view_agent.application.harness import HarnessState
from full_view_agent.application.session_run_service import SessionRunService
from full_view_agent.application.tool_registry import ToolRegistry
from full_view_agent.domain.models import (
    AgentMessage,
    PopulationMetricRow,
    PopulationMetricTable,
    TableDataResult,
    TextContent,
    ToolResult,
)
from full_view_agent.infrastructure.memory_store import InMemoryAgentStore

from .test_policy import population_auth_context
from .test_session_run_service import run_request


@pytest.mark.asyncio
async def test_context_builder_uses_messages_and_only_authorized_tools() -> None:
    store = InMemoryAgentStore()
    service = SessionRunService(store)
    session = await service.create_session(user_id="user-01", title="模型上下文")
    run = await service.create_run(
        user_id="user-01",
        session_id=session.session_id,
        request=run_request(),
    )
    auth_context = population_auth_context().model_copy(
        update={"session_id": session.session_id, "run_id": run.run_id}
    )
    builder = AgentContextBuilder(store=store, registry=ToolRegistry.default())

    request = await builder.build(
        user_id="user-01",
        auth_context=auth_context,
        state=HarnessState(),
    )

    serialized_messages = json.dumps(
        [message.__dict__ for message in request.messages],
        ensure_ascii=False,
    )
    assert "查询独居老人数量" in serialized_messages
    assert "330106" in serialized_messages
    assert auth_context.credential_ref not in serialized_messages
    assert [tool.tool_id for tool in request.tools] == [
        "governance.query_population_metrics"
    ]
    assert request.prompt_version == "full-view-governance-readonly-v4"
    assert "需要业务数据时必须调用" in request.messages[0].content
    assert "solitary_elderly" in request.messages[0].content
    assert "区县按街道" in request.messages[0].content
    assert "不代表任何业务指标为零" in request.messages[0].content
    assert request.tools[0].input_schema["type"] == "object"
    assert "query" in request.tools[0].input_schema["properties"]
    assert "solitary_elderly" in request.tools[0].description
    assert "group_by" in request.tools[0].description


@pytest.mark.asyncio
async def test_context_builder_adds_sanitized_tool_observations() -> None:
    store = InMemoryAgentStore()
    service = SessionRunService(store)
    session = await service.create_session(user_id="user-01", title="Tool 观察")
    run = await service.create_run(
        user_id="user-01",
        session_id=session.session_id,
        request=run_request(),
    )
    auth_context = population_auth_context().model_copy(
        update={"session_id": session.session_id, "run_id": run.run_id}
    )
    builder = AgentContextBuilder(store=store, registry=ToolRegistry.default())
    state = HarnessState(
        tool_results=(
            ToolResult(
                tool_call_id="tcl-01",
                tool_id="governance.query_population_metrics",
                tool_version="1.0.0",
                status="denied",
                summary="该区划不在授权范围",
                warnings=["AREA_OUT_OF_SCOPE"],
            ),
        )
    )

    request = await builder.build(
        user_id="user-01",
        auth_context=auth_context,
        state=state,
    )

    observation = request.messages[-1]
    assert observation.role == "system"
    assert "该区划不在授权范围" in observation.content
    assert "AREA_OUT_OF_SCOPE" in observation.content
    assert "policy_fingerprint" not in observation.content


@pytest.mark.asyncio
async def test_context_builder_stops_advertising_a_successful_tool() -> None:
    store = InMemoryAgentStore()
    service = SessionRunService(store)
    session = await service.create_session(user_id="user-01", title="完成后总结")
    run = await service.create_run(
        user_id="user-01",
        session_id=session.session_id,
        request=run_request(),
    )
    auth_context = population_auth_context().model_copy(
        update={"session_id": session.session_id, "run_id": run.run_id}
    )
    builder = AgentContextBuilder(store=store, registry=ToolRegistry.default())
    state = HarnessState(
        tool_results=(
            ToolResult(
                tool_call_id="tcl-success-01",
                tool_id="governance.query_population_metrics",
                tool_version="1.0.0",
                status="success",
                summary="Tool 执行成功。",
                data_result=TableDataResult(
                    result_id="res-success-01",
                    data_schema_ref=(
                        "schema://data/population-metric-table/1.0.0"
                    ),
                    result_fingerprint="sha256:test-success",
                    data=PopulationMetricTable(
                        rows=[
                            PopulationMetricRow(
                                area_code="330106001",
                                area_name="示例街道",
                                person_count=128,
                            )
                        ]
                    ),
                    row_count=1,
                ),
            ),
        )
    )

    request = await builder.build(
        user_id="user-01",
        auth_context=auth_context,
        state=state,
    )

    assert request.tools == ()
    assert "Tool 执行成功" in request.messages[-1].content


@pytest.mark.asyncio
async def test_context_builder_stops_advertising_tool_after_upstream_timeout() -> None:
    store = InMemoryAgentStore()
    service = SessionRunService(store)
    session = await service.create_session(user_id="user-01", title="超时后停止重试")
    run = await service.create_run(
        user_id="user-01",
        session_id=session.session_id,
        request=run_request(),
    )
    auth_context = population_auth_context().model_copy(
        update={"session_id": session.session_id, "run_id": run.run_id}
    )
    builder = AgentContextBuilder(store=store, registry=ToolRegistry.default())
    state = HarnessState(
        tool_results=(
            ToolResult(
                tool_call_id="tcl-timeout-01",
                tool_id="governance.query_population_metrics",
                tool_version="1.0.0",
                status="failed",
                summary="现有业务服务暂时不可用。",
                warnings=["upstream_timeout"],
            ),
        )
    )

    request = await builder.build(
        user_id="user-01",
        auth_context=auth_context,
        state=state,
    )

    assert request.tools == ()
    assert "upstream_timeout" in request.messages[-1].content
    assert "不得重试" in request.messages[0].content


@pytest.mark.asyncio
async def test_context_builder_bounded_context_summarizes_old_messages() -> None:
    store = InMemoryAgentStore()
    service = SessionRunService(store)
    session = await service.create_session(user_id="user-bc", title="长会话")
    run = await service.create_run(
        user_id="user-bc",
        session_id=session.session_id,
        request=run_request(),
    )
    await service.start_run(user_id="user-bc", run_id=run.run_id)
    for i in range(25):
        msg = AgentMessage(
            message_id=f"msg-{i}",
            session_id=session.session_id,
            run_id=run.run_id,
            role="user" if i % 2 == 0 else "assistant",
            content=[TextContent(type="text", text=f"第 {i + 1} 条消息")],
        )
        await store.save_message(
            user_id="user-bc",
            run_id=run.run_id,
            message=msg,
        )
    auth_context = population_auth_context().model_copy(
        update={"session_id": session.session_id, "run_id": run.run_id}
    )
    builder = AgentContextBuilder(
        store=store, registry=ToolRegistry.default(), max_context_messages=6
    )

    request = await builder.build(
        user_id="user-bc",
        auth_context=auth_context,
        state=HarnessState(),
    )

    system_messages = [m for m in request.messages if m.role == "system"]
    summary = next(
        (m for m in system_messages if "会话摘要" in m.content), None
    )
    assert summary is not None, "should contain a summary for old messages"
    assert "更早的" in summary.content and "条" in summary.content
    non_summary_user = [
        m
        for m in request.messages
        if m.role == "user" and "会话摘要" not in m.content
    ]
    assert len(non_summary_user) <= 6
