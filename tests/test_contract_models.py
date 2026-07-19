import pytest
from pydantic import ValidationError

from full_view_agent.domain.models import (
    AgentEvent,
    AgentRun,
    FrontendCommand,
    FrontendCommandPreconditions,
    MapRenderChoroplethPayload,
    PanelShowTablePayload,
    RunCreateRequest,
    RunInputBody,
    ToolResult,
)


def test_frontend_map_command_uses_a_typed_result_reference() -> None:
    command = FrontendCommand(
        command_id="cmd-map-01",
        run_id="run-map-01",
        target_client_instance_id="agent-web-client",
        type="map.render_choropleth",
        target="map_panel",
        preconditions=FrontendCommandPreconditions(
            session_id="session-map-01",
            area_code="330106",
            required_client_capability="map.render_choropleth@1.0",
        ),
        payload=MapRenderChoroplethPayload(result_id="result-map-01"),
    )

    assert command.payload.result_id == "result-map-01"
    assert command.payload.metric_field == "person_count"
    assert command.payload.palette == "sequential_blue_5"


def test_frontend_command_rejects_a_payload_that_does_not_match_its_type() -> None:
    with pytest.raises(ValidationError):
        FrontendCommand(
            command_id="cmd-invalid-map-01",
            run_id="run-invalid-map-01",
            target_client_instance_id="agent-web-client",
            type="map.render_choropleth",
            target="map_panel",
            preconditions=FrontendCommandPreconditions(
                session_id="session-invalid-map-01",
                area_code="330106",
                required_client_capability="map.render_choropleth@1.0",
            ),
            payload=PanelShowTablePayload(result_id="result-invalid-map-01"),
        )


def test_run_create_request_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        RunCreateRequest.model_validate(
            {
                "input": {
                    "client_message_id": "web-msg-01",
                    "content": [{"type": "text", "text": "查询独居老人数量"}],
                },
                "client": {
                    "client_instance_id": "cli-01",
                    "frontend_command_schema_versions": ["1.0"],
                    "supported_commands": ["panel.show_table"],
                },
                "mode": "agent",
                "unknown": "must-not-be-silently-dropped",
            }
        )


def test_workflow_mode_requires_registered_workflow_reference() -> None:
    with pytest.raises(ValidationError):
        RunCreateRequest.model_validate(
            {
                "input": {
                    "client_message_id": "web-msg-02",
                    "content": [{"type": "text", "text": "生成治理简报"}],
                },
                "client": {
                    "client_instance_id": "cli-01",
                    "frontend_command_schema_versions": ["1.0"],
                    "supported_commands": [],
                },
                "mode": "workflow",
            }
        )


def test_partial_result_uses_completed_status_and_partial_outcome() -> None:
    run = AgentRun(
        run_id="run-01",
        session_id="session-01",
        origin_client_instance_id="cli-01",
        status="completed",
        outcome="partial",
        completion_reason_code="upstream_partial_failure",
        input_message_id="message-01",
        base_context_version=1,
    )

    assert run.status == "completed"
    assert run.outcome == "partial"


def test_non_terminal_run_rejects_terminal_outcome() -> None:
    with pytest.raises(ValidationError):
        AgentRun(
            run_id="run-02",
            session_id="session-01",
            origin_client_instance_id="cli-01",
            status="running",
            outcome="success",
            input_message_id="message-02",
            base_context_version=1,
        )


def test_agent_event_sequence_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        AgentEvent(
            event_id="evt-01",
            sequence=0,
            type="run.created",
            session_id="session-01",
            run_id="run-01",
            trace_id="trace-01",
            data={},
        )


def test_successful_tool_result_requires_typed_data_result() -> None:
    with pytest.raises(ValidationError):
        ToolResult(
            tool_call_id="tool-call-01",
            tool_id="governance.query_population_metrics",
            tool_version="1.0.0",
            status="success",
            summary="查询成功",
            data_result=None,
        )


@pytest.mark.parametrize(
    "response",
    [
        {"type": "text", "text": "西湖区"},
        {"type": "choice", "option_id": "area-330106"},
        {"type": "approval", "decision": "approve"},
        {"type": "reauthenticated"},
    ],
)
def test_run_input_supports_all_declared_waiting_response_kinds(response) -> None:
    body = RunInputBody.model_validate(
        {
            "input_request_id": "inreq-01",
            "client_instance_id": "cli-01",
            "run_state_version": 2,
            "response": response,
        }
    )

    assert body.response.type == response["type"]


def test_reauthentication_input_rejects_embedded_credentials() -> None:
    with pytest.raises(ValidationError):
        RunInputBody.model_validate(
            {
                "input_request_id": "inreq-01",
                "client_instance_id": "cli-01",
                "run_state_version": 2,
                "response": {
                    "type": "reauthenticated",
                    "geoToken": "must-stay-in-header",
                },
            }
        )
