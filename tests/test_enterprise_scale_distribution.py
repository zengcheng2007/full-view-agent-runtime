"""企业规模分布：旧系统真实合同、严格边界与受控展示。"""

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


def _enterprise_scale_arguments(*, area_code: str = "330106"):
    return models.QueryEnterpriseMetricsInput.model_validate(
        {
            "query": {
                "scope": {"area_code": area_code},
                "group_by": ["enterprise_scale"],
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


def test_enterprise_metrics_accepts_enterprise_scale_as_single_group() -> None:
    arguments = models.QueryEnterpriseMetricsInput.model_validate(
        {
            "query": {
                "scope": {"area_code": "330106"},
                "group_by": ["enterprise_scale"],
            }
        }
    )

    assert arguments.query.group_by == ["enterprise_scale"]


def test_enterprise_metrics_rejects_combined_scale_and_existing_group() -> None:
    with pytest.raises(ValidationError):
        models.QueryEnterpriseMetricsInput.model_validate(
            {
                "query": {
                    "scope": {"area_code": "330106"},
                    "group_by": ["enterprise_scale", "enterprise_type"],
                }
            }
        )


@pytest.mark.asyncio
async def test_http_adapter_maps_enterprise_scale_to_verified_get_contract() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.headers["geoToken"] == "run-scoped-token"
        assert request.url.path == "/geo-qxst/api/getEnterpriseScale"
        return httpx.Response(
            200,
            json=_legacy_envelope(
                [
                    {
                        "5人以下": 11,
                        "5-10人": "7",
                        "10-50人": 5,
                        "50-100人": 3,
                        "100人以上": 2,
                    }
                ]
            ),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = governance_adapter.HttpGovernanceAdapter(
            base_url="http://legacy.test/geo-qxst",
            credential_broker=RecordingCredentialBroker(),
            client=client,
        )
        result = await _execute_enterprise(adapter, _enterprise_scale_arguments())

    assert [(request.method, request.url.path) for request in requests] == [
        ("GET", "/geo-qxst/api/getEnterpriseScale")
    ]
    assert dict(requests[0].url.params) == {
        "areaCodeName": "county_code",
        "areaCodeValue": "330106",
    }
    assert result.data_schema_ref == (
        "schema://data/enterprise-scale-distribution-table/1.0.0"
    )
    assert [
        (row.enterprise_scale, row.enterprise_count)
        for row in result.data.rows
    ] == [
        ("5人以下", 11),
        ("5-10人", 7),
        ("11-50人", 5),
        ("51-100人", 3),
        ("100人以上", 2),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        [],
        [{"5人以下": 1}],
        [
            {
                "5人以下": 1,
                "5-10人": 2,
                "10-50人": 3,
                "50-100人": 4,
                "100人以上": 5,
                "未知": 6,
            }
        ],
        [
            {
                "5人以下": True,
                "5-10人": 2,
                "10-50人": 3,
                "50-100人": 4,
                "100人以上": 5,
            }
        ],
        [
            {
                "5人以下": 1.5,
                "5-10人": 2,
                "10-50人": 3,
                "50-100人": 4,
                "100人以上": 5,
            }
        ],
        [
            {
                "5人以下": -1,
                "5-10人": 2,
                "10-50人": 3,
                "50-100人": 4,
                "100人以上": 5,
            }
        ],
        [
            {
                "5人以下": 1,
                "5-10人": 2,
                "10-50人": 3,
                "50-100人": 4,
                "100人以上": 5,
            },
            {
                "5人以下": 1,
                "5-10人": 2,
                "10-50人": 3,
                "50-100人": 4,
                "100人以上": 5,
            },
        ],
    ],
)
async def test_http_adapter_enterprise_scale_fails_closed_on_contract_drift(
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
            await _execute_enterprise(adapter, _enterprise_scale_arguments())


@pytest.mark.asyncio
async def test_enterprise_scale_rejects_invalid_area_before_credentials() -> None:
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
                _enterprise_scale_arguments(area_code="33010Ａ"),
            )

    assert broker.resolved == []
    assert requests == []


@pytest.mark.asyncio
async def test_enterprise_scale_is_denied_without_dataset_and_entitlement() -> None:
    result = await CapabilityService(
        registry=ToolRegistry.default(),
        policy=MinimalPolicyAdapter(),
        adapter=governance_adapter.InMemoryGovernanceAdapter(),
    ).execute(
        tool_call_id="call-enterprise-scale-denied",
        tool_id="governance.query_enterprise_metrics",
        raw_arguments={
            "query": {
                "scope": {"area_code": "330106"},
                "group_by": ["enterprise_scale"],
            }
        },
        auth_context=population_auth_context(),
    )

    assert result.status == "denied"
    assert result.warnings == ["TOOL_NOT_ENTITLED"]


def test_semantic_enterprise_scale_compiles_to_existing_tool_and_shape() -> None:
    compiler = SemanticCompiler(SemanticCatalog.default())
    spec = SemanticQuerySpec(
        subject="enterprise",
        metrics=["enterprise_count"],
        scope={"area_code": "330106"},
        group_by=["enterprise_scale"],
    )

    plan = compiler.compile(spec, authorization=_enterprise_authorization())

    assert plan.steps[0].capability_id == "governance.query_enterprise_metrics"
    assert plan.steps[0].arguments == {
        "query": {
            "schema_version": "1.1",
            "scope": {"area_code": "330106", "include_descendants": True},
            "group_by": ["enterprise_scale"],
            "limit": 200,
        }
    }
    assert plan.expected_result.data_schema_ref == (
        "schema://data/enterprise-scale-distribution-table/1.0.0"
    )
    assert plan.expected_result.row_fields == (
        "enterprise_scale",
        "enterprise_count",
    )


def test_semantic_enterprise_scale_rejects_choropleth_output() -> None:
    compiler = SemanticCompiler(SemanticCatalog.default())
    spec = SemanticQuerySpec(
        subject="enterprise",
        metrics=["enterprise_count"],
        scope={"area_code": "330106"},
        group_by=["enterprise_scale"],
        output="choropleth",
    )

    with pytest.raises(SemanticQueryRejected) as excinfo:
        compiler.compile(spec, authorization=_enterprise_authorization())

    assert "INVALID_OUTPUT" in str(excinfo.value)


def test_enterprise_scale_presentation_is_chinese_and_states_scope_boundary(
) -> None:
    data = models.EnterpriseScaleDistributionTable(
        rows=[
            models.EnterpriseScaleDistributionRow(
                enterprise_scale="5人以下",
                enterprise_count=11,
            ),
            models.EnterpriseScaleDistributionRow(
                enterprise_scale="5-10人",
                enterprise_count=7,
            ),
            models.EnterpriseScaleDistributionRow(
                enterprise_scale="11-50人",
                enterprise_count=5,
            ),
            models.EnterpriseScaleDistributionRow(
                enterprise_scale="51-100人",
                enterprise_count=3,
            ),
            models.EnterpriseScaleDistributionRow(
                enterprise_scale="100人以上",
                enterprise_count=2,
            ),
        ]
    )
    result = models.TableDataResult(
        result_id="res-enterprise-scale",
        data_schema_ref=(
            "schema://data/enterprise-scale-distribution-table/1.0.0"
        ),
        result_fingerprint="sha256:test",
        data=data,
        row_count=5,
    )

    presentation = _table_presentation(
        result,
        action=ToolAction(
            tool_id="governance.query_enterprise_metrics",
            arguments={
                "query": {
                    "scope": {"area_code": "330106"},
                    "group_by": ["enterprise_scale"],
                }
            },
        ),
    )

    assert presentation.title == "企业规模分布"
    assert presentation.summary == (
        "按从业人数统计 5 档规模，已纳入规模统计的企业合计 28 家；"
        "从业人数为空的企业不在上述合计内。"
    )
    assert [field.label for field in presentation.fields] == [
        "企业规模",
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


def test_enterprise_scale_contract_is_registered_on_existing_tool() -> None:
    registry = ToolRegistry.default()
    manifest = registry.get_manifest("governance.query_enterprise_metrics")
    descriptor = registry.get_model_descriptor(
        "governance.query_enterprise_metrics"
    )

    assert (
        "schema://data/enterprise-scale-distribution-table/1.0.0"
        in {schema.data_schema_ref for schema in manifest.result_schemas}
    )
    assert (
        SCHEMA_MODELS[
            "data/tool-specific/enterprise-scale-distribution-table.schema.json"
        ]
        is models.EnterpriseScaleDistributionTable
    )
    assert descriptor.name == "查询企业区划分布"
    assert "group_by=[enterprise_scale]" in descriptor.description
    assert "从业人数" in descriptor.description
