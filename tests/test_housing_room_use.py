"""房屋用途最小纵向切片：受控语义、真实 HTTP 契约与中文结果展示。"""

import httpx
import pytest

from full_view_agent.application import errors
from full_view_agent.application.capability_service import CapabilityService
from full_view_agent.application.harness import ToolAction
from full_view_agent.application.policy import MinimalPolicyAdapter
from full_view_agent.application.tool_observation_service import (
    _dataset_label,
    _table_presentation,
)
from full_view_agent.application.tool_registry import ToolRegistry
from full_view_agent.domain import models
from full_view_agent.infrastructure import governance_adapter
from full_view_agent.semantic.catalog import SemanticCatalog

from .test_http_governance_adapter import (
    RecordingCredentialBroker,
    _domain_auth_context,
)
from .test_policy import population_auth_context


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


def _room_use_arguments(*, area_code: str = "330106", limit: int = 200):
    return models.QueryHousingMetricsInput.model_validate(
        {
            "query": {
                "scope": {"area_code": area_code},
                "group_by": ["room_use"],
                "limit": limit,
            }
        }
    )


def _legacy_envelope(data: object) -> dict[str, object]:
    return {"state": True, "code": 200, "msg": "", "data": data}


def test_housing_contract_accepts_only_one_room_use_grouping() -> None:
    arguments = _room_use_arguments()

    assert arguments.query.group_by == ["room_use"]
    with pytest.raises(Exception) as excinfo:  # noqa: B017 - Pydantic error
        models.QueryHousingMetricsInput.model_validate(
            {
                "query": {
                    "scope": {"area_code": "330106"},
                    "group_by": ["room_use", "next_area"],
                }
            }
        )
    assert "group_by" in str(excinfo.value)


@pytest.mark.asyncio
async def test_http_adapter_maps_room_use_to_verified_query_contract_and_dictionary() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.headers["geoToken"] == "run-scoped-token"
        if request.url.path == "/geo-qxst/room/getRoomUseAndAlone":
            return httpx.Response(
                200,
                json=_legacy_envelope(
                    [
                        {"key": "10", "doc_count": 18},
                        {"key": "20", "doc_count": "7"},
                    ]
                ),
            )
        if request.url.path == "/geo-qxst/dict/getDictValue":
            return httpx.Response(
                200,
                json=_legacy_envelope(
                    [
                        {
                            "room_user": [
                                {"dicValue": "10", "dicName": "自住"},
                                {"dicValue": "20", "dicName": "出租"},
                            ]
                        }
                    ]
                ),
            )
        raise AssertionError(f"unexpected path: {request.url.path}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = governance_adapter.HttpGovernanceAdapter(
            base_url="http://legacy.test/geo-qxst",
            credential_broker=RecordingCredentialBroker(),
            client=client,
        )
        result = await _execute_housing(adapter, _room_use_arguments())

    assert [(request.method, request.url.path) for request in requests] == [
        ("POST", "/geo-qxst/room/getRoomUseAndAlone"),
        ("POST", "/geo-qxst/dict/getDictValue"),
    ]
    assert dict(requests[0].url.params) == {
        "areaName": "county_code",
        "areaCode": "330106",
    }
    assert requests[0].content == b""
    assert requests[1].content == b""
    assert result.data_schema_ref == "schema://data/housing-room-use-table/1.0.0"
    assert [(row.room_use, row.dwelling_count) for row in result.data.rows] == [
        ("自住", 18),
        ("出租", 7),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "room_rows,dict_rows",
    [
        ([{"key": "10", "doc_count": True}], [{"room_user": []}]),
        ([{"key": "10", "doc_count": 1.5}], [{"room_user": []}]),
        ([{"key": "10", "doc_count": 1}], [{"room_user": []}]),
        ([{"key": "", "doc_count": 1}], [{"room_user": []}]),
    ],
)
async def test_http_adapter_room_use_fails_closed_on_bad_count_or_dictionary(
    room_rows: list[dict[str, object]],
    dict_rows: list[dict[str, object]],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/room/getRoomUseAndAlone"):
            return httpx.Response(200, json=_legacy_envelope(room_rows))
        return httpx.Response(200, json=_legacy_envelope(dict_rows))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = governance_adapter.HttpGovernanceAdapter(
            base_url="http://legacy.test/geo-qxst",
            credential_broker=RecordingCredentialBroker(),
            client=client,
        )
        with pytest.raises(errors.UpstreamContractError):
            await _execute_housing(adapter, _room_use_arguments())


@pytest.mark.asyncio
async def test_http_adapter_room_use_rejects_invalid_area_before_credentials() -> None:
    broker = RecordingCredentialBroker()
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=_legacy_envelope([]))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = governance_adapter.HttpGovernanceAdapter(
            base_url="http://legacy.test/geo-qxst",
            credential_broker=broker,
            client=client,
        )
        with pytest.raises(errors.SemanticValidationError):
            await _execute_housing(adapter, _room_use_arguments(area_code="33010Ａ"))

    assert broker.resolved == []
    assert requests == []


@pytest.mark.asyncio
async def test_room_use_is_denied_without_housing_entitlement_or_dataset() -> None:
    result = await CapabilityService(
        registry=ToolRegistry.default(),
        policy=MinimalPolicyAdapter(),
        adapter=governance_adapter.InMemoryGovernanceAdapter(),
    ).execute(
        tool_call_id="call-room-use-denied",
        tool_id="governance.query_housing_metrics",
        raw_arguments={
            "query": {
                "scope": {"area_code": "330106"},
                "group_by": ["room_use"],
            }
        },
        auth_context=population_auth_context(),
    )

    assert result.status == "denied"
    assert result.warnings == ["TOOL_NOT_ENTITLED"]


def test_semantic_catalog_exposes_room_use_without_changing_default_shape() -> None:
    subject = SemanticCatalog.default(housing_next_area_enabled=True).subject(
        "housing"
    )

    assert subject is not None
    assert subject.metrics[0].label == "房屋数量"
    assert any(rule.value == "room_use" for rule in subject.group_by_rules)
    room_use_shape = next(
        shape
        for shape in subject.result_shapes
        if shape.group_by_selection == ("room_use",)
    )
    assert room_use_shape.row_fields == ("room_use", "dwelling_count")
    default_shape = next(
        shape for shape in subject.result_shapes if shape.group_by_selection == ()
    )
    assert default_shape.shape_id == "housing_lease_type_table"


def test_room_use_presentation_is_chinese_table_metric_bar_and_csv() -> None:
    data = models.HousingRoomUseTable(
        rows=[
            models.HousingRoomUseRow(room_use="自住", dwelling_count=18),
            models.HousingRoomUseRow(room_use="出租", dwelling_count=7),
        ]
    )
    result = models.TableDataResult(
        result_id="res-room-use",
        data_schema_ref="schema://data/housing-room-use-table/1.0.0",
        result_fingerprint="sha256:test",
        data=data,
        row_count=2,
    )

    presentation = _table_presentation(
        result,
        action=ToolAction(
            tool_id="governance.query_housing_metrics",
            arguments={
                "query": {
                    "scope": {"area_code": "330106"},
                    "group_by": ["room_use"],
                }
            },
        ),
    )

    assert presentation.title == "户室用途统计"
    assert presentation.summary == "共 2 种户室用途，户室合计 25 套。"
    assert [field.label for field in presentation.fields] == ["户室用途", "户室数量"]
    assert {view.kind for view in presentation.visualizations} == {
        "table",
        "metric",
        "bar",
    }
    assert presentation.download is not None
    assert presentation.download.formats == ["csv"]


def test_housing_manifest_and_schema_registry_include_room_use_result() -> None:
    registry = ToolRegistry.default()
    manifest = registry.get_manifest("governance.query_housing_metrics")

    assert "schema://data/housing-room-use-table/1.0.0" in {
        item.data_schema_ref for item in manifest.result_schemas
    }
    assert registry.get_model_descriptor(
        "governance.query_housing_metrics"
    ).name == "查询房屋聚合指标"
    assert _dataset_label("housing") == "房屋聚合数据"
