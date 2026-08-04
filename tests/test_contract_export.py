import json
from pathlib import Path

import yaml

from full_view_agent.contract_export import export_contracts


def test_export_contracts_writes_openapi_and_versioned_json_schemas(tmp_path) -> None:
    export_contracts(tmp_path)

    openapi_path = tmp_path / "openapi" / "agent-api-v1.yaml"
    openapi = yaml.safe_load(openapi_path.read_text(encoding="utf-8"))
    assert openapi["info"]["version"] == "0.1.0"
    assert "/agent-api/v1/runs/{run_id}/events" in openapi["paths"]
    assert "/agent-api/v1/runs/{run_id}/steers" in openapi["paths"]
    assert "/agent-api/v1/runs/{run_id}/analysis-plans" in openapi["paths"]
    assert "/agent-api/v1/sessions/{session_id}/messages" in openapi["paths"]
    security_scheme = openapi["components"]["securitySchemes"]["GeoToken"]
    assert security_scheme == {"type": "apiKey", "in": "header", "name": "geoToken"}
    assert openapi["paths"]["/agent-api/v1/sessions"]["post"]["security"] == [
        {"GeoToken": []}
    ]
    session_schema = openapi["paths"]["/agent-api/v1/sessions"]["post"][
        "responses"
    ]["201"]["content"]["application/json"]["schema"]
    assert session_schema["$ref"].endswith("/SessionResponse")
    run_schema = openapi["paths"][
        "/agent-api/v1/sessions/{session_id}/runs"
    ]["post"]["responses"]["202"]["content"]["application/json"]["schema"]
    assert run_schema["$ref"].endswith("/RunResponse")
    analysis_plan_schema = openapi["paths"][
        "/agent-api/v1/runs/{run_id}/analysis-plans"
    ]["post"]["responses"]["201"]["content"]["application/json"]["schema"]
    assert analysis_plan_schema["$ref"].endswith("/AnalysisPlanResponse")
    analysis_operation = openapi["paths"][
        "/agent-api/v1/runs/{run_id}/analysis-plans"
    ]["post"]
    assert analysis_operation["security"] == [{"GeoToken": []}]
    idempotency_header = next(
        parameter
        for parameter in analysis_operation["parameters"]
        if parameter["name"] == "Idempotency-Key"
    )
    assert idempotency_header["in"] == "header"
    assert idempotency_header["required"] is True
    request_schema = analysis_operation["requestBody"]["content"][
        "application/json"
    ]["schema"]
    assert request_schema["$ref"].endswith("/AnalysisRequest")
    for status_code in ("400", "401", "404", "409", "422", "503"):
        error_schema = analysis_operation["responses"][status_code]["content"][
            "application/json"
        ]["schema"]
        assert error_schema["$ref"].endswith("/ErrorResponse")
    event_content = openapi["paths"]["/agent-api/v1/runs/{run_id}/events"][
        "get"
    ]["responses"]["200"]["content"]
    assert "text/event-stream" in event_content
    result_schema = openapi["paths"]["/agent-api/v1/results/{result_id}"]["get"][
        "responses"
    ]["200"]["content"]["application/json"]["schema"]
    assert result_schema["$ref"].endswith("/ResultResponse")

    expected_schemas = {
        "agent/run-create-request.schema.json": "RunCreateRequest",
        "agent/run-input-body.schema.json": "RunInputBody",
        "agent/pending-input-request.schema.json": "PendingInputRequest",
        "agent/agent-session.schema.json": "AgentSession",
        "agent/agent-run.schema.json": "AgentRun",
        "agent/agent-event.schema.json": "AgentEvent",
        "agent/agent-message.schema.json": "AgentMessage",
        "agent/steer.schema.json": "Steer",
        "frontend/frontend-command.schema.json": "FrontendCommand",
        "frontend/frontend-command-receipt.schema.json": "FrontendCommandReceipt",
        "common/auth-context.schema.json": "AuthContext",
        "policy/policy-decision.schema.json": "PolicyDecision",
        "tools/internal-tool-manifest.schema.json": "InternalToolManifest",
        "tools/model-tool-descriptor.schema.json": "ModelToolDescriptor",
        "tools/resolve-area-input.schema.json": "ResolveAreaInput",
        "tools/query-population-metrics-input.schema.json": (
            "QueryPopulationMetricsInput"
        ),
        "tools/get-object-profile-input.schema.json": "GetObjectProfileInput",
        "tools/tool-result.schema.json": "ToolResult",
        "data/area-candidates.schema.json": "AreaCandidatesResult",
        "data/object-profile.schema.json": "ObjectProfileResult",
        "data/table-data-result.schema.json": "TableDataResult",
        "data/result-metadata.schema.json": "ResultMetadata",
        "data/evidence.schema.json": "Evidence",
        "data/tool-specific/population-metric-table.schema.json": (
            "PopulationMetricTable"
        ),
        "data/tool-specific/housing-lease-type-table.schema.json": (
            "HousingLeaseTypeTable"
        ),
        "data/tool-specific/event-finish-rate-table.schema.json": (
            "EventFinishRateTable"
        ),
    }
    for relative_path, expected_title in expected_schemas.items():
        schema = json.loads(
            (tmp_path / "schemas" / relative_path).read_text(encoding="utf-8")
        )
        assert schema["title"] == expected_title
        assert schema["additionalProperties"] is False

    population_manifest = json.loads(
        (
            tmp_path
            / "manifests"
            / "governance"
            / "query-population-metrics.manifest.json"
        ).read_text(encoding="utf-8")
    )
    assert population_manifest["adapter_ref"] == (
        "adapter://geo-qxst/population-metrics/1.0"
    )
    descriptors = json.loads(
        (
            tmp_path
            / "manifests"
            / "governance"
            / "model-tool-descriptors.json"
        ).read_text(encoding="utf-8")
    )
    assert [item["tool_id"] for item in descriptors] == [
        "governance.get_object_profile",
        "governance.query_event_metrics",
        "governance.query_housing_metrics",
        "governance.query_population_metrics",
        "governance.resolve_area",
    ]
    assert all("adapter_ref" not in item for item in descriptors)
    for descriptor in descriptors:
        schema_ref = descriptor["input_schema"]["$ref"]
        schema_name = schema_ref.split("/")[-2]
        assert (
            tmp_path / "schemas" / "tools" / f"{schema_name}.schema.json"
        ).is_file()


def test_exported_contracts_cover_the_analysis_report_union(tmp_path) -> None:
    export_contracts(tmp_path)

    result_metadata = json.loads(
        (tmp_path / "schemas" / "data" / "result-metadata.schema.json").read_text(
            encoding="utf-8"
        )
    )
    assert "analysis_report" in result_metadata["properties"]["kind"]["enum"]

    tool_result = json.loads(
        (tmp_path / "schemas" / "tools" / "tool-result.schema.json").read_text(
            encoding="utf-8"
        )
    )
    assert "AnalysisReportDataResult" in tool_result["$defs"]
    data_result = tool_result["properties"]["data_result"]["anyOf"][0]
    assert (
        data_result["discriminator"]["mapping"]["analysis_report"]
        == "#/$defs/AnalysisReportDataResult"
    )
    assert {"$ref": "#/$defs/AnalysisReportDataResult"} in data_result["oneOf"]

    openapi = yaml.safe_load(
        (tmp_path / "openapi" / "agent-api-v1.yaml").read_text(encoding="utf-8")
    )
    assert "AnalysisReportDataResult" in openapi["components"]["schemas"]


def test_committed_contracts_cover_the_analysis_report_union() -> None:
    committed_root = Path(__file__).resolve().parents[2] / "contracts"

    result_metadata = json.loads(
        (
            committed_root / "schemas" / "data" / "result-metadata.schema.json"
        ).read_text(encoding="utf-8")
    )
    assert "analysis_report" in result_metadata["properties"]["kind"]["enum"]

    tool_result = json.loads(
        (
            committed_root / "schemas" / "tools" / "tool-result.schema.json"
        ).read_text(encoding="utf-8")
    )
    assert "AnalysisReportDataResult" in tool_result["$defs"]

    openapi = yaml.safe_load(
        (committed_root / "openapi" / "agent-api-v1.yaml").read_text(encoding="utf-8")
    )
    assert "AnalysisReportDataResult" in openapi["components"]["schemas"]


def test_committed_contracts_match_fresh_export(tmp_path) -> None:
    export_contracts(tmp_path)
    committed_root = Path(__file__).resolve().parents[2] / "contracts"

    generated = {
        path.relative_to(tmp_path): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }
    committed = {
        path.relative_to(committed_root): path.read_bytes()
        for path in committed_root.rglob("*")
        if path.is_file()
    }

    assert committed == generated
