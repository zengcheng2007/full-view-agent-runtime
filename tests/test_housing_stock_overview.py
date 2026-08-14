"""房屋存量总览：旧系统真实契约、受控语义和中文展示。"""

import httpx
import pytest
from pydantic import ValidationError

from full_view_agent.application import errors
from full_view_agent.application.capability_service import CapabilityService
from full_view_agent.application.harness import ToolAction
from full_view_agent.application.policy import MinimalPolicyAdapter
from full_view_agent.application.tool_observation_service import (
    _choropleth_metric,
    _table_presentation,
)
from full_view_agent.application.tool_registry import ToolRegistry
from full_view_agent.domain import models
from full_view_agent.infrastructure import governance_adapter
from full_view_agent.semantic.authorization import SubjectAuthorization
from full_view_agent.semantic.catalog import SemanticCatalog
from full_view_agent.semantic.compiler import SemanticCompiler
from full_view_agent.semantic.errors import SemanticQueryRejected
from full_view_agent.semantic.query_spec import SemanticQuerySpec

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


def _stock_arguments(*, area_code: str = "330106"):
    return models.QueryHousingMetricsInput.model_validate(
        {
            "query": {
                "metrics": ["building_count", "room_count"],
                "scope": {"area_code": area_code},
                "group_by": [],
            }
        }
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


def _legacy_envelope(data: object) -> dict[str, object]:
    return {"state": True, "code": 200, "msg": "", "data": data}


def test_housing_contract_accepts_only_complete_stock_metric_pair() -> None:
    arguments = _stock_arguments()
    assert arguments.query.metrics == ["building_count", "room_count"]

    for metrics, group_by in [
        (["building_count"], []),
        (["room_count"], []),
        (["building_count", "room_count", "dwelling_count"], []),
        (["building_count", "room_count"], ["room_use"]),
    ]:
        with pytest.raises(ValidationError):
            models.QueryHousingMetricsInput.model_validate(
                {
                    "query": {
                        "metrics": metrics,
                        "scope": {"area_code": "330106"},
                        "group_by": group_by,
                    }
                }
            )


def test_enterprise_contract_remains_independent_of_housing_metric_shape() -> None:
    arguments = models.QueryEnterpriseMetricsInput.model_validate(
        {
            "query": {
                "scope": {"area_code": "330106"},
                "group_by": ["next_area"],
            }
        }
    )
    assert arguments.query.group_by == ["next_area"]


@pytest.mark.asyncio
async def test_http_adapter_maps_stock_to_verified_get_contract() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.headers["geoToken"] == "run-scoped-token"
        return httpx.Response(
            200,
            json=_legacy_envelope({"buildingTotal": 128, "roomTotal": "4096"}),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = governance_adapter.HttpGovernanceAdapter(
            base_url="http://legacy.test/geo-qxst",
            credential_broker=RecordingCredentialBroker(),
            client=client,
        )
        result = await _execute_housing(adapter, _stock_arguments())

    assert [(request.method, request.url.path) for request in requests] == [
        ("GET", "/geo-qxst/api/getBuildingAndRoomTotal")
    ]
    assert dict(requests[0].url.params) == {
        "areaCodeName": "county_code",
        "areaCodeValue": "330106",
    }
    assert requests[0].content == b""
    assert result.data_schema_ref == "schema://data/housing-stock-overview/1.0.0"
    assert result.row_count == 1
    assert result.data.rows[0].building_count == 128
    assert result.data.rows[0].room_count == 4096


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"buildingTotal": True, "roomTotal": 2},
        {"buildingTotal": -1, "roomTotal": 2},
        {"buildingTotal": 1.5, "roomTotal": 2},
        {"buildingTotal": 1},
        [{"buildingTotal": 1, "roomTotal": 2}],
    ],
)
async def test_http_adapter_stock_fails_closed_on_malformed_response(
    payload: object,
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_legacy_envelope(payload))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = governance_adapter.HttpGovernanceAdapter(
            base_url="http://legacy.test/geo-qxst",
            credential_broker=RecordingCredentialBroker(),
            client=client,
        )
        with pytest.raises(errors.UpstreamContractError):
            await _execute_housing(adapter, _stock_arguments())


@pytest.mark.asyncio
async def test_http_adapter_stock_rejects_bad_area_before_credentials_or_http() -> None:
    broker = RecordingCredentialBroker()
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=_legacy_envelope({}))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = governance_adapter.HttpGovernanceAdapter(
            base_url="http://legacy.test/geo-qxst",
            credential_broker=broker,
            client=client,
        )
        with pytest.raises(errors.SemanticValidationError):
            await _execute_housing(adapter, _stock_arguments(area_code="33010Ａ"))

    assert broker.resolved == []
    assert requests == []


@pytest.mark.asyncio
async def test_stock_is_denied_without_housing_entitlement_or_dataset() -> None:
    result = await CapabilityService(
        registry=ToolRegistry.default(),
        policy=MinimalPolicyAdapter(),
        adapter=governance_adapter.InMemoryGovernanceAdapter(),
    ).execute(
        tool_call_id="call-stock-denied",
        tool_id="governance.query_housing_metrics",
        raw_arguments={
            "query": {
                "metrics": ["building_count", "room_count"],
                "scope": {"area_code": "330106"},
                "group_by": [],
            }
        },
        auth_context=population_auth_context(),
    )

    assert result.status == "denied"
    assert result.warnings == ["TOOL_NOT_ENTITLED"]


def test_semantic_stock_shape_compiles_to_existing_housing_tool() -> None:
    catalog = SemanticCatalog.default(housing_next_area_enabled=True)
    compiler = SemanticCompiler(catalog)
    authorization = SubjectAuthorization(
        entitlements=("governance.housing.aggregate.read",),
        datasets=("housing",),
        field_policy_set="governance_analyst_v1",
        area_scopes=(
            models.AuthorizedAreaScope(
                area_code="3301",
                include_descendants=True,
            ),
        ),
    )
    spec = SemanticQuerySpec(
        subject="housing",
        metrics=["building_count", "room_count"],
        scope={"area_code": "330106"},
        group_by=[],
    )

    plan = compiler.compile(spec, authorization=authorization)

    assert plan.steps[0].capability_id == "governance.query_housing_metrics"
    assert plan.steps[0].arguments == {
        "query": {
            "schema_version": "1.1",
            "metrics": ["building_count", "room_count"],
            "scope": {"area_code": "330106", "include_descendants": True},
            "group_by": [],
            "limit": 200,
        }
    }
    assert plan.expected_result.data_schema_ref == (
        "schema://data/housing-stock-overview/1.0.0"
    )
    assert [item.metric_id for item in plan.evidence.metric_definitions] == [
        "building_count",
        "room_count",
    ]


def test_semantic_stock_rejects_partial_metric_pair_as_user_error() -> None:
    catalog = SemanticCatalog.default(housing_next_area_enabled=True)
    compiler = SemanticCompiler(catalog)
    authorization = SubjectAuthorization(
        entitlements=("governance.housing.aggregate.read",),
        datasets=("housing",),
        field_policy_set="governance_analyst_v1",
        area_scopes=(models.AuthorizedAreaScope(area_code="3301"),),
    )
    spec = SemanticQuerySpec(
        subject="housing",
        metrics=["building_count"],
        scope={"area_code": "330106"},
        group_by=[],
    )

    with pytest.raises(SemanticQueryRejected) as excinfo:
        compiler.compile(spec, authorization=authorization)

    assert "INVALID_RESULT_SHAPE" in str(excinfo.value)


def test_stock_presentation_is_two_chinese_metrics_table_and_csv_without_map() -> None:
    data = models.HousingStockOverviewTable(
        rows=[models.HousingStockOverviewRow(building_count=128, room_count=4096)]
    )
    result = models.TableDataResult(
        result_id="res-stock",
        data_schema_ref="schema://data/housing-stock-overview/1.0.0",
        result_fingerprint="sha256:test",
        data=data,
        row_count=1,
    )
    presentation = _table_presentation(
        result,
        action=ToolAction(
            tool_id="governance.query_housing_metrics",
            arguments={
                "query": {
                    "metrics": ["building_count", "room_count"],
                    "scope": {"area_code": "330106"},
                    "group_by": [],
                }
            },
        ),
    )

    assert presentation.title == "房屋存量总览"
    assert presentation.summary == "楼幢共 128 栋，户室共 4096 间。"
    assert [field.label for field in presentation.fields] == [
        "楼幢总数",
        "户室总数",
    ]
    assert [view.kind for view in presentation.visualizations] == [
        "table",
        "metric",
        "metric",
    ]
    assert not any(view.kind in {"bar", "choropleth"} for view in presentation.visualizations)
    assert presentation.download is not None
    assert presentation.download.formats == ["csv"]
    assert (
        _choropleth_metric(
            canonical_tool_id="governance.query_housing_metrics",
            data_result=result,
        )
        is None
    )


def test_housing_manifest_includes_stock_result_schema() -> None:
    manifest = ToolRegistry.default().get_manifest(
        "governance.query_housing_metrics"
    )
    assert "schema://data/housing-stock-overview/1.0.0" in {
        item.data_schema_ref for item in manifest.result_schemas
    }
