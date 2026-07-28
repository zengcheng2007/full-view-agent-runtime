"""S1-A：SemanticToolExecutor —— Harness 执行层的语义入口包装。

语义查询经解析后走既有 CapabilityService/Policy/Adapter 执行真实规范
Tool；非语义 Tool 原样直通。失败/拒绝按结构化分类落 ToolResult，
成功结果附加语义血缘，并由 Compiler 复核结果 Schema。
SemanticToolCallFingerprinter 使循环检测基于规范动作，同义问法收敛、
不同查询不误判、非法输入回退原始指纹。
"""

import pytest

from full_view_agent.application.capability_service import CapabilityService
from full_view_agent.application.fingerprints import canonical_fingerprint
from full_view_agent.application.harness import (
    DefaultToolCallFingerprinter,
    ToolAction,
)
from full_view_agent.application.policy import MinimalPolicyAdapter
from full_view_agent.application.semantic_executor import (
    SemanticToolCallFingerprinter,
    SemanticToolExecutor,
    default_tool_call_fingerprint,
)
from full_view_agent.application.tool_registry import ToolRegistry
from full_view_agent.domain.models import (
    InternalToolManifest,
    PolicyDecision,
    PopulationMetricRow,
    PopulationMetricTable,
    TableDataResult,
)
from full_view_agent.infrastructure.governance_adapter import (
    InMemoryGovernanceAdapter,
)
from full_view_agent.semantic.action_resolver import (
    SEMANTIC_QUERY_TOOL_ID,
    SEMANTIC_QUERY_TOOL_VERSION,
    SemanticActionResolver,
)
from full_view_agent.semantic.catalog import SemanticCatalog

from .test_policy import population_auth_context


def _capability(adapter: object | None = None) -> CapabilityService:
    return CapabilityService(
        registry=ToolRegistry.default(),
        policy=MinimalPolicyAdapter(),
        adapter=adapter or InMemoryGovernanceAdapter(),
    )


def _executor(capability: CapabilityService | None = None) -> SemanticToolExecutor:
    registry = ToolRegistry.default()
    policy = MinimalPolicyAdapter()
    return SemanticToolExecutor(
        inner=capability or _capability(),
        resolver=SemanticActionResolver(
            catalog=SemanticCatalog.default(),
            registry=registry,
            policy=policy,
        ),
    )


def _semantic_args(**spec_overrides: object) -> dict[str, object]:
    spec: dict[str, object] = {
        "subject": "population",
        "metrics": ["person_count"],
        "scope": {"area_code": "330106"},
        "filters": [
            {"field": "person_category", "operator": "eq", "value": "solitary_elderly"}
        ],
        "group_by": ["street"],
    }
    spec.update(spec_overrides)
    return {
        "catalog_version": SemanticCatalog.default().catalog_version,
        "catalog_fingerprint": SemanticCatalog.default().execution_fingerprint,
        "spec": spec,
    }


# ---------------------------------------------------------------------------
# 直通：非 semantic_query Tool 行为完全不变
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_executor_passes_through_canonical_tools_untouched() -> None:
    executor = _executor()
    auth = population_auth_context()

    direct = await _capability().execute(
        tool_call_id="tcl-direct",
        tool_id="governance.query_population_metrics",
        raw_arguments={
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
        },
        auth_context=auth,
    )
    wrapped = await executor.execute(
        tool_call_id="tcl-direct",
        tool_id="governance.query_population_metrics",
        raw_arguments={
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
        },
        auth_context=auth,
    )

    assert wrapped.status == "success"
    assert wrapped.semantic_lineage is None
    assert wrapped.data_result is not None
    assert direct.data_result is not None
    assert wrapped.data_result.data == direct.data_result.data


# ---------------------------------------------------------------------------
# 成功路径：解析 → 规范执行 → 血缘 + 结果 Schema 复核
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_executor_resolves_semantic_query_to_canonical_execution() -> None:
    executor = _executor()

    result = await executor.execute(
        tool_call_id="tcl-semantic-1",
        tool_id=SEMANTIC_QUERY_TOOL_ID,
        raw_arguments=_semantic_args(),
        auth_context=population_auth_context(),
    )

    assert result.status == "success"
    # ToolResult 归属规范 Tool：生产 manifest/Evidence 读取不受影响。
    assert result.tool_id == "governance.query_population_metrics"
    assert result.tool_version == "1.0.0"
    assert result.data_result is not None
    assert result.data_result.kind == "table"

    lineage = result.semantic_lineage
    assert lineage is not None
    assert lineage.virtual_tool_id == SEMANTIC_QUERY_TOOL_ID
    assert lineage.virtual_tool_version == SEMANTIC_QUERY_TOOL_VERSION
    assert lineage.canonical_tool_id == "governance.query_population_metrics"
    assert lineage.subject == "population"
    assert lineage.area_code == "330106"
    assert [m.metric_id for m in lineage.metric_definitions] == ["person_count"]


@pytest.mark.asyncio
async def test_executor_rejected_semantic_error_is_failed_with_codes() -> None:
    executor = _executor()

    result = await executor.execute(
        tool_call_id="tcl-bad-metric",
        tool_id=SEMANTIC_QUERY_TOOL_ID,
        raw_arguments=_semantic_args(metrics=["household_count"]),
        auth_context=population_auth_context(),
    )

    assert result.status == "failed"
    assert result.tool_id == SEMANTIC_QUERY_TOOL_ID
    assert result.tool_version == SEMANTIC_QUERY_TOOL_VERSION
    assert "UNKNOWN_METRIC" in result.warnings
    assert result.data_result is None
    assert result.semantic_lineage is None


@pytest.mark.asyncio
async def test_executor_authorization_rejection_is_denied() -> None:
    executor = _executor()
    auth = population_auth_context()
    auth = auth.model_copy(update={"entitlements": []})

    result = await executor.execute(
        tool_call_id="tcl-no-entitlement",
        tool_id=SEMANTIC_QUERY_TOOL_ID,
        raw_arguments=_semantic_args(),
        auth_context=auth,
    )

    assert result.status == "denied"
    assert "SUBJECT_NOT_ENTITLED" in result.warnings


@pytest.mark.asyncio
async def test_executor_housing_subject_is_denied_entry_not_executed() -> None:
    executor = _executor()
    auth = population_auth_context().model_copy(
        update={
            "entitlements": [
                "governance.population.aggregate.read",
                "governance.housing.aggregate.read",
            ],
            "data_scopes": population_auth_context().data_scopes.model_copy(
                update={"datasets": ["population", "housing"]}
            ),
        }
    )

    result = await executor.execute(
        tool_call_id="tcl-housing",
        tool_id=SEMANTIC_QUERY_TOOL_ID,
        raw_arguments={
            "catalog_version": SemanticCatalog.default().catalog_version,
            "catalog_fingerprint": SemanticCatalog.default().execution_fingerprint,
            "spec": {
                "subject": "housing",
                "metrics": ["dwelling_count"],
                "scope": {"area_code": "330106"},
            }
        },
        auth_context=auth,
    )

    # S1-A fail closed：housing 不绑定，且不得触达住房 Adapter。
    assert result.status == "failed"
    assert "SUBJECT_NOT_BINDABLE" in result.warnings


@pytest.mark.asyncio
async def test_executor_malformed_spec_is_failed_semantic_input_invalid() -> None:
    executor = _executor()

    result = await executor.execute(
        tool_call_id="tcl-malformed",
        tool_id=SEMANTIC_QUERY_TOOL_ID,
        raw_arguments={"spec": {"subject": "population"}},
        auth_context=population_auth_context(),
    )

    assert result.status == "failed"
    assert "SEMANTIC_INPUT_INVALID" in result.warnings


# ---------------------------------------------------------------------------
# 结果 Schema 复核：规范 Tool 返回漂移 Schema 时 fail closed
# ---------------------------------------------------------------------------


class _WrongSchemaAdapter:
    """返回 population 行但 schema_ref 漂移到 housing 的故障 Adapter。"""

    async def execute(
        self,
        *,
        manifest: InternalToolManifest,
        arguments: object,
        policy_decision: PolicyDecision,
        auth_context: object,
    ) -> TableDataResult:
        del manifest, arguments, policy_decision, auth_context
        data = PopulationMetricTable(
            rows=[
                PopulationMetricRow(
                    area_code="330106001", area_name="翠苑街道", person_count=1
                )
            ]
        )
        return TableDataResult(
            result_id="res-drift",
            data_schema_ref="schema://data/housing-lease-type-table/1.0.0",
            result_fingerprint=canonical_fingerprint(
                domain="data-result:population-metric-table:1.0.0", value=data
            ),
            data=data,
            row_count=1,
        )


@pytest.mark.asyncio
async def test_executor_rejects_result_schema_drift_after_execution() -> None:
    executor = _executor(_capability(adapter=_WrongSchemaAdapter()))

    result = await executor.execute(
        tool_call_id="tcl-drift",
        tool_id=SEMANTIC_QUERY_TOOL_ID,
        raw_arguments=_semantic_args(),
        auth_context=population_auth_context(),
    )

    assert result.status == "failed"
    assert "SEMANTIC_RESULT_SCHEMA_MISMATCH" in result.warnings
    assert result.semantic_lineage is None


# ---------------------------------------------------------------------------
# 循环指纹：基于规范动作，同义收敛、异义区分、非法回退
# ---------------------------------------------------------------------------


def _fingerprinter() -> SemanticToolCallFingerprinter:
    registry = ToolRegistry.default()
    policy = MinimalPolicyAdapter()
    return SemanticToolCallFingerprinter(
        resolver=SemanticActionResolver(
            catalog=SemanticCatalog.default(),
            registry=registry,
            policy=policy,
        )
    )


def test_fingerprinter_converges_synonymous_specs_to_canonical_fingerprint() -> None:
    fingerprinter = _fingerprinter()
    auth = population_auth_context()
    first = ToolAction(tool_id=SEMANTIC_QUERY_TOOL_ID, arguments=_semantic_args())
    second = ToolAction(
        tool_id=SEMANTIC_QUERY_TOOL_ID,
        arguments={
            "catalog_version": SemanticCatalog.default().catalog_version,
            "catalog_fingerprint": SemanticCatalog.default().execution_fingerprint,
            "spec": {
                "group_by": ["street"],
                "filters": [
                    {
                        "value": "solitary_elderly",
                        "operator": "eq",
                        "field": "person_category",
                    }
                ],
                "scope": {"include_descendants": True, "area_code": "330106"},
                "metrics": ["person_count"],
                "subject": "population",
            }
        },
    )

    assert fingerprinter.fingerprint(first, auth_context=auth) == (
        fingerprinter.fingerprint(second, auth_context=auth)
    )


def test_fingerprinter_semantic_call_matches_equivalent_direct_call() -> None:
    # 语义入口与直接调用同一规范动作共享循环计数，防止重复查询绕检测。
    fingerprinter = _fingerprinter()
    auth = population_auth_context()
    semantic = ToolAction(tool_id=SEMANTIC_QUERY_TOOL_ID, arguments=_semantic_args())
    direct = ToolAction(
        tool_id="governance.query_population_metrics",
        arguments={
            "query": {
                "schema_version": "1.1",
                "metrics": ["person_count"],
                "scope": {"area_code": "330106", "include_descendants": True},
                "filters": [
                    {
                        "field": "person_category",
                        "operator": "eq",
                        "value": "solitary_elderly",
                    }
                ],
                "group_by": ["street"],
                "limit": 200,
                "presentation_hint": "table",
            }
        },
    )

    assert fingerprinter.fingerprint(semantic, auth_context=auth) == (
        fingerprinter.fingerprint(direct, auth_context=auth)
    )


def test_fingerprinter_distinguishes_different_queries() -> None:
    fingerprinter = _fingerprinter()
    auth = population_auth_context()
    district = ToolAction(tool_id=SEMANTIC_QUERY_TOOL_ID, arguments=_semantic_args())
    street = ToolAction(
        tool_id=SEMANTIC_QUERY_TOOL_ID,
        arguments=_semantic_args(scope={"area_code": "330106001"}, group_by=["community"]),
    )

    assert fingerprinter.fingerprint(district, auth_context=auth) != (
        fingerprinter.fingerprint(street, auth_context=auth)
    )


def test_fingerprinter_falls_back_to_raw_for_invalid_spec() -> None:
    fingerprinter = _fingerprinter()
    default = DefaultToolCallFingerprinter()
    auth = population_auth_context()
    invalid = ToolAction(
        tool_id=SEMANTIC_QUERY_TOOL_ID,
        arguments={"spec": {"subject": "population"}},
    )

    assert fingerprinter.fingerprint(invalid, auth_context=auth) == (
        default.fingerprint(invalid, auth_context=auth)
    )


def test_fingerprinter_does_not_touch_non_semantic_actions() -> None:
    fingerprinter = _fingerprinter()
    auth = population_auth_context()
    action = ToolAction(
        tool_id="governance.resolve_area", arguments={"query": "西湖区"}
    )

    assert fingerprinter.fingerprint(action, auth_context=auth) == (
        default_tool_call_fingerprint(action)
    )
