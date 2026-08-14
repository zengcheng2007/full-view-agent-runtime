from urllib.parse import parse_qs

import httpx
import pytest
from pydantic import SecretStr

from full_view_agent.application import errors
from full_view_agent.application.answer_claims import AnswerClaim, StructuredFinish
from full_view_agent.application.capability_service import CapabilityService
from full_view_agent.application.harness import (
    DeterministicCompletionValidator,
    FinishAction,
    HarnessState,
)
from full_view_agent.application.policy import MinimalPolicyAdapter
from full_view_agent.application.tool_registry import ToolRegistry
from full_view_agent.domain import models
from full_view_agent.domain.models import (
    QueryPopulationMetricsInput,
    ResolveAreaInput,
    ToolResult,
)
from full_view_agent.infrastructure import governance_adapter

from .test_policy import population_auth_context


class RecordingCredentialBroker:
    def __init__(self) -> None:
        self.resolved: list[dict[str, str]] = []

    async def resolve(self, **kwargs) -> SecretStr:
        self.resolved.append(kwargs)
        return SecretStr("run-scoped-token")


def full_auth_context():
    context = population_auth_context()
    return context.model_copy(
        update={
            "entitlements": [
                "governance.area.read",
                "governance.population.aggregate.read",
            ],
            "data_scopes": context.data_scopes.model_copy(
                update={"datasets": ["administrative_area", "population"]}
            ),
        }
    )


def overview_auth_context():
    context = full_auth_context()
    return context.model_copy(
        update={
            "entitlements": [
                *context.entitlements,
                "governance.overview.aggregate.read",
            ],
            "data_scopes": context.data_scopes.model_copy(
                update={
                    "datasets": [
                        *context.data_scopes.datasets,
                        "governance_overview",
                    ]
                }
            ),
        }
    )


def governance_power_auth_context():
    context = full_auth_context()
    return context.model_copy(
        update={
            "entitlements": [
                *context.entitlements,
                "governance.power.aggregate.read",
            ],
            "data_scopes": context.data_scopes.model_copy(
                update={
                    "datasets": [
                        *context.data_scopes.datasets,
                        "governance_power",
                    ]
                }
            ),
        }
    )


@pytest.mark.asyncio
async def test_http_adapter_queries_governance_power_aggregate_without_person_details() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "state": True,
                "code": 200,
                "data": [
                    {"type": "roomNum", "count": "120"},
                    {"type": "10", "count": 5},
                    {"type": "11", "count": 4},
                    {"type": "12", "count": 9},
                    {"type": "13", "count": 8},
                    {"type": "nGridSum", "count": 7},
                    {"type": "gridUnitSum", "count": 30},
                    # The legacy service always appends this superseded aggregate.
                    # The product UI ignores it in favour of nGridSum.
                    {"type": "999", "count": 6},
                ],
            },
        )

    arguments = models.QueryGovernancePowerMetricsInput.model_validate(
        {"query": {"scope": {"area_code": "330106"}}}
    )
    auth_context = governance_power_auth_context()
    manifest = ToolRegistry.default().get_manifest(
        "governance.query_governance_power_metrics"
    )
    policy = MinimalPolicyAdapter().evaluate(
        manifest=manifest,
        auth_context=auth_context,
        arguments=arguments,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await governance_adapter.HttpGovernanceAdapter(
            base_url="http://legacy.test/geo-qxst",
            credential_broker=RecordingCredentialBroker(),
            client=client,
        ).execute(
            manifest=manifest,
            arguments=arguments,
            policy_decision=policy,
            auth_context=auth_context,
        )

    assert requests[0].url.path == "/geo-qxst/api/getGovernancePower"
    assert dict(requests[0].url.params) == {
        "areaCodeName": "county_code",
        "areaCodeValue": "330106",
    }
    assert requests[0].headers["geoToken"] == "run-scoped-token"
    assert [(row.type_code, row.type_name, row.count) for row in result.data.rows] == [
        ("roomNum", "户数", 120),
        ("10", "网格长", 5),
        ("11", "网格指导员", 4),
        ("12", "专职网格员", 9),
        ("13", "兼职网格员", 8),
        ("nGridSum", "其他网格力量", 7),
        ("gridUnitSum", "微网格", 30),
    ]
    assert "getGovernancePowerByGridCode" not in str(requests[0].url)


@pytest.mark.parametrize(
    "data",
    [
        [{"type": "unknown", "count": 1}],
        [{"type": "10", "count": 1}, {"type": "10", "count": 2}],
        [{"type": "999", "count": 1}, {"type": "999", "count": 2}],
        [{"type": "10", "count": True}],
        [{"type": "999", "count": True}],
        [{"type": "10", "count": -1}],
    ],
)
@pytest.mark.asyncio
async def test_http_adapter_rejects_unsafe_governance_power_aggregate(
    data: list[dict[str, object]],
) -> None:
    arguments = models.QueryGovernancePowerMetricsInput.model_validate(
        {"query": {"scope": {"area_code": "330106"}}}
    )
    auth_context = governance_power_auth_context()
    manifest = ToolRegistry.default().get_manifest(
        "governance.query_governance_power_metrics"
    )
    policy = MinimalPolicyAdapter().evaluate(
        manifest=manifest,
        auth_context=auth_context,
        arguments=arguments,
    )
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200, json={"state": True, "code": 200, "data": data}
        )
    )
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(errors.UpstreamContractError):
            await governance_adapter.HttpGovernanceAdapter(
                base_url="http://legacy.test/geo-qxst",
                credential_broker=RecordingCredentialBroker(),
                client=client,
            ).execute(
                manifest=manifest,
                arguments=arguments,
                policy_decision=policy,
                auth_context=auth_context,
            )


def test_http_governance_adapter_is_available() -> None:
    assert getattr(governance_adapter, "HttpGovernanceAdapter", None) is not None


@pytest.mark.asyncio
async def test_http_adapter_queries_governance_overview_with_run_token() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "state": True,
                "code": 200,
                "msg": "",
                "data": {
                    "personRelationNum": "80",
                    "personTotal": 100,
                    "houseRelationNum": 45,
                    "houseTotal": 50,
                    "enterpriseRelationNum": 18,
                    "enterpriseTotal": 20,
                    "eventRelationNum": 12,
                    "eventTotal": 15,
                    "matterRelationNum": 8,
                    "matterTotal": 10,
                },
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = governance_adapter.HttpGovernanceAdapter(
            base_url="http://legacy.test/geo-qxst",
            credential_broker=RecordingCredentialBroker(),
            client=client,
        )
        arguments = models.QueryGovernanceOverviewInput.model_validate(
            {"query": {"scope": {"area_code": "330106"}}}
        )
        auth_context = overview_auth_context()
        manifest = ToolRegistry.default().get_manifest(
            "governance.get_governance_overview"
        )
        policy = MinimalPolicyAdapter().evaluate(
            manifest=manifest,
            auth_context=auth_context,
            arguments=arguments,
        )

        result = await adapter.execute(
            manifest=manifest,
            arguments=arguments,
            policy_decision=policy,
            auth_context=auth_context,
        )

    assert len(requests) == 1
    assert requests[0].method == "GET"
    assert requests[0].url.path == "/geo-qxst/base/getBaseTotal"
    assert dict(requests[0].url.params) == {
        "areaName": "county_code",
        "areaCode": "330106",
        "dataBaseType": "2",
    }
    assert requests[0].headers["geoToken"] == "run-scoped-token"
    assert [row.model_dump() for row in result.data.rows] == [
        {
            "subject": "person",
            "subject_label": "人",
            "related_count": 80,
            "total_count": 100,
            "coverage_rate": 80.0,
        },
        {
            "subject": "house",
            "subject_label": "房",
            "related_count": 45,
            "total_count": 50,
            "coverage_rate": 90.0,
        },
        {
            "subject": "enterprise",
            "subject_label": "企",
            "related_count": 18,
            "total_count": 20,
            "coverage_rate": 90.0,
        },
        {
            "subject": "event",
            "subject_label": "事",
            "related_count": 12,
            "total_count": 15,
            "coverage_rate": 80.0,
        },
        {
            "subject": "matter",
            "subject_label": "物",
            "related_count": 8,
            "total_count": 10,
            "coverage_rate": 80.0,
        },
    ]


@pytest.mark.asyncio
async def test_http_adapter_rejects_incomplete_governance_overview() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "state": True,
                "code": 200,
                "msg": "",
                "data": {
                    "personRelationNum": 80,
                    "personTotal": 100,
                    "houseRelationNum": 45,
                    "houseTotal": 50,
                    "enterpriseRelationNum": 18,
                    "enterpriseTotal": 20,
                    "eventRelationNum": 12,
                    "eventTotal": 15,
                    "matterRelationNum": 8,
                },
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = governance_adapter.HttpGovernanceAdapter(
            base_url="http://legacy.test/geo-qxst",
            credential_broker=RecordingCredentialBroker(),
            client=client,
        )
        arguments = models.QueryGovernanceOverviewInput.model_validate(
            {"query": {"scope": {"area_code": "330106"}}}
        )
        auth_context = overview_auth_context()
        manifest = ToolRegistry.default().get_manifest(
            "governance.get_governance_overview"
        )
        policy = MinimalPolicyAdapter().evaluate(
            manifest=manifest,
            auth_context=auth_context,
            arguments=arguments,
        )

        with pytest.raises(
            errors.UpstreamContractError,
            match="missing required counts",
        ):
            await adapter.execute(
                manifest=manifest,
                arguments=arguments,
                policy_decision=policy,
                auth_context=auth_context,
            )


@pytest.mark.parametrize("invalid_count", [True, 1.0, "1.0", -1, "-1"])
@pytest.mark.asyncio
async def test_http_adapter_rejects_non_integer_governance_overview_counts(
    invalid_count: object,
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        data: dict[str, object] = {
            "personRelationNum": invalid_count,
            "personTotal": 100,
            "houseRelationNum": 45,
            "houseTotal": 50,
            "enterpriseRelationNum": 18,
            "enterpriseTotal": 20,
            "eventRelationNum": 12,
            "eventTotal": 15,
            "matterRelationNum": 8,
            "matterTotal": 10,
        }
        return httpx.Response(
            200,
            json={"state": True, "code": 200, "msg": "", "data": data},
        )

    arguments = models.QueryGovernanceOverviewInput.model_validate(
        {"query": {"scope": {"area_code": "330106"}}}
    )
    auth_context = overview_auth_context()
    manifest = ToolRegistry.default().get_manifest(
        "governance.get_governance_overview"
    )
    policy = MinimalPolicyAdapter().evaluate(
        manifest=manifest,
        auth_context=auth_context,
        arguments=arguments,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(errors.UpstreamContractError, match="invalid counts"):
            await governance_adapter.HttpGovernanceAdapter(
                base_url="http://legacy.test/geo-qxst",
                credential_broker=RecordingCredentialBroker(),
                client=client,
            ).execute(
                manifest=manifest,
                arguments=arguments,
                policy_decision=policy,
                auth_context=auth_context,
            )


@pytest.mark.parametrize("area_code", ["33010x", "33010", "3301060010000000"])
@pytest.mark.asyncio
async def test_http_governance_overview_rejects_invalid_area_code_semantically(
    area_code: str,
) -> None:
    arguments = models.QueryGovernanceOverviewInput.model_validate(
        {"query": {"scope": {"area_code": area_code}}}
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: pytest.fail("unexpected HTTP"))
    ) as client:
        adapter = governance_adapter.HttpGovernanceAdapter(
            base_url="http://legacy.test/geo-qxst",
            credential_broker=RecordingCredentialBroker(),
            client=client,
        )
        with pytest.raises(errors.SemanticValidationError, match="area code"):
            await adapter._get_governance_overview_http(
                arguments=arguments,
                auth_context=overview_auth_context(),
                timeout_seconds=8,
                max_attempts=1,
            )


@pytest.mark.asyncio
async def test_http_adapter_queries_enterprise_metrics_with_form_and_run_token() -> None:
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
                    {"areaCode": "330106001", "areaName": "翠苑街道", "total": "31"},
                    {"areaCode": "330106002", "areaName": "北山街道", "total": 18},
                ],
            },
        )

    input_model = getattr(models, "QueryEnterpriseMetricsInput", None)
    assert input_model is not None
    arguments = input_model.model_validate(
        {"query": {"scope": {"area_code": "330106"}, "group_by": ["next_area"]}}
    )
    auth_context = _domain_auth_context(
        entitlement="governance.enterprise.aggregate.read",
        dataset_id="enterprise",
    )
    manifest = ToolRegistry.default().get_manifest(
        "governance.query_enterprise_metrics"
    )
    policy = MinimalPolicyAdapter().evaluate(
        manifest=manifest,
        auth_context=auth_context,
        arguments=arguments,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await governance_adapter.HttpGovernanceAdapter(
            base_url="http://legacy.test/geo-qxst",
            credential_broker=RecordingCredentialBroker(),
            client=client,
        ).execute(
            manifest=manifest,
            arguments=arguments,
            policy_decision=policy,
            auth_context=auth_context,
        )

    assert len(requests) == 1
    assert requests[0].method == "POST"
    assert requests[0].url.path == "/geo-qxst/enterprise/getNextEnterprise"
    assert parse_qs(requests[0].content.decode()) == {
        "areaCode": ["330106"],
        "areaName": ["county_code"],
    }
    assert requests[0].headers["geoToken"] == "run-scoped-token"
    assert [row.model_dump() for row in result.data.rows] == [
        {
            "area_code": "330106001",
            "area_name": "翠苑街道",
            "enterprise_count": 31,
        },
        {
            "area_code": "330106002",
            "area_name": "北山街道",
            "enterprise_count": 18,
        },
    ]


@pytest.mark.parametrize(
    "row",
    [
        {"areaCode": "330106001", "areaName": "翠苑街道"},
        {"areaCode": "330108001", "areaName": "越权街道", "total": 1},
        {"areaCode": "330106001", "areaName": "翠苑街道", "total": -1},
        {"areaCode": "330106001", "areaName": "翠苑街道", "total": True},
        {"areaCode": "330106001", "areaName": "翠苑街道", "total": 1.0},
        {"areaCode": "330106001", "areaName": "翠苑街道", "total": "1.0"},
        {"areaCode": "33010600x", "areaName": "非法街道", "total": 1},
    ],
)
@pytest.mark.asyncio
async def test_http_adapter_rejects_invalid_enterprise_metric_row(
    row: dict[str, object],
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"state": True, "code": 200, "msg": "", "data": [row]},
        )

    arguments = models.QueryEnterpriseMetricsInput.model_validate(
        {"query": {"scope": {"area_code": "330106"}, "group_by": ["next_area"]}}
    )
    auth_context = _domain_auth_context(
        entitlement="governance.enterprise.aggregate.read",
        dataset_id="enterprise",
    )
    manifest = ToolRegistry.default().get_manifest(
        "governance.query_enterprise_metrics"
    )
    policy = MinimalPolicyAdapter().evaluate(
        manifest=manifest,
        auth_context=auth_context,
        arguments=arguments,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(errors.UpstreamContractError):
            await governance_adapter.HttpGovernanceAdapter(
                base_url="http://legacy.test/geo-qxst",
                credential_broker=RecordingCredentialBroker(),
                client=client,
            ).execute(
                manifest=manifest,
                arguments=arguments,
                policy_decision=policy,
                auth_context=auth_context,
            )


@pytest.mark.parametrize("area_code", ["33010x", "33010", "330106001000000"])
def test_enterprise_query_rejects_non_numeric_or_unsupported_area_code(
    area_code: str,
) -> None:
    arguments = models.QueryEnterpriseMetricsInput.model_validate(
        {
            "query": {
                "scope": {"area_code": area_code},
                "group_by": ["next_area"],
            }
        }
    )

    with pytest.raises(errors.SemanticValidationError, match="area code"):
        governance_adapter._validate_enterprise_next_area_query(arguments)


@pytest.mark.asyncio
async def test_http_adapter_closes_only_the_client_it_owns() -> None:
    owned_adapter = governance_adapter.HttpGovernanceAdapter(
        base_url="http://legacy.test/geo-qxst",
        credential_broker=RecordingCredentialBroker(),
    )
    owned_client = owned_adapter._client
    injected_client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: None))
    injected_adapter = governance_adapter.HttpGovernanceAdapter(
        base_url="http://legacy.test/geo-qxst",
        credential_broker=RecordingCredentialBroker(),
        client=injected_client,
    )

    await owned_adapter.aclose()
    await injected_adapter.aclose()

    assert owned_client.is_closed is True
    assert injected_client.is_closed is False
    await injected_client.aclose()


def test_legacy_upstream_errors_have_stable_codes() -> None:
    assert errors.UpstreamTimeout.code == "upstream_timeout"
    assert errors.UpstreamUnavailable.code == "upstream_unavailable"
    assert errors.UpstreamContractError.code == "upstream_contract_error"
    assert errors.SemanticValidationError.code == "semantic_validation_error"


@pytest.mark.asyncio
async def test_http_adapter_normalizes_legacy_business_failure() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"state": False, "code": 0, "msg": "请求失败异常", "data": []},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = governance_adapter.HttpGovernanceAdapter(
            base_url="http://legacy.test/geo-qxst",
            credential_broker=RecordingCredentialBroker(),
            client=client,
        )
        arguments = ResolveAreaInput(query="西湖区")
        auth_context = full_auth_context()
        manifest = ToolRegistry.default().get_manifest("governance.resolve_area")
        policy = MinimalPolicyAdapter().evaluate(
            manifest=manifest,
            auth_context=auth_context,
            arguments=arguments,
        )

        with pytest.raises(errors.UpstreamUnavailable, match="请求失败异常"):
            await adapter.execute(
                manifest=manifest,
                arguments=arguments,
                policy_decision=policy,
                auth_context=auth_context,
            )


@pytest.mark.asyncio
async def test_http_adapter_normalizes_legacy_timeout() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ReadTimeout("legacy timed out", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = governance_adapter.HttpGovernanceAdapter(
            base_url="http://legacy.test/geo-qxst",
            credential_broker=RecordingCredentialBroker(),
            client=client,
        )
        arguments = ResolveAreaInput(query="西湖区")
        auth_context = full_auth_context()
        manifest = ToolRegistry.default().get_manifest("governance.resolve_area")
        policy = MinimalPolicyAdapter().evaluate(
            manifest=manifest,
            auth_context=auth_context,
            arguments=arguments,
        )

        with pytest.raises(errors.UpstreamTimeout):
            await adapter.execute(
                manifest=manifest,
                arguments=arguments,
                policy_decision=policy,
                auth_context=auth_context,
            )

        assert attempts == manifest.limits.max_attempts


@pytest.mark.asyncio
async def test_http_adapter_does_not_retry_non_authentication_4xx() -> None:
    attempts = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(400, json={"message": "bad request"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = governance_adapter.HttpGovernanceAdapter(
            base_url="http://legacy.test/geo-qxst",
            credential_broker=RecordingCredentialBroker(),
            client=client,
        )
        arguments = ResolveAreaInput(query="西湖区")
        auth_context = full_auth_context()
        manifest = ToolRegistry.default().get_manifest("governance.resolve_area")
        policy = MinimalPolicyAdapter().evaluate(
            manifest=manifest,
            auth_context=auth_context,
            arguments=arguments,
        )

        with pytest.raises(errors.UpstreamUnavailable):
            await adapter.execute(
                manifest=manifest,
                arguments=arguments,
                policy_decision=policy,
                auth_context=auth_context,
            )

        assert attempts == 1


@pytest.mark.asyncio
async def test_http_adapter_turns_gateway_token_failure_into_reauthentication() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            json={
                "state": False,
                "code": 403,
                "msg": "token已过期或不存在",
                "data": [],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = governance_adapter.HttpGovernanceAdapter(
            base_url="http://legacy.test/geo-qxst",
            credential_broker=RecordingCredentialBroker(),
            client=client,
        )
        arguments = ResolveAreaInput(query="西湖区")
        auth_context = full_auth_context()
        manifest = ToolRegistry.default().get_manifest("governance.resolve_area")
        policy = MinimalPolicyAdapter().evaluate(
            manifest=manifest,
            auth_context=auth_context,
            arguments=arguments,
        )

        with pytest.raises(errors.ReauthenticationRequired):
            await adapter.execute(
                manifest=manifest,
                arguments=arguments,
                policy_decision=policy,
                auth_context=auth_context,
            )


@pytest.mark.asyncio
async def test_http_adapter_turns_success_status_token_envelope_into_reauthentication() -> None:
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            json={
                "state": False,
                "code": 403,
                "msg": "token已过期或不存在",
                "data": [],
            },
        )
    )
    async with httpx.AsyncClient(transport=transport) as client:
        adapter = governance_adapter.HttpGovernanceAdapter(
            base_url="http://legacy.test/geo-qxst",
            credential_broker=RecordingCredentialBroker(),
            client=client,
        )
        arguments = ResolveAreaInput(query="西湖区")
        auth_context = full_auth_context()
        manifest = ToolRegistry.default().get_manifest("governance.resolve_area")
        policy = MinimalPolicyAdapter().evaluate(
            manifest=manifest,
            auth_context=auth_context,
            arguments=arguments,
        )

        with pytest.raises(errors.ReauthenticationRequired):
            await adapter.execute(
                manifest=manifest,
                arguments=arguments,
                policy_decision=policy,
                auth_context=auth_context,
            )


@pytest.mark.asyncio
async def test_http_adapter_rejects_malformed_legacy_payload() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"state": True, "code": 200, "msg": "", "data": {}},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = governance_adapter.HttpGovernanceAdapter(
            base_url="http://legacy.test/geo-qxst",
            credential_broker=RecordingCredentialBroker(),
            client=client,
        )
        arguments = ResolveAreaInput(query="西湖区")
        auth_context = full_auth_context()
        manifest = ToolRegistry.default().get_manifest("governance.resolve_area")
        policy = MinimalPolicyAdapter().evaluate(
            manifest=manifest,
            auth_context=auth_context,
            arguments=arguments,
        )

        with pytest.raises(errors.UpstreamContractError):
            await adapter.execute(
                manifest=manifest,
                arguments=arguments,
                policy_decision=policy,
                auth_context=auth_context,
            )


@pytest.mark.asyncio
async def test_http_adapter_treats_empty_legacy_area_result_as_no_match() -> None:
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            json={"state": True, "code": 200, "msg": "", "data": []},
        )
    )
    async with httpx.AsyncClient(transport=transport) as client:
        adapter = governance_adapter.HttpGovernanceAdapter(
            base_url="http://legacy.test/geo-qxst",
            credential_broker=RecordingCredentialBroker(),
            client=client,
        )
        arguments = ResolveAreaInput(query="不存在的区划")
        auth_context = full_auth_context()
        manifest = ToolRegistry.default().get_manifest("governance.resolve_area")
        policy = MinimalPolicyAdapter().evaluate(
            manifest=manifest,
            auth_context=auth_context,
            arguments=arguments,
        )

        result = await adapter.execute(
            manifest=manifest,
            arguments=arguments,
            policy_decision=policy,
            auth_context=auth_context,
        )

    assert result.candidate_count == 0
    assert result.data.resolved_area_code is None
    assert result.data.candidates == []


@pytest.mark.asyncio
async def test_http_adapter_rejects_semantics_not_in_the_legacy_whitelist() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(500)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = governance_adapter.HttpGovernanceAdapter(
            base_url="http://legacy.test/geo-qxst",
            credential_broker=RecordingCredentialBroker(),
            client=client,
        )
        arguments = QueryPopulationMetricsInput.model_validate(
            {
                "query": {
                    "metrics": ["person_count"],
                    "scope": {"area_code": "330106"},
                    "filters": [
                        {
                            "field": "person_category",
                            "operator": "eq",
                            "value": "solitary_elderly",
                        },
                        {"field": "age", "operator": "gte", "value": 80},
                    ],
                    "group_by": ["street"],
                }
            }
        )
        auth_context = full_auth_context()
        manifest = ToolRegistry.default().get_manifest(
            "governance.query_population_metrics"
        )
        policy = MinimalPolicyAdapter().evaluate(
            manifest=manifest,
            auth_context=auth_context,
            arguments=arguments,
        )

        with pytest.raises(errors.SemanticValidationError):
            await adapter.execute(
                manifest=manifest,
                arguments=arguments,
                policy_decision=policy,
                auth_context=auth_context,
            )

    assert requests == []


@pytest.mark.asyncio
async def test_http_adapter_resolves_area_with_run_credential_in_header() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "state": True,
                "code": 200,
                "msg": "",
                "data": {"areaname": "西湖区", "areacode": "330106"},
            },
        )

    credentials = RecordingCredentialBroker()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = governance_adapter.HttpGovernanceAdapter(
            base_url="http://legacy.test/geo-qxst",
            credential_broker=credentials,
            client=client,
        )
        arguments = ResolveAreaInput(query="西湖区")
        auth_context = full_auth_context()
        manifest = ToolRegistry.default().get_manifest("governance.resolve_area")
        policy = MinimalPolicyAdapter().evaluate(
            manifest=manifest,
            auth_context=auth_context,
            arguments=arguments,
        )

        result = await adapter.execute(
            manifest=manifest,
            arguments=arguments,
            policy_decision=policy,
            auth_context=auth_context,
        )

    assert requests[0].url.path == "/geo-qxst/area/getAreaInfoByAreaName"
    assert requests[0].headers["geoToken"] == "run-scoped-token"
    assert requests[0].extensions["timeout"]["read"] == (
        manifest.limits.timeout_ms / 1000
    )
    assert parse_qs(requests[0].content.decode()) == {"areaName": ["西湖区"]}
    assert credentials.resolved == [
        {
            "credential_ref": "cred-01",
            "subject_user_id": "user-01",
            "app_id": "full_information_view",
            "run_id": "run-01",
        }
    ]
    assert result.kind == "area_candidates"
    assert result.data.resolved_area_code == "330106"
    assert result.data.candidates[0].level == "district"


@pytest.mark.asyncio
async def test_http_adapter_maps_general_population_to_direct_child_totals() -> None:
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
                        "areaCode": "330106002",
                        "areaName": "北山街道",
                        "total": 64000,
                    },
                    {
                        "areaCode": "330106001",
                        "areaName": "翠苑街道",
                        "total": 128000,
                    }
                ],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = governance_adapter.HttpGovernanceAdapter(
            base_url="http://legacy.test/geo-qxst",
            credential_broker=RecordingCredentialBroker(),
            client=client,
        )
        arguments = QueryPopulationMetricsInput.model_validate(
            {
                "query": {
                    "metrics": ["person_count"],
                    "scope": {"area_code": "330106"},
                    "group_by": ["street"],
                    "order_by": [{"field": "person_count", "direction": "desc"}],
                }
            }
        )
        auth_context = full_auth_context()
        manifest = ToolRegistry.default().get_manifest(
            "governance.query_population_metrics"
        )
        policy = MinimalPolicyAdapter().evaluate(
            manifest=manifest,
            auth_context=auth_context,
            arguments=arguments,
        )

        result = await adapter.execute(
            manifest=manifest,
            arguments=arguments,
            policy_decision=policy,
            auth_context=auth_context,
        )

    assert requests[0].url.path == "/geo-qxst/area/getNextPersonByType"
    assert parse_qs(requests[0].content.decode()) == {
        "areaName": ["county_code"],
        "areaCode": ["330106"],
    }
    assert result.data.rows[0].area_name == "翠苑街道"
    assert result.data.rows[0].person_count == 128000


@pytest.mark.asyncio
async def test_http_adapter_maps_solitary_elderly_query_to_fixed_legacy_fields() -> None:
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
                        "total": 12,
                        "lon": 120.1,
                        "lat": 30.2,
                    }
                ],
            },
        )

    credentials = RecordingCredentialBroker()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = governance_adapter.HttpGovernanceAdapter(
            base_url="http://legacy.test/geo-qxst",
            credential_broker=credentials,
            client=client,
        )
        arguments = QueryPopulationMetricsInput.model_validate(
            {
                "query": {
                    "metrics": ["person_count"],
                    "scope": {"area_code": "330106"},
                    "filters": [
                        {
                            "field": "person_category",
                            "operator": "eq",
                            "value": "solitary_elderly",
                        }
                    ],
                    "group_by": ["street"],
                }
            }
        )
        auth_context = full_auth_context()
        manifest = ToolRegistry.default().get_manifest(
            "governance.query_population_metrics"
        )
        policy = MinimalPolicyAdapter().evaluate(
            manifest=manifest,
            auth_context=auth_context,
            arguments=arguments,
        )

        result = await adapter.execute(
            manifest=manifest,
            arguments=arguments,
            policy_decision=policy,
            auth_context=auth_context,
        )

    assert requests[0].url.path == "/geo-qxst/getNextSiteData"
    assert parse_qs(requests[0].content.decode()) == {
        "areaName": ["county_code"],
        "areaCode": ["330106"],
        "tableName": ["dm_empty_nest_old"],
    }
    assert result.kind == "table"
    assert result.data.rows[0].area_code == "330106001"
    assert result.data.rows[0].area_name == "翠苑街道"
    assert result.data.rows[0].person_count == 12


def _domain_auth_context(*, entitlement: str, dataset_id: str):
    context = population_auth_context()
    return context.model_copy(
        update={
            "entitlements": [entitlement],
            "data_scopes": context.data_scopes.model_copy(
                update={"datasets": [dataset_id]}
            ),
        }
    )


@pytest.mark.asyncio
async def test_http_adapter_maps_housing_request_to_lease_type_contract() -> None:
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
                    {"house_type": "住宅出租", "total": 32},
                    {"house_type": "商铺出租", "total": 8},
                ],
            },
        )

    auth_context = _domain_auth_context(
        entitlement="governance.housing.aggregate.read",
        dataset_id="housing",
    )
    arguments = models.QueryHousingMetricsInput.model_validate(
        {"query": {"scope": {"area_code": "330106"}}}
    )
    manifest = ToolRegistry.default().get_manifest(
        "governance.query_housing_metrics"
    )
    policy = MinimalPolicyAdapter().evaluate(
        manifest=manifest,
        auth_context=auth_context,
        arguments=arguments,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = governance_adapter.HttpGovernanceAdapter(
            base_url="http://legacy.test/geo-qxst",
            credential_broker=RecordingCredentialBroker(),
            client=client,
        )
        result = await adapter.execute(
            manifest=manifest,
            arguments=arguments,
            policy_decision=policy,
            auth_context=auth_context,
        )

    assert requests[0].method == "POST"
    assert requests[0].url.path == "/geo-qxst/house/getRoomLeaseType"
    assert parse_qs(requests[0].content.decode()) == {
        "areaName": ["county_code"],
        "areaCode": ["330106"],
    }
    assert result.data_schema_ref == "schema://data/housing-lease-type-table/1.0.0"
    assert result.data.rows[0].lease_type == "住宅出租"
    assert result.data.rows[0].dwelling_count == 32


@pytest.mark.asyncio
async def test_http_housing_limit_marks_result_truncated_and_blocks_global_claim() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "state": True,
                "code": 200,
                "msg": "",
                "data": [
                    {"house_type": "住宅出租", "total": 32},
                    {"house_type": "商铺出租", "total": 8},
                ],
            },
        )

    auth_context = _domain_auth_context(
        entitlement="governance.housing.aggregate.read",
        dataset_id="housing",
    )
    arguments = models.QueryHousingMetricsInput.model_validate(
        {"query": {"scope": {"area_code": "330106"}, "limit": 1}}
    )
    manifest = ToolRegistry.default().get_manifest(
        "governance.query_housing_metrics"
    )
    policy = MinimalPolicyAdapter().evaluate(
        manifest=manifest,
        auth_context=auth_context,
        arguments=arguments,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await governance_adapter.HttpGovernanceAdapter(
            base_url="http://legacy.test/geo-qxst",
            credential_broker=RecordingCredentialBroker(),
            client=client,
        ).execute(
            manifest=manifest,
            arguments=arguments,
            policy_decision=policy,
            auth_context=auth_context,
        )

    assert result.row_count == 1
    assert result.truncated is True
    claim = AnswerClaim(
        claim_id="claim-sum",
        result_id=result.result_id,
        result_fingerprint=result.result_fingerprint,
        collection="rows",
        row_locator={},
        field="dwelling_count",
        operation="sum",
        value=32,
    )
    finish = StructuredFinish(
        kind="claims",
        summary="出租房合计32套。",
        claims=[claim],
    )
    assessment = await DeterministicCompletionValidator().assess(
        HarnessState(
            tool_results=(
                ToolResult(
                    tool_call_id="tcl-housing-limit",
                    tool_id=manifest.tool_id,
                        tool_version=manifest.tool_version,
                    status="success",
                    summary="查询成功",
                    data_result=result,
                ),
            )
        ),
        FinishAction(
            summary=finish.summary,
            structured_finish=finish,
            legacy=False,
        ),
    )
    assert assessment.status == "revise"
    assert assessment.reason_code == "claim_truncated_aggregate"


@pytest.mark.asyncio
async def test_http_housing_honors_explicit_upstream_truncated_flag() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "state": True,
                "code": 200,
                "msg": "",
                "truncated": True,
                "data": [{"house_type": "住宅出租", "total": 32}],
            },
        )

    auth_context = _domain_auth_context(
        entitlement="governance.housing.aggregate.read",
        dataset_id="housing",
    )
    arguments = models.QueryHousingMetricsInput.model_validate(
        {"query": {"scope": {"area_code": "330106"}, "limit": 1}}
    )
    manifest = ToolRegistry.default().get_manifest(
        "governance.query_housing_metrics"
    )
    policy = MinimalPolicyAdapter().evaluate(
        manifest=manifest,
        auth_context=auth_context,
        arguments=arguments,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await governance_adapter.HttpGovernanceAdapter(
            base_url="http://legacy.test/geo-qxst",
            credential_broker=RecordingCredentialBroker(),
            client=client,
        ).execute(
            manifest=manifest,
            arguments=arguments,
            policy_decision=policy,
            auth_context=auth_context,
        )

    assert result.truncated is True


@pytest.mark.parametrize(
    ("group_by", "malformed_data"),
    [
        (None, {}),
        (None, "not-a-list"),
        (["next_area"], {}),
        (["next_area"], "not-a-list"),
    ],
)
@pytest.mark.asyncio
async def test_housing_non_list_data_fails_closed_as_stable_tool_result(
    group_by: list[str] | None,
    malformed_data: object,
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "state": True,
                "code": 200,
                "msg": "",
                "data": malformed_data,
            },
        )

    auth_context = _domain_auth_context(
        entitlement="governance.housing.aggregate.read",
        dataset_id="housing",
    )
    query: dict[str, object] = {"scope": {"area_code": "330106"}}
    if group_by is not None:
        query["group_by"] = group_by

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = CapabilityService(
            registry=ToolRegistry.default(),
            policy=MinimalPolicyAdapter(),
            adapter=governance_adapter.HttpGovernanceAdapter(
                base_url="http://legacy.test/geo-qxst",
                credential_broker=RecordingCredentialBroker(),
                client=client,
            ),
        )
        result = await service.execute(
            tool_call_id="tcl-housing-malformed-data",
            tool_id="governance.query_housing_metrics",
            raw_arguments={"query": query},
            auth_context=auth_context,
        )

    assert result.status == "failed"
    assert result.data_result is None
    assert result.summary == "现有业务服务暂时不可用。"
    assert result.warnings == ["upstream_contract_error"]


@pytest.mark.asyncio
async def test_http_adapter_preserves_event_finish_rate_decimals() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "state": True,
                "code": 200,
                "msg": "",
                "data": {
                    "gridFinishRate": "75.5%",
                    "communityFinishRate": "82.25%",
                    "streetFinishRate": "90%",
                },
            },
        )

    auth_context = _domain_auth_context(
        entitlement="governance.event.aggregate.read",
        dataset_id="event",
    )
    arguments = models.QueryEventMetricsInput.model_validate(
        {"query": {"scope": {"area_code": "330106"}}}
    )
    manifest = ToolRegistry.default().get_manifest(
        "governance.query_event_metrics"
    )
    policy = MinimalPolicyAdapter().evaluate(
        manifest=manifest,
        auth_context=auth_context,
        arguments=arguments,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await governance_adapter.HttpGovernanceAdapter(
            base_url="http://legacy.test/geo-qxst",
            credential_broker=RecordingCredentialBroker(),
            client=client,
        ).execute(
            manifest=manifest,
            arguments=arguments,
            policy_decision=policy,
            auth_context=auth_context,
        )

    assert result.data_schema_ref == "schema://data/event-finish-rate-table/1.0.0"
    assert [(row.level, row.finish_rate) for row in result.data.rows] == [
        ("grid", 75.5),
        ("community", 82.25),
        ("street", 90.0),
    ]


@pytest.mark.asyncio
async def test_http_adapter_rejects_missing_event_finish_rate() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "state": True,
                "code": 200,
                "msg": "",
                "data": {
                    "gridFinishRate": "75.5%",
                    "communityFinishRate": "82.25%",
                },
            },
        )

    auth_context = _domain_auth_context(
        entitlement="governance.event.aggregate.read",
        dataset_id="event",
    )
    arguments = models.QueryEventMetricsInput.model_validate(
        {"query": {"scope": {"area_code": "330106"}}}
    )
    manifest = ToolRegistry.default().get_manifest(
        "governance.query_event_metrics"
    )
    policy = MinimalPolicyAdapter().evaluate(
        manifest=manifest,
        auth_context=auth_context,
        arguments=arguments,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = governance_adapter.HttpGovernanceAdapter(
            base_url="http://legacy.test/geo-qxst",
            credential_broker=RecordingCredentialBroker(),
            client=client,
        )
        with pytest.raises(
            errors.UpstreamContractError,
            match="required finish rates",
        ):
            await adapter.execute(
                manifest=manifest,
                arguments=arguments,
                policy_decision=policy,
                auth_context=auth_context,
            )


@pytest.mark.asyncio
async def test_http_adapter_maps_building_profile_and_verifies_area_binding() -> None:
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
                        "_source": {
                            "code": "3301060010016838",
                            "building_path": "测试路18号1幢",
                            "longitude": 120.1,
                            "latitude": 30.2,
                        }
                    }
                ],
            },
        )

    auth_context = _domain_auth_context(
        entitlement="governance.object.profile.read",
        dataset_id="governance_objects",
    )
    arguments = models.GetObjectProfileInput.model_validate(
        {
            "object_ref": {
                "object_type": "building",
                "object_id": "3301060010016838",
            },
            "scope": {"area_code": "330106"},
            "field_sets": ["summary", "location"],
        }
    )
    manifest = ToolRegistry.default().get_manifest(
        "governance.get_object_profile"
    )
    policy = MinimalPolicyAdapter().evaluate(
        manifest=manifest,
        auth_context=auth_context,
        arguments=arguments,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await governance_adapter.HttpGovernanceAdapter(
            base_url="http://legacy.test/geo-qxst",
            credential_broker=RecordingCredentialBroker(),
            client=client,
        ).execute(
            manifest=manifest,
            arguments=arguments,
            policy_decision=policy,
            auth_context=auth_context,
        )

    assert requests[0].method == "POST"
    assert requests[0].url.path == "/geo-qxst/house/getHouseDetails"
    assert parse_qs(requests[0].content.decode()) == {
        "houseId": ["3301060010016838"]
    }
    assert result.data.area_code == "330106"
    assert result.data.title == "测试路18号1幢"


@pytest.mark.asyncio
async def test_http_adapter_rejects_building_outside_declared_area_scope() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "state": True,
                "code": 200,
                "msg": "",
                "data": [
                    {
                        "_source": {
                            "code": "3301080010010001",
                            "building_path": "另一区域楼栋",
                        }
                    }
                ],
            },
        )

    auth_context = _domain_auth_context(
        entitlement="governance.object.profile.read",
        dataset_id="governance_objects",
    )
    arguments = models.GetObjectProfileInput.model_validate(
        {
            "object_ref": {
                "object_type": "building",
                "object_id": "3301060010016838",
            },
            "scope": {"area_code": "330106"},
            "field_sets": ["summary"],
        }
    )
    manifest = ToolRegistry.default().get_manifest(
        "governance.get_object_profile"
    )
    policy = MinimalPolicyAdapter().evaluate(
        manifest=manifest,
        auth_context=auth_context,
        arguments=arguments,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = governance_adapter.HttpGovernanceAdapter(
            base_url="http://legacy.test/geo-qxst",
            credential_broker=RecordingCredentialBroker(),
            client=client,
        )
        with pytest.raises(errors.UpstreamContractError, match="outside"):
            await adapter.execute(
                manifest=manifest,
                arguments=arguments,
                policy_decision=policy,
                auth_context=auth_context,
            )
