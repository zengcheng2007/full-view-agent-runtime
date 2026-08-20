from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from full_view_agent.evaluation.http_environment import HttpEvalEnvironment
from full_view_agent.evaluation.loader import load_eval_case
from full_view_agent.evaluation.runner import EvalRunner


@pytest.mark.asyncio
async def test_http_eval_environment_uses_real_identity_admission_and_http_tool() -> None:
    seen_paths: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen_paths.append(request.url.path)
        assert request.headers["geoToken"] == "test-geo-token"
        if request.url.path == "/getUserByToken":
            return httpx.Response(
                200,
                json={
                    "state": True,
                    "code": 200,
                    "data": {
                        "systemid": "legacy-user-1",
                        "organizatedId": "legacy-org-1",
                        "roleIds": "1",
                        "areaCode": "3301",
                    },
                },
            )
        if request.url.path == "/geo-qxst/area/getAreaInfoByAreaName":
            return httpx.Response(
                200,
                json={
                    "state": True,
                    "code": 200,
                    "data": {"areaname": "西湖区", "areacode": "330106"},
                },
            )
        if request.url.path == "/geo-qxst/getNextSiteData":
            return httpx.Response(
                200,
                json={
                    "state": True,
                    "code": 200,
                    "data": [
                        {
                            "areaCode": "330106003",
                            "areaName": "灵隐街道",
                            "total": 1,
                        }
                    ],
                },
            )
        raise AssertionError(f"unexpected path: {request.url.path}")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    environment = HttpEvalEnvironment(
        raw_token=SecretStr("test-geo-token"),
        legacy_gateway_url="http://legacy.test",
        governance_base_url="http://legacy.test/geo-qxst",
        p0_allowed_user_ids={"legacy-user-1"},
        client=client,
    )
    # S1-B：生产准入钉扎 governance_analyst_v1，人口查询经语义入口进入
    # 同一规范 HTTP 链路（区划解析 + 语义入口）。
    case = load_eval_case(
        Path(__file__).parents[1]
        / "evals"
        / "cases"
        / "planning-population-http-success.yaml"
    )

    trace = await EvalRunner(
        environment=environment,
        runtime_version="runtime-test-123",
    ).run(case)

    assert trace.passed is True
    assert trace.environment_kind == "live_http"
    assert trace.evidence_source_system == "legacy_geo_qxst"
    assert trace.runtime_version == "runtime-test-123"
    assert [
        summary.model_dump(mode="json") for summary in trace.outbound_requests
    ] == [
        {"method": "GET", "path": "/getUserByToken", "count": 1},
        {
            "method": "POST",
            "path": "/geo-qxst/area/getAreaInfoByAreaName",
            "count": 1,
        },
        {"method": "POST", "path": "/geo-qxst/getNextSiteData", "count": 1},
    ]
    # governance.get_object_profile is now a verified production HTTP tool
    # (no longer blocked from exposure); it is available alongside other tools.
    assert "governance.resolve_area" in trace.model_requests[0].tool_ids
    assert seen_paths == [
        "/getUserByToken",
        "/geo-qxst/area/getAreaInfoByAreaName",
        "/geo-qxst/getNextSiteData",
    ]
    serialized = trace.model_dump_json()
    assert "test-geo-token" not in serialized
    assert "credential_ref" not in serialized
    assert "geoToken" not in serialized
