"""企业行业分布的受控合同、真实 HTTP 映射和中文展示。"""

import httpx
import pytest

from full_view_agent.application.harness import ToolAction
from full_view_agent.application.policy import MinimalPolicyAdapter
from full_view_agent.application.tool_observation_service import _table_presentation
from full_view_agent.application.tool_registry import ToolRegistry
from full_view_agent.contract_registry import SCHEMA_MODELS
from full_view_agent.domain import models
from full_view_agent.infrastructure.governance_adapter import (
    HttpGovernanceAdapter,
    UpstreamContractError,
)
from full_view_agent.infrastructure.postgres_persistence import PostgresAgentPersistence
from full_view_agent.semantic.authorization import SubjectAuthorization
from full_view_agent.semantic.catalog import SemanticCatalog
from full_view_agent.semantic.compiler import SemanticCompiler
from full_view_agent.semantic.query_spec import SemanticQuerySpec

from .test_http_governance_adapter import RecordingCredentialBroker, _domain_auth_context


def _arguments() -> models.QueryEnterpriseMetricsInput:
    return models.QueryEnterpriseMetricsInput.model_validate(
        {"query": {"scope": {"area_code": "330106"}, "group_by": ["industry_name"]}}
    )


def _auth() -> models.AuthContext:
    return _domain_auth_context(
        entitlement="governance.enterprise.aggregate.read", dataset_id="enterprise"
    )


async def _execute(payload: object) -> models.TableDataResult:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/geo-qxst/api/getEnterpriseTypeCount"
        assert dict(request.url.params) == {
            "areaCodeName": "county_code",
            "areaCodeValue": "330106",
            "typeColumn": "industry_name",
        }
        assert request.headers["geoToken"] == "run-scoped-token"
        return httpx.Response(
            200, json={"state": True, "code": 200, "msg": "", "data": payload}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = HttpGovernanceAdapter(
            base_url="http://legacy.test/geo-qxst",
            credential_broker=RecordingCredentialBroker(),
            client=client,
        )
        manifest = ToolRegistry.default().get_manifest(
            "governance.query_enterprise_metrics"
        )
        auth = _auth()
        policy = MinimalPolicyAdapter().evaluate(
            manifest=manifest, auth_context=auth, arguments=_arguments()
        )
        result = await adapter.execute(
            manifest=manifest,
            arguments=_arguments(),
            auth_context=auth,
            policy_decision=policy,
        )
        assert isinstance(result, models.TableDataResult)
        return result


@pytest.mark.asyncio
async def test_http_industry_distribution_uses_real_contract_without_dictionary() -> None:
    result = await _execute(
        [
            {"industry_name": "零售业", "count": 18},
            {"industry_name": "信息技术", "count": "7"},
        ]
    )
    assert result.data_schema_ref == (
        "schema://data/enterprise-industry-distribution-table/1.0.0"
    )
    assert [
        (row.industry_name, row.enterprise_count) for row in result.data.rows
    ] == [("零售业", 18), ("信息技术", 7)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        [{"industry_name": "", "count": 1}],
        [{"industry_name": "零售业", "count": True}],
        [{"industry_name": "零售业", "count": 1.5}],
        [{"industry_name": "零售业", "count": -1}],
        [
            {"industry_name": "零售业", "count": 1},
            {"industry_name": "零售业", "count": 2},
        ],
        [{"industry_name": f"行业{i}", "count": i} for i in range(9)],
        {"industry_name": "零售业", "count": 1},
    ],
)
async def test_http_industry_distribution_fails_closed(payload: object) -> None:
    with pytest.raises(UpstreamContractError):
        await _execute(payload)


def test_semantic_industry_shape_compiles_to_existing_enterprise_tool() -> None:
    plan = SemanticCompiler(SemanticCatalog.default()).compile(
        SemanticQuerySpec(
            subject="enterprise",
            metrics=["enterprise_count"],
            scope={"area_code": "330106"},
            group_by=["industry_name"],
        ),
        authorization=SubjectAuthorization(
            entitlements=("governance.enterprise.aggregate.read",),
            datasets=("enterprise",),
            field_policy_set="governance_analyst_v1",
            area_scopes=(
                models.AuthorizedAreaScope(
                    area_code="3301", include_descendants=True
                ),
            ),
        ),
    )
    assert plan.steps[0].capability_id == "governance.query_enterprise_metrics"
    assert plan.steps[0].arguments["query"]["group_by"] == ["industry_name"]
    assert plan.expected_result.data_schema_ref.endswith(
        "/enterprise-industry-distribution-table/1.0.0"
    )


def test_industry_presentation_is_chinese_table_metric_bar_and_csv() -> None:
    data = models.EnterpriseIndustryDistributionTable(
        rows=[
            models.EnterpriseIndustryDistributionRow(
                industry_name="零售业", enterprise_count=18
            )
        ]
    )
    result = models.TableDataResult(
        result_id="res-industry",
        data_schema_ref="schema://data/enterprise-industry-distribution-table/1.0.0",
        result_fingerprint="sha256:test",
        data=data,
        row_count=1,
    )
    presentation = _table_presentation(
        result,
        action=ToolAction(
            tool_id="governance.query_enterprise_metrics",
            arguments={"query": {"group_by": ["industry_name"]}},
        ),
    )
    assert presentation.title == "企业行业分布"
    assert "已返回企业数量合计" in presentation.summary
    assert [item.kind for item in presentation.visualizations] == [
        "table",
        "metric",
        "bar",
    ]
    assert presentation.download is not None
    assert presentation.download.formats == ["csv"]


def test_industry_contract_is_registered_on_existing_tool() -> None:
    manifest = ToolRegistry.default().get_manifest(
        "governance.query_enterprise_metrics"
    )
    ref = "schema://data/enterprise-industry-distribution-table/1.0.0"
    assert ref in {schema.data_schema_ref for schema in manifest.result_schemas}
    assert (
        SCHEMA_MODELS[
            "data/tool-specific/enterprise-industry-distribution-table.schema.json"
        ]
        is models.EnterpriseIndustryDistributionTable
    )


def test_v023_is_loaded_by_automatic_migrations() -> None:
    persistence = PostgresAgentPersistence(
        dsn="postgresql://unused:unused@127.0.0.1:1/unused",
        schema="fva_test_migration_inventory",
    )
    assert any("V023" in sql for sql in persistence._p2_migration_statements())
