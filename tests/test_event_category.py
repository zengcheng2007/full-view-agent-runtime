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


def _arguments() -> models.QueryEventMetricsInput:
    return models.QueryEventMetricsInput.model_validate(
        {
            "query": {
                "metrics": ["event_count"],
                "scope": {"area_code": "330106"},
                "group_by": ["event_category"],
                "limit": 100,
            }
        }
    )


def _auth_context():
    context = population_auth_context()
    return context.model_copy(
        update={
            "entitlements": ["governance.event.aggregate.read"],
            "data_scopes": context.data_scopes.model_copy(
                update={"datasets": ["event"]}
            ),
        }
    )


async def _execute(handler: httpx.MockTransport) -> models.TableDataResult:
    arguments = _arguments()
    manifest = ToolRegistry.default(
        event_category_enabled=True
    ).get_manifest("governance.query_event_metrics")
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


def test_event_category_is_an_exact_shape_without_time_range() -> None:
    assert _arguments().query.group_by == ["event_category"]
    with pytest.raises(ValidationError):
        models.QueryEventMetricsInput.model_validate(
            {
                "query": {
                    "metrics": ["finish_rate"],
                    "scope": {"area_code": "330106"},
                    "group_by": ["event_category"],
                }
            }
        )


def test_semantic_compiler_selects_event_category_shape() -> None:
    plan = SemanticCompiler(
        SemanticCatalog.default(event_category_enabled=True)
    ).compile(
        SemanticQuerySpec.model_validate(
            {
                "subject": "event",
                "metrics": ["event_count"],
                "scope": {"area_code": "330106", "include_descendants": True},
                "group_by": ["event_category"],
                "output": "table",
            }
        )
    )

    assert plan.expected_result.data_schema_ref == (
        "schema://data/event-category-table/1.0.0"
    )
    assert plan.steps[0].arguments["query"]["group_by"] == ["event_category"]


@pytest.mark.asyncio
async def test_event_category_uses_verified_get_and_dictionary_contract() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.headers["geoToken"] == "run-scoped-token"
        if request.url.path.endswith("/api/getEventProperties"):
            return httpx.Response(
                200,
                json={
                    "state": True,
                    "code": 200,
                    "msg": "",
                    "data": [
                        {"key": "01", "doc_count": 12},
                        {"key": "02", "doc_count": "7"},
                    ],
                },
            )
        if request.url.path.endswith("/dict/getDictValue"):
            return httpx.Response(
                200,
                json={
                    "state": True,
                    "code": 200,
                    "msg": "",
                    "data": [
                        {
                            "eventtype_code1": [
                                {"dicValue": "01", "dicName": "社会治理"},
                                {"dicValue": "02", "dicName": "公共安全"},
                            ]
                        }
                    ],
                },
            )
        raise AssertionError(request.url.path)

    result = await _execute(httpx.MockTransport(handler))

    assert [(request.method, request.url.path) for request in requests] == [
        ("GET", "/geo-qxst/api/getEventProperties"),
        ("POST", "/geo-qxst/dict/getDictValue"),
    ]
    assert dict(requests[0].url.params) == {
        "areaCodeName": "county_code",
        "areaCodeValue": "330106",
        "eventType": "eventtype_code1",
    }
    assert parse_qs(requests[1].content.decode()) == {}
    assert [row.model_dump() for row in result.data.rows] == [
        {"category_code": "01", "category_name": "社会治理", "event_count": 12},
        {"category_code": "02", "category_name": "公共安全", "event_count": 7},
    ]


@pytest.mark.parametrize(
    ("rows", "dictionary"),
    [
        ([{"key": "01", "doc_count": True}], {"01": "社会治理"}),
        ([{"key": "01", "doc_count": 1.5}], {"01": "社会治理"}),
        ([{"key": "01", "doc_count": -1}], {"01": "社会治理"}),
        ([{"key": "", "doc_count": 1}], {"01": "社会治理"}),
        (
            [{"key": "01", "doc_count": 1}, {"key": "01", "doc_count": 2}],
            {"01": "社会治理"},
        ),
        ([{"key": "01", "doc_count": 1}], {"02": "公共安全"}),
        ([{"key": "01", "doc_count": 1}], {"01": ""}),
    ],
)
@pytest.mark.asyncio
async def test_event_category_fails_closed_on_malformed_or_unknown_values(
    rows: list[dict[str, object]], dictionary: dict[str, str]
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/api/getEventProperties"):
            data: object = rows
        else:
            data = [
                {
                    "eventtype_code1": [
                        {"dicValue": key, "dicName": value}
                        for key, value in dictionary.items()
                    ]
                }
            ]
        return httpx.Response(
            200, json={"state": True, "code": 200, "msg": "", "data": data}
        )

    with pytest.raises(errors.UpstreamContractError):
        await _execute(httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_event_category_presentation_is_chinese_table_bar_csv_without_map() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        data: object
        if request.url.path.endswith("/api/getEventProperties"):
            data = [{"key": "01", "doc_count": 3}]
        else:
            data = [
                {
                    "eventtype_code1": [
                        {"dicValue": "01", "dicName": "社会治理"}
                    ]
                }
            ]
        return httpx.Response(
            200, json={"state": True, "code": 200, "msg": "", "data": data}
        )

    result = await _execute(httpx.MockTransport(handler))
    presented = _with_presentation(
        result,
        action=ToolAction(
            tool_id="governance.query_event_metrics",
            arguments=_arguments().model_dump(mode="json"),
        ),
    )

    assert presented.presentation is not None
    assert presented.presentation.title == "网格事件一级分类"
    assert [view.kind for view in presented.presentation.visualizations] == [
        "table",
        "bar",
    ]
    assert presented.presentation.download is not None
    assert presented.presentation.download.formats == ["csv"]
    serialized = presented.presentation.model_dump_json()
    assert "现有主题块统计口径" in serialized
    assert "全量事件" not in serialized
