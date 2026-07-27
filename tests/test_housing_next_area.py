"""C1: 出租房纵向切片 —— 受控 group_by=next_area 契约、适配器与模型可见性。

真实性依据：原系统出租房地图层固定调用 geo-qxst 通用下级区划聚合端点
（qxst-sj src/pages/map/modules/getFeature.js:304-305、urlByType.js:257-263），
与人口 Tool 已在 live-HTTP 验证的 /getNextSiteData 同族；按租赁类型汇总走
/house/getRoomLeaseType。两个端点均为服务端固定聚合，非客户端推算。
"""

import json
from urllib.parse import parse_qs

import httpx
import pytest

from full_view_agent.application import errors
from full_view_agent.application.policy import MinimalPolicyAdapter
from full_view_agent.application.prompt_catalog import (
    build_full_view_system_prompt,
)
from full_view_agent.application.tool_registry import ToolRegistry
from full_view_agent.domain import models
from full_view_agent.infrastructure import governance_adapter

from .test_http_governance_adapter import (
    RecordingCredentialBroker,
    _domain_auth_context,
)

PHYSICAL_IMPLEMENTATION_TOKENS = (
    "base_room_lease",
    "dm_empty_nest_old",
    "getNextSiteData",
    "getRoomLeaseType",
    "house_type",
    "geo-qxst",
    "http",
)


def _housing_auth_context():
    return _domain_auth_context(
        entitlement="governance.housing.aggregate.read",
        dataset_id="housing",
    )


def _execute_housing(adapter, arguments):
    manifest = ToolRegistry.default().get_manifest(
        "governance.query_housing_metrics"
    )
    auth_context = _housing_auth_context()
    policy = MinimalPolicyAdapter().evaluate(
        manifest=manifest,
        auth_context=auth_context,
        arguments=arguments,
    )
    return adapter.execute(
        manifest=manifest,
        arguments=arguments,
        policy_decision=policy,
        auth_context=auth_context,
    )


# ---------------------------------------------------------------------------
# 契约层：group_by 只接受受控的 next_area，其他分组维度一律拒绝
# ---------------------------------------------------------------------------


def test_housing_contract_accepts_controlled_next_area_grouping() -> None:
    query = models.QueryHousingMetricsInput.model_validate(
        {
            "query": {
                "scope": {"area_code": "330106"},
                "group_by": ["next_area"],
            }
        }
    )

    assert query.query.group_by == ["next_area"]
    default = models.QueryHousingMetricsInput.model_validate(
        {"query": {"scope": {"area_code": "330106"}}}
    )
    assert default.query.group_by == []


@pytest.mark.parametrize(
    "group_by",
    [
        ["street"],
        ["community"],
        ["district"],
        ["lease_type"],
        ["next_area", "next_area"],
    ],
)
def test_housing_contract_rejects_unregistered_group_dimensions(
    group_by: list[str],
) -> None:
    with pytest.raises(Exception) as excinfo:  # noqa: B017 - pydantic ValidationError
        models.QueryHousingMetricsInput.model_validate(
            {
                "query": {
                    "scope": {"area_code": "330106"},
                    "group_by": group_by,
                }
            }
        )
    assert "group_by" in str(excinfo.value)


# ---------------------------------------------------------------------------
# HTTP 适配器：next_area 编译到固定 legacy 端点，参数不来自模型
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_http_adapter_maps_housing_next_area_to_fixed_legacy_endpoint() -> None:
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
                    {
                        "areaCode": "330106001",
                        "areaName": "翠苑街道",
                        "total": 210,
                        "lon": 120.1,
                        "lat": 30.2,
                    },
                    {
                        "areaCode": "330106002",
                        "areaName": "文新街道",
                        "total": 180,
                        "lon": 120.1,
                        "lat": 30.3,
                    },
                ],
            },
        )

    arguments = models.QueryHousingMetricsInput.model_validate(
        {
            "query": {
                "scope": {"area_code": "330106", "include_descendants": True},
                "group_by": ["next_area"],
            }
        }
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = governance_adapter.HttpGovernanceAdapter(
            base_url="http://legacy.test/geo-qxst",
            credential_broker=RecordingCredentialBroker(),
            client=client,
        )
        result = await _execute_housing(adapter, arguments)

    assert requests[0].method == "POST"
    assert requests[0].url.path == "/geo-qxst/getNextSiteData"
    assert parse_qs(requests[0].content.decode()) == {
        "areaName": ["county_code"],
        "areaCode": ["330106"],
        "tableName": ["base_room_lease"],
    }
    assert result.kind == "table"
    assert result.data_schema_ref == "schema://data/housing-area-group-table/1.0.0"
    assert [(row.area_code, row.area_name, row.dwelling_count) for row in result.data.rows] == [
        ("330106001", "翠苑街道", 210),
        ("330106002", "文新街道", 180),
    ]
    assert result.truncated is False


@pytest.mark.asyncio
async def test_http_adapter_maps_housing_next_area_city_scope_to_city_column() -> None:
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
                    {
                        "areaCode": "330106",
                        "areaName": "西湖区",
                        "total": 210,
                        "lon": 120.1,
                        "lat": 30.2,
                    },
                    {
                        "areaCode": "330108",
                        "areaName": "滨江区",
                        "total": 320,
                        "lon": 120.2,
                        "lat": 30.2,
                    },
                ],
            },
        )

    arguments = models.QueryHousingMetricsInput.model_validate(
        {
            "query": {
                "scope": {"area_code": "3301", "include_descendants": True},
                "group_by": ["next_area"],
            }
        }
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = governance_adapter.HttpGovernanceAdapter(
            base_url="http://legacy.test/geo-qxst",
            credential_broker=RecordingCredentialBroker(),
            client=client,
        )
        result = await _execute_housing(adapter, arguments)

    assert requests[0].method == "POST"
    assert requests[0].url.path == "/geo-qxst/getNextSiteData"
    assert parse_qs(requests[0].content.decode()) == {
        "areaName": ["city_code"],
        "areaCode": ["3301"],
        "tableName": ["base_room_lease"],
    }
    assert result.kind == "table"
    assert result.data_schema_ref == "schema://data/housing-area-group-table/1.0.0"
    assert [(row.area_code, row.area_name, row.dwelling_count) for row in result.data.rows] == [
        ("330108", "滨江区", 320),
        ("330106", "西湖区", 210),
    ]
    assert result.truncated is False


@pytest.mark.asyncio
async def test_http_adapter_housing_next_area_street_scope_uses_town_column() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={"state": True, "code": 200, "msg": "", "data": []},
        )

    arguments = models.QueryHousingMetricsInput.model_validate(
        {
            "query": {
                "scope": {"area_code": "330106001"},
                "group_by": ["next_area"],
            }
        }
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = governance_adapter.HttpGovernanceAdapter(
            base_url="http://legacy.test/geo-qxst",
            credential_broker=RecordingCredentialBroker(),
            client=client,
        )
        result = await _execute_housing(adapter, arguments)

    assert parse_qs(requests[0].content.decode()) == {
        "areaName": ["town_code"],
        "areaCode": ["330106001"],
        "tableName": ["base_room_lease"],
    }
    assert result.data.rows == []
    assert result.row_count == 0


@pytest.mark.asyncio
async def test_http_adapter_housing_next_area_truncates_beyond_limit() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "state": True,
                "code": 200,
                "msg": "",
                "data": [
                    {"areaCode": f"33010600{i}", "areaName": f"街道{i}", "total": i}
                    for i in range(1, 4)
                ],
            },
        )

    arguments = models.QueryHousingMetricsInput.model_validate(
        {
            "query": {
                "scope": {"area_code": "330106"},
                "group_by": ["next_area"],
                "limit": 2,
            }
        }
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = governance_adapter.HttpGovernanceAdapter(
            base_url="http://legacy.test/geo-qxst",
            credential_broker=RecordingCredentialBroker(),
            client=client,
        )
        result = await _execute_housing(adapter, arguments)

    assert len(result.data.rows) == 2
    assert result.truncated is True


@pytest.mark.parametrize("area_code", ["330106001001001"])
@pytest.mark.asyncio
async def test_http_adapter_rejects_housing_next_area_at_unsupported_levels(
    area_code: str,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"state": True, "code": 200, "data": []})

    arguments = models.QueryHousingMetricsInput.model_validate(
        {
            "query": {
                "scope": {"area_code": area_code},
                "group_by": ["next_area"],
            }
        }
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = governance_adapter.HttpGovernanceAdapter(
            base_url="http://legacy.test/geo-qxst",
            credential_broker=RecordingCredentialBroker(),
            client=client,
        )
        with pytest.raises(errors.SemanticValidationError):
            await _execute_housing(adapter, arguments)

    assert requests == []


@pytest.mark.asyncio
async def test_http_adapter_housing_next_area_rejects_malformed_rows() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "state": True,
                "code": 200,
                "msg": "",
                "data": [{"house_type": "住宅出租", "total": 3}],
            },
        )

    arguments = models.QueryHousingMetricsInput.model_validate(
        {
            "query": {
                "scope": {"area_code": "330106"},
                "group_by": ["next_area"],
            }
        }
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = governance_adapter.HttpGovernanceAdapter(
            base_url="http://legacy.test/geo-qxst",
            credential_broker=RecordingCredentialBroker(),
            client=client,
        )
        with pytest.raises(errors.UpstreamContractError):
            await _execute_housing(adapter, arguments)


# ---------------------------------------------------------------------------
# 出租房 HTTP 链路的权限/上游异常反例（Gate C1：上游异常均有反例）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_http_adapter_housing_upstream_failure_is_unavailable_after_retries() -> None:
    attempts = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(500)

    arguments = models.QueryHousingMetricsInput.model_validate(
        {"query": {"scope": {"area_code": "330106"}}}
    )
    manifest = ToolRegistry.default().get_manifest(
        "governance.query_housing_metrics"
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = governance_adapter.HttpGovernanceAdapter(
            base_url="http://legacy.test/geo-qxst",
            credential_broker=RecordingCredentialBroker(),
            client=client,
        )
        with pytest.raises(errors.UpstreamUnavailable):
            await _execute_housing(adapter, arguments)

    assert attempts == manifest.limits.max_attempts


@pytest.mark.asyncio
async def test_http_adapter_housing_token_failure_requires_reauthentication() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "state": False,
                "code": 403,
                "msg": "token已过期或不存在",
                "data": [],
            },
        )

    arguments = models.QueryHousingMetricsInput.model_validate(
        {"query": {"scope": {"area_code": "330106"}}}
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = governance_adapter.HttpGovernanceAdapter(
            base_url="http://legacy.test/geo-qxst",
            credential_broker=RecordingCredentialBroker(),
            client=client,
        )
        with pytest.raises(errors.ReauthenticationRequired):
            await _execute_housing(adapter, arguments)


# ---------------------------------------------------------------------------
# InMemory 适配器：脚本 Eval 与本地开发的确定性行为
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_in_memory_adapter_serves_housing_next_area_distribution() -> None:
    adapter = governance_adapter.InMemoryGovernanceAdapter()
    arguments = models.QueryHousingMetricsInput.model_validate(
        {
            "query": {
                "scope": {"area_code": "330106001"},
                "group_by": ["next_area"],
            }
        }
    )

    result = await _execute_housing(adapter, arguments)

    assert result.data_schema_ref == "schema://data/housing-area-group-table/1.0.0"
    assert result.data.rows
    assert all(
        row.area_code.startswith("330106001") for row in result.data.rows
    )
    assert all(row.dwelling_count >= 0 for row in result.data.rows)


@pytest.mark.asyncio
async def test_in_memory_adapter_serves_housing_next_area_city_wide_districts() -> None:
    adapter = governance_adapter.InMemoryGovernanceAdapter()
    arguments = models.QueryHousingMetricsInput.model_validate(
        {
            "query": {
                "scope": {"area_code": "3301"},
                "group_by": ["next_area"],
            }
        }
    )

    result = await _execute_housing(adapter, arguments)

    assert result.data_schema_ref == "schema://data/housing-area-group-table/1.0.0"
    assert result.data.rows
    assert all(row.area_code.startswith("3301") for row in result.data.rows)
    assert all("区县" in row.area_name for row in result.data.rows)


@pytest.mark.asyncio
async def test_in_memory_adapter_keeps_lease_type_summary_without_group_by() -> None:
    adapter = governance_adapter.InMemoryGovernanceAdapter()
    arguments = models.QueryHousingMetricsInput.model_validate(
        {"query": {"scope": {"area_code": "330106"}}}
    )

    result = await _execute_housing(adapter, arguments)

    assert result.data_schema_ref == "schema://data/housing-lease-type-table/1.0.0"
    assert {row.lease_type for row in result.data.rows} == {
        "住宅出租",
        "商铺出租",
        "公寓出租",
        "群租房",
        "工业出租",
    }


# ---------------------------------------------------------------------------
# 清单/模型可见性：双结果 Schema、且不泄露物理实现
# ---------------------------------------------------------------------------


def test_housing_manifest_declares_both_result_schemas() -> None:
    manifest = ToolRegistry.default().get_manifest(
        "governance.query_housing_metrics"
    )

    refs = [binding.data_schema_ref for binding in manifest.result_schemas]
    assert refs[0] == "schema://data/housing-lease-type-table/1.0.0"
    assert "schema://data/housing-area-group-table/1.0.0" in refs
    assert all(binding.kind == "table" for binding in manifest.result_schemas)


def test_housing_model_surface_hides_physical_implementation() -> None:
    registry = ToolRegistry.default()
    descriptor = registry.get_model_descriptor("governance.query_housing_metrics")
    input_schema = registry.get_input_schema("governance.query_housing_metrics")
    surfaces = (
        descriptor.description,
        json.dumps(input_schema, ensure_ascii=False),
    )

    for surface in surfaces:
        for token in PHYSICAL_IMPLEMENTATION_TOKENS:
            assert token not in surface

    assert "next_area" in json.dumps(input_schema)
    assert "next_area" in descriptor.description


def test_housing_model_surface_advertises_city_wide_district_grouping() -> None:
    registry = ToolRegistry.default()
    descriptor = registry.get_model_descriptor("governance.query_housing_metrics")
    prompt = build_full_view_system_prompt(
        {}, tool_ids=("governance.query_housing_metrics",)
    )

    assert "全市按区县" in descriptor.description
    assert "不支持全市按区县" not in descriptor.description
    assert "全市按区县" in prompt
    assert "不支持全市按区县" not in prompt


def test_system_prompt_forbids_unsupported_causal_explanations() -> None:
    prompt = build_full_view_system_prompt(
        {}, tool_ids=("governance.query_housing_metrics",)
    )

    assert "不得自行推测原因或作因果归因" in prompt
