"""市级人口排名：受控目标层级、确定性有界下钻与可视化结果。"""

from urllib.parse import parse_qs

import httpx
import pytest

from full_view_agent.application.capability_service import CapabilityService
from full_view_agent.application.policy import MinimalPolicyAdapter
from full_view_agent.application.prompt_catalog import build_full_view_system_prompt
from full_view_agent.application.tool_registry import ToolRegistry
from full_view_agent.domain import models
from full_view_agent.infrastructure.governance_adapter import HttpGovernanceAdapter
from full_view_agent.semantic.catalog import SemanticCatalog

from .test_http_governance_adapter import RecordingCredentialBroker
from .test_policy import population_auth_context


def _city_auth_context() -> models.AuthContext:
    base = population_auth_context()
    return base.model_copy(
        update={
            "data_scopes": base.data_scopes.model_copy(
                update={
                    "areas": [
                        models.AuthorizedAreaScope(
                            area_code="3301", include_descendants=True
                        )
                    ]
                }
            )
        }
    )


async def _execute_population(
    adapter: HttpGovernanceAdapter,
    arguments: models.QueryPopulationMetricsInput,
) -> models.TableDataResult:
    manifest = ToolRegistry.default().get_manifest(
        "governance.query_population_metrics"
    )
    auth_context = _city_auth_context()
    result = await CapabilityService(
        registry=ToolRegistry.default(),
        policy=MinimalPolicyAdapter(),
        adapter=adapter,
    ).execute(
        tool_call_id="tcl-population-city-ranking",
        tool_id=manifest.tool_id,
        raw_arguments=arguments.model_dump(mode="json"),
        auth_context=auth_context,
    )
    assert isinstance(result.data_result, models.TableDataResult)
    return result.data_result


@pytest.mark.parametrize(
    "group_by",
    [["district"], ["descendant_street"], ["descendant_community"]],
)
def test_population_contract_accepts_controlled_city_ranking_targets(
    group_by: list[str],
) -> None:
    query = models.QueryPopulationMetricsInput.model_validate(
        {
            "query": {
                "metrics": ["person_count"],
                "scope": {"area_code": "3301", "include_descendants": True},
                "group_by": group_by,
                "order_by": [{"field": "person_count", "direction": "desc"}],
                "limit": 10,
            }
        }
    )

    assert query.query.group_by == group_by


def test_population_catalog_declares_city_ranking_without_physical_endpoints() -> None:
    catalog = SemanticCatalog.default()
    subject = catalog.subject("population")

    assert subject is not None
    assert 4 in subject.scope_levels
    rules = {rule.value: rule.allowed_scope_levels for rule in subject.group_by_rules}
    assert rules["district"] == (4,)
    assert rules["descendant_street"] == (4,)
    assert rules["descendant_community"] == (4,)
    ranking_shapes = {
        shape.group_by_selection: shape.data_schema_ref
        for shape in subject.result_shapes
    }
    assert ranking_shapes[("district",)] == (
        "schema://data/population-ranking-table/1.0.0"
    )
    assert ranking_shapes[("descendant_street",)] == (
        "schema://data/population-ranking-table/1.0.0"
    )
    assert ranking_shapes[("descendant_community",)] == (
        "schema://data/population-ranking-table/1.0.0"
    )

    enterprise = catalog.subject("enterprise")
    assert enterprise is not None
    assert all(
        shape.data_schema_ref != "schema://data/population-ranking-table/1.0.0"
        for shape in enterprise.result_shapes
    )


def test_population_prompt_declares_controlled_city_ranking_groupings() -> None:
    prompt = build_full_view_system_prompt(
        {}, tool_ids=("governance.query_population_metrics",)
    )

    assert "group_by=['district']" in prompt
    assert "group_by=['descendant_street']" in prompt
    assert "group_by=['descendant_community']" in prompt


@pytest.mark.asyncio
async def test_city_district_population_ranking_uses_one_upstream_aggregate() -> None:
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
                    {"areaCode": "330106", "areaName": "西湖区", "total": "8"},
                    {"areaCode": "330102", "areaName": "上城区", "total": 12},
                ],
            },
        )

    arguments = models.QueryPopulationMetricsInput.model_validate(
        {
            "query": {
                "metrics": ["person_count"],
                "scope": {"area_code": "3301", "include_descendants": True},
                "group_by": ["district"],
                "order_by": [{"field": "person_count", "direction": "desc"}],
                "limit": 1,
            }
        }
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await _execute_population(
            HttpGovernanceAdapter(
                base_url="http://legacy.test/geo-qxst",
                credential_broker=RecordingCredentialBroker(),
                client=client,
            ),
            arguments,
        )

    assert len(requests) == 1
    assert requests[0].url.path == "/geo-qxst/area/getNextPersonByType"
    assert parse_qs(requests[0].content.decode()) == {
        "areaName": ["city_code"],
        "areaCode": ["3301"],
    }
    assert isinstance(result.data, models.PopulationRankingTable)
    assert [row.model_dump() for row in result.data.rows] == [
        {
            "rank": 1,
            "area_code": "330102",
            "area_name": "上城区",
            "person_count": 12,
        }
    ]
    assert result.truncated is True


@pytest.mark.asyncio
async def test_city_street_population_ranking_fans_out_by_district_once_each() -> None:
    calls: list[str] = []
    rows_by_area = {
        "3301": [
            {"areaCode": "330102", "areaName": "上城区", "total": 20},
            {"areaCode": "330106", "areaName": "西湖区", "total": 30},
        ],
        "330102": [
            {"areaCode": "330102001", "areaName": "湖滨街道", "total": 9}
        ],
        "330106": [
            {"areaCode": "330106002", "areaName": "文新街道", "total": 11},
            {"areaCode": "330106001", "areaName": "翠苑街道", "total": 15},
        ],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        area_code = parse_qs(request.content.decode())["areaCode"][0]
        calls.append(area_code)
        return httpx.Response(
            200,
            json={
                "state": True,
                "code": 200,
                "msg": "",
                "data": rows_by_area[area_code],
            },
        )

    arguments = models.QueryPopulationMetricsInput.model_validate(
        {
            "query": {
                "metrics": ["person_count"],
                "scope": {"area_code": "3301", "include_descendants": True},
                "group_by": ["descendant_street"],
                "order_by": [{"field": "person_count", "direction": "desc"}],
                "limit": 2,
            }
        }
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await _execute_population(
            HttpGovernanceAdapter(
                base_url="http://legacy.test/geo-qxst",
                credential_broker=RecordingCredentialBroker(),
                client=client,
            ),
            arguments,
        )

    assert calls[0] == "3301"
    assert set(calls[1:]) == {"330102", "330106"}
    assert isinstance(result.data, models.PopulationRankingTable)
    assert [(row.rank, row.area_name, row.person_count) for row in result.data.rows] == [
        (1, "翠苑街道", 15),
        (2, "文新街道", 11),
    ]
    assert result.truncated is True


@pytest.mark.asyncio
async def test_city_community_population_ranking_fails_closed_when_fanout_exceeds_bound() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        area_code = parse_qs(request.content.decode())["areaCode"][0]
        calls.append(area_code)
        if area_code == "3301":
            rows = [
                {
                    "areaCode": f"3301{index:02d}",
                    "areaName": f"区县{index}",
                    "total": index,
                }
                for index in range(21)
            ]
        else:
            raise AssertionError("bound must fail before descendant requests")
        return httpx.Response(
            200,
            json={"state": True, "code": 200, "msg": "", "data": rows},
        )

    arguments = models.QueryPopulationMetricsInput.model_validate(
        {
            "query": {
                "metrics": ["person_count"],
                "scope": {"area_code": "3301", "include_descendants": True},
                "group_by": ["descendant_community"],
                "order_by": [{"field": "person_count", "direction": "desc"}],
                "limit": 10,
            }
        }
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = HttpGovernanceAdapter(
            base_url="http://legacy.test/geo-qxst",
            credential_broker=RecordingCredentialBroker(),
            client=client,
        )
        auth_context = _city_auth_context()
        result = await CapabilityService(
            registry=ToolRegistry.default(),
            policy=MinimalPolicyAdapter(),
            adapter=adapter,
        ).execute(
            tool_call_id="tcl-population-city-ranking-bound",
            tool_id="governance.query_population_metrics",
            raw_arguments=arguments.model_dump(mode="json"),
            auth_context=auth_context,
        )

    assert calls == ["3301"]
    assert result.status == "failed"
    assert result.data_result is None
    assert result.warnings == ["upstream_contract_error"]
