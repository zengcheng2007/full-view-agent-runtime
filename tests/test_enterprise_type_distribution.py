"""企业类型分布：旧系统真实合同、字典映射与受控展示。"""

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
from full_view_agent.contract_registry import SCHEMA_MODELS
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


def _enterprise_type_arguments(*, area_code: str = "330106"):
    return models.QueryEnterpriseMetricsInput.model_validate(
        {
            "query": {
                "scope": {"area_code": area_code},
                "group_by": ["enterprise_type"],
            }
        }
    )


def _enterprise_auth_context():
    return _domain_auth_context(
        entitlement="governance.enterprise.aggregate.read",
        dataset_id="enterprise",
    )


async def _execute_enterprise(adapter, arguments):
    manifest = ToolRegistry.default().get_manifest(
        "governance.query_enterprise_metrics"
    )
    auth_context = _enterprise_auth_context()
    policy = MinimalPolicyAdapter().evaluate(
        manifest=manifest,
        auth_context=auth_context,
        arguments=arguments,
    )
    return await adapter.execute(
        manifest=manifest,
        arguments=arguments,
        policy_decision=policy,
        auth_context=auth_context,
    )


def _legacy_envelope(data: object) -> dict[str, object]:
    return {"state": True, "code": 200, "msg": "", "data": data}


def test_enterprise_contract_accepts_one_enterprise_type_grouping() -> None:
    arguments = _enterprise_type_arguments()

    assert arguments.query.group_by == ["enterprise_type"]

    with pytest.raises(ValidationError):
        models.QueryEnterpriseMetricsInput.model_validate(
            {
                "query": {
                    "scope": {"area_code": "330106"},
                    "group_by": ["enterprise_type", "next_area"],
                }
            }
        )


def test_enterprise_contract_keeps_existing_next_area_shape() -> None:
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
async def test_http_adapter_maps_enterprise_type_to_verified_get_and_dictionary(
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.headers["geoToken"] == "run-scoped-token"
        if request.url.path == "/geo-qxst/api/getEnterpriseTypeCount":
            return httpx.Response(
                200,
                json=_legacy_envelope(
                    [
                        {"enterprise_type": "10", "count": 18},
                        {"enterprise_type": "20", "count": "7"},
                    ]
                ),
            )
        if request.url.path == "/geo-qxst/dict/getDictValue":
            return httpx.Response(
                200,
                json=_legacy_envelope(
                    [
                        {
                            "enterprise_type": [
                                {"dicValue": "10", "dicName": "有限责任公司"},
                                {"dicValue": "20", "dicName": "股份有限公司"},
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
        result = await _execute_enterprise(adapter, _enterprise_type_arguments())

    assert [(request.method, request.url.path) for request in requests] == [
        ("GET", "/geo-qxst/api/getEnterpriseTypeCount"),
        ("POST", "/geo-qxst/dict/getDictValue"),
    ]
    assert dict(requests[0].url.params) == {
        "areaCodeName": "county_code",
        "areaCodeValue": "330106",
        "typeColumn": "enterprise_type",
    }
    assert requests[1].content == b""
    assert result.data_schema_ref == (
        "schema://data/enterprise-type-distribution-table/1.0.0"
    )
    assert [
        (row.enterprise_type, row.enterprise_count)
        for row in result.data.rows
    ] == [
        ("有限责任公司", 18),
        ("股份有限公司", 7),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("enterprise_rows", "dictionary_rows"),
    [
        (
            [{"enterprise_type": "10", "count": 18}],
            [
                {
                    "enterprise_type": [
                        {"dicValue": "10", "dicName": "企业"},
                        {"dicValue": "20", "dicName": "企业"},
                    ]
                }
            ],
        ),
    ],
)
async def test_http_adapter_enterprise_type_fails_closed_on_label_ambiguity(
    enterprise_rows: list[dict[str, object]],
    dictionary_rows: list[dict[str, object]],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/api/getEnterpriseTypeCount"):
            return httpx.Response(200, json=_legacy_envelope(enterprise_rows))
        return httpx.Response(200, json=_legacy_envelope(dictionary_rows))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = governance_adapter.HttpGovernanceAdapter(
            base_url="http://legacy.test/geo-qxst",
            credential_broker=RecordingCredentialBroker(),
            client=client,
        )
        with pytest.raises(errors.UpstreamContractError):
            await _execute_enterprise(adapter, _enterprise_type_arguments())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("enterprise_rows", "dictionary_rows"),
    [
        (
            [{"enterprise_type": "10", "count": True}],
            [{"enterprise_type": [{"dicValue": "10", "dicName": "企业A"}]}],
        ),
        (
            [{"enterprise_type": "10", "count": 1.5}],
            [{"enterprise_type": [{"dicValue": "10", "dicName": "企业A"}]}],
        ),
        (
            [{"enterprise_type": "", "count": 1}],
            [{"enterprise_type": [{"dicValue": "10", "dicName": "企业A"}]}],
        ),
        (
            [{"enterprise_type": "10", "count": 1}],
            [{"enterprise_type": [{"dicValue": "20", "dicName": "企业B"}]}],
        ),
        (
            [
                {"enterprise_type": "10", "count": 2},
                {"enterprise_type": "10", "count": 1},
            ],
            [{"enterprise_type": [{"dicValue": "10", "dicName": "企业A"}]}],
        ),
        (
            [{"enterprise_type": str(index), "count": 1} for index in range(9)],
            [],
        ),
        (
            [{"enterprise_type": "10", "count": 1}],
            [
                {"enterprise_type": [{"dicValue": "10", "dicName": "企业A"}]},
                {"enterprise_type": [{"dicValue": "10", "dicName": "企业A"}]},
            ],
        ),
    ],
)
async def test_http_adapter_enterprise_type_fails_closed_on_malformed_source(
    enterprise_rows: list[dict[str, object]],
    dictionary_rows: list[dict[str, object]],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/api/getEnterpriseTypeCount"):
            return httpx.Response(200, json=_legacy_envelope(enterprise_rows))
        return httpx.Response(200, json=_legacy_envelope(dictionary_rows))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = governance_adapter.HttpGovernanceAdapter(
            base_url="http://legacy.test/geo-qxst",
            credential_broker=RecordingCredentialBroker(),
            client=client,
        )
        with pytest.raises(errors.UpstreamContractError):
            await _execute_enterprise(adapter, _enterprise_type_arguments())


@pytest.mark.asyncio
async def test_http_adapter_enterprise_type_empty_result_needs_no_dictionary() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=_legacy_envelope([]))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = governance_adapter.HttpGovernanceAdapter(
            base_url="http://legacy.test/geo-qxst",
            credential_broker=RecordingCredentialBroker(),
            client=client,
        )
        result = await _execute_enterprise(adapter, _enterprise_type_arguments())

    assert len(requests) == 1
    assert result.data.rows == []
    assert result.row_count == 0


@pytest.mark.asyncio
async def test_enterprise_type_rejects_invalid_area_before_credentials() -> None:
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
            await _execute_enterprise(
                adapter,
                _enterprise_type_arguments(area_code="33010Ａ"),
            )

    assert broker.resolved == []
    assert requests == []


@pytest.mark.asyncio
async def test_enterprise_type_is_denied_without_dataset_and_entitlement() -> None:
    result = await CapabilityService(
        registry=ToolRegistry.default(),
        policy=MinimalPolicyAdapter(),
        adapter=governance_adapter.InMemoryGovernanceAdapter(),
    ).execute(
        tool_call_id="call-enterprise-type-denied",
        tool_id="governance.query_enterprise_metrics",
        raw_arguments={
            "query": {
                "scope": {"area_code": "330106"},
                "group_by": ["enterprise_type"],
            }
        },
        auth_context=population_auth_context(),
    )

    assert result.status == "denied"
    assert result.warnings == ["TOOL_NOT_ENTITLED"]


def _enterprise_authorization() -> SubjectAuthorization:
    return SubjectAuthorization(
        entitlements=("governance.enterprise.aggregate.read",),
        datasets=("enterprise",),
        field_policy_set="governance_analyst_v1",
        area_scopes=(
            models.AuthorizedAreaScope(
                area_code="3301",
                include_descendants=True,
            ),
        ),
    )


def test_semantic_enterprise_type_compiles_to_existing_enterprise_tool() -> None:
    catalog = SemanticCatalog.default()
    compiler = SemanticCompiler(catalog)
    spec = SemanticQuerySpec(
        subject="enterprise",
        metrics=["enterprise_count"],
        scope={"area_code": "330106"},
        group_by=["enterprise_type"],
    )

    plan = compiler.compile(spec, authorization=_enterprise_authorization())

    assert plan.steps[0].capability_id == "governance.query_enterprise_metrics"
    assert plan.steps[0].arguments == {
        "query": {
            "schema_version": "1.1",
            "scope": {"area_code": "330106", "include_descendants": True},
            "group_by": ["enterprise_type"],
            "limit": 200,
        }
    }
    assert plan.expected_result.data_schema_ref == (
        "schema://data/enterprise-type-distribution-table/1.0.0"
    )
    assert plan.expected_result.row_fields == (
        "enterprise_type",
        "enterprise_count",
    )


def test_semantic_enterprise_next_area_shape_is_unchanged() -> None:
    compiler = SemanticCompiler(SemanticCatalog.default())
    spec = SemanticQuerySpec(
        subject="enterprise",
        metrics=["enterprise_count"],
        scope={"area_code": "330106"},
        group_by=["next_area"],
    )

    plan = compiler.compile(spec, authorization=_enterprise_authorization())

    assert plan.expected_result.data_schema_ref == (
        "schema://data/enterprise-metric-table/1.0.0"
    )
    assert plan.expected_result.row_fields == (
        "area_code",
        "area_name",
        "enterprise_count",
    )


def test_semantic_enterprise_type_rejects_choropleth_output() -> None:
    compiler = SemanticCompiler(SemanticCatalog.default())
    spec = SemanticQuerySpec(
        subject="enterprise",
        metrics=["enterprise_count"],
        scope={"area_code": "330106"},
        group_by=["enterprise_type"],
        output="choropleth",
    )

    with pytest.raises(SemanticQueryRejected) as excinfo:
        compiler.compile(spec, authorization=_enterprise_authorization())

    assert "INVALID_OUTPUT" in str(excinfo.value)


def test_enterprise_type_presentation_is_chinese_table_metric_bar_csv_without_map(
) -> None:
    data = models.EnterpriseTypeDistributionTable(
        rows=[
            models.EnterpriseTypeDistributionRow(
                enterprise_type="有限责任公司",
                enterprise_count=18,
            ),
            models.EnterpriseTypeDistributionRow(
                enterprise_type="股份有限公司",
                enterprise_count=7,
            ),
        ]
    )
    result = models.TableDataResult(
        result_id="res-enterprise-type",
        data_schema_ref="schema://data/enterprise-type-distribution-table/1.0.0",
        result_fingerprint="sha256:test",
        data=data,
        row_count=2,
    )
    presentation = _table_presentation(
        result,
        action=ToolAction(
            tool_id="governance.query_enterprise_metrics",
            arguments={
                "query": {
                    "scope": {"area_code": "330106"},
                    "group_by": ["enterprise_type"],
                }
            },
        ),
    )

    assert presentation.title == "企业类型分布"
    assert presentation.summary == (
        "旧接口最多返回 8 类；当前返回 2 类，已返回企业数量合计 25 家。"
    )
    assert [field.label for field in presentation.fields] == [
        "企业类型",
        "企业数量",
    ]
    assert [view.kind for view in presentation.visualizations] == [
        "table",
        "metric",
        "bar",
    ]
    assert presentation.download is not None
    assert presentation.download.formats == ["csv"]
    assert (
        _choropleth_metric(
            canonical_tool_id="governance.query_enterprise_metrics",
            data_result=result,
        )
        is None
    )


def test_enterprise_type_contract_is_registered_on_existing_tool() -> None:
    registry = ToolRegistry.default()
    manifest = registry.get_manifest("governance.query_enterprise_metrics")
    descriptor = registry.get_model_descriptor(
        "governance.query_enterprise_metrics"
    )

    assert (
        "schema://data/enterprise-type-distribution-table/1.0.0"
        in {schema.data_schema_ref for schema in manifest.result_schemas}
    )
    assert (
        SCHEMA_MODELS[
            "data/tool-specific/enterprise-type-distribution-table.schema.json"
        ]
        is models.EnterpriseTypeDistributionTable
    )
    assert descriptor.name == "查询企业区划分布"
    assert "group_by=[enterprise_type]" in descriptor.description
    assert "最多 8 类" in descriptor.description
