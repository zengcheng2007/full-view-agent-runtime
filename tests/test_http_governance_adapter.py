from urllib.parse import parse_qs

import httpx
import pytest
from pydantic import SecretStr

from full_view_agent.application import errors
from full_view_agent.application.policy import MinimalPolicyAdapter
from full_view_agent.application.tool_registry import ToolRegistry
from full_view_agent.domain.models import QueryPopulationMetricsInput, ResolveAreaInput
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


def test_http_governance_adapter_is_available() -> None:
    assert getattr(governance_adapter, "HttpGovernanceAdapter", None) is not None


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
