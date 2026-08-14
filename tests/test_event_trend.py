from datetime import date
from urllib.parse import parse_qs

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from full_view_agent.application import errors
from full_view_agent.application.harness import ToolAction
from full_view_agent.application.policy import MinimalPolicyAdapter
from full_view_agent.application.tool_observation_service import _with_presentation
from full_view_agent.application.tool_registry import ToolRegistry
from full_view_agent.domain import models
from full_view_agent.infrastructure.governance_adapter import HttpGovernanceAdapter
from full_view_agent.semantic import SemanticCatalog, SemanticCompiler, SemanticQuerySpec

from .test_policy import population_auth_context


class _CredentialBroker:
    async def resolve(self, **_kwargs: object) -> SecretStr:
        return SecretStr("run-scoped-token")


def _auth_context():
    context = population_auth_context()
    return context.model_copy(
        update={
            "entitlements": [
                "governance.area.read",
                "governance.event.aggregate.read",
            ],
            "data_scopes": context.data_scopes.model_copy(
                update={"datasets": ["administrative_area", "event"]}
            ),
        }
    )


def _trend_arguments(**overrides: object) -> models.QueryEventMetricsInput:
    query: dict[str, object] = {
        "metrics": ["event_count"],
        "scope": {"area_code": "330106"},
        "group_by": ["month"],
        "time_range": {"start": "2026-01-01", "end": "2026-04-30"},
        "limit": 24,
    }
    query.update(overrides)
    return models.QueryEventMetricsInput.model_validate({"query": query})


def test_event_trend_input_requires_exact_controlled_shape() -> None:
    validated = _trend_arguments()

    assert validated.query.metrics == ["event_count"]
    assert validated.query.group_by == ["month"]
    assert validated.query.time_range is not None
    assert validated.query.time_range.start == date(2026, 1, 1)
    assert validated.query.time_range.end == date(2026, 4, 30)


@pytest.mark.parametrize(
    "query_update",
    [
        {"time_range": None},
        {"time_range": {"start": "2026/01/01", "end": "2026-04-30"}},
        {"time_range": {"start": "2020-12-31", "end": "2021-01-31"}},
        {"time_range": {"start": "2026-04-30", "end": "2026-01-01"}},
        {"time_range": {"start": "2021-01-01", "end": "2023-01-02"}},
        {"metrics": ["event_count"], "group_by": []},
        {"metrics": ["finish_rate"], "group_by": ["month"]},
    ],
)
def test_event_trend_input_rejects_unsafe_shapes(
    query_update: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        _trend_arguments(**query_update)


def test_semantic_compiler_emits_event_trend_arguments() -> None:
    spec = SemanticQuerySpec.model_validate(
        {
            "subject": "event",
            "metrics": ["event_count"],
            "scope": {"area_code": "330106", "include_descendants": True},
            "group_by": ["month"],
            "time_range": {"start": "2026-01-01", "end": "2026-04-30"},
            "output": "table",
        }
    )

    plan = SemanticCompiler(SemanticCatalog.default()).compile(spec)

    assert plan.expected_result.data_schema_ref == (
        "schema://data/event-trend-table/1.0.0"
    )
    assert plan.steps[0].arguments == {
        "query": {
            "schema_version": "1.1",
            "metrics": ["event_count"],
            "scope": {"area_code": "330106", "include_descendants": True},
            "group_by": ["month"],
            "time_range": {"start": "2026-01-01", "end": "2026-04-30"},
            "limit": 200,
        }
    }


async def _execute_trend(
    handler: httpx.MockTransport,
) -> models.TableDataResult:
    arguments = _trend_arguments()
    manifest = ToolRegistry.default().get_manifest("governance.query_event_metrics")
    auth_context = _auth_context()
    policy = MinimalPolicyAdapter().evaluate(
        manifest=manifest,
        auth_context=auth_context,
        arguments=arguments,
    )
    async with httpx.AsyncClient(transport=handler) as client:
        return await HttpGovernanceAdapter(
            base_url="http://legacy.test/geo-qxst",
            credential_broker=_CredentialBroker(),
            client=client,
        ).execute(
            manifest=manifest,
            arguments=arguments,
            policy_decision=policy,
            auth_context=auth_context,
        )


@pytest.mark.asyncio
async def test_http_event_trend_uses_exact_form_contract_and_zero_fills_months() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "state": True,
                "code": 200,
                "msg": "",
                "data": [
                    {"month": "2026-04", "total": "4"},
                    {"month": "2026-01", "total": 1},
                    {"month": "2026-03", "total": 3},
                ],
            },
        )

    result = await _execute_trend(httpx.MockTransport(handler))

    assert len(requests) == 1
    request = requests[0]
    assert request.method == "POST"
    assert request.url.path == "/geo-qxst/event/getEventCountByMonth"
    assert request.headers["geoToken"] == "run-scoped-token"
    assert request.headers["content-type"].startswith(
        "application/x-www-form-urlencoded"
    )
    assert parse_qs(request.content.decode()) == {
        "areaName": ["county_code"],
        "areaCode": ["330106"],
        "startDate": ["2026-01-01"],
        "endDate": ["2026-04-30"],
    }
    assert result.data_schema_ref == "schema://data/event-trend-table/1.0.0"
    assert [row.model_dump() for row in result.data.rows] == [
        {"month": "2026-01", "event_count": 1},
        {"month": "2026-02", "event_count": 0},
        {"month": "2026-03", "event_count": 3},
        {"month": "2026-04", "event_count": 4},
    ]


@pytest.mark.parametrize(
    "rows",
    [
        [{"month": "2026-01", "total": True}],
        [{"month": "2026-01", "total": 1.0}],
        [{"month": "2026-01", "total": -1}],
        [{"month": "2026-01", "total": "1.0"}],
        [{"month": "2026-13", "total": 1}],
        [
            {"month": "2026-01", "total": 1},
            {"month": "2026-01", "total": 2},
        ],
    ],
)
@pytest.mark.asyncio
async def test_http_event_trend_rejects_invalid_upstream_rows(
    rows: list[dict[str, object]],
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"state": True, "code": 200, "msg": "", "data": rows},
        )

    with pytest.raises(errors.UpstreamContractError):
        await _execute_trend(httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_event_trend_presentation_is_chinese_line_table_and_csv() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "state": True,
                "code": 200,
                "msg": "",
                "data": [{"month": "2026-01", "total": 2}],
            },
        )

    result = await _execute_trend(httpx.MockTransport(handler))
    presented = _with_presentation(
        result,
        action=ToolAction(
            tool_id="governance.query_event_metrics",
            arguments=_trend_arguments().model_dump(mode="json"),
        ),
    )

    assert presented.presentation is not None
    assert presented.presentation.title == "事件总数月度趋势"
    assert presented.presentation.fields[0].label == "月份"
    assert presented.presentation.fields[1].label == "事件总数"
    line = next(
        item for item in presented.presentation.visualizations if item.kind == "line"
    )
    assert line.x_field == "month"
    assert line.y_field == "event_count"
    assert presented.presentation.download is not None
    assert presented.presentation.download.formats == ["csv"]
    assert "缺失月份按 0 补齐" in presented.presentation.summary
    serialized = presented.presentation.model_dump_json()
    assert "上报" not in serialized
    assert "处置" not in serialized
