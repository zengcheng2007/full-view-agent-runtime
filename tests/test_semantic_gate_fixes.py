"""S1-A Gate 修复：版本钉扎、拒绝账本与必填筛选呈现。"""

import pytest

from full_view_agent.application.errors import ModelContractError
from full_view_agent.application.harness import HarnessState, ToolAction
from full_view_agent.application.model_planner import ModelPlanner
from full_view_agent.application.model_provider import (
    ModelRequest,
    ModelResponse,
    ModelToolCall,
    ModelToolDefinition,
    ModelUsage,
)
from full_view_agent.application.policy import MinimalPolicyAdapter
from full_view_agent.application.semantic_wiring import (
    build_semantic_capability_stack,
)
from full_view_agent.application.tool_registry import ToolRegistry
from full_view_agent.domain.models import QueryPopulationMetricsInput
from full_view_agent.infrastructure.denial_ledger import InMemoryDenialLedger
from full_view_agent.infrastructure.governance_adapter import (
    InMemoryGovernanceAdapter,
)
from full_view_agent.semantic.action_resolver import (
    SEMANTIC_QUERY_TOOL_ID,
    RejectedSemanticAction,
    SemanticActionResolver,
)
from full_view_agent.semantic.catalog import SemanticCatalog
from full_view_agent.semantic.presenter import SemanticToolPresenter

from .test_policy import population_auth_context


def _semantic_arguments(
    *,
    area_code: str = "330106",
    catalog_version: str | None = None,
) -> dict[str, object]:
    return {
        "catalog_version": catalog_version or SemanticCatalog.default().catalog_version,
        "catalog_fingerprint": SemanticCatalog.default().execution_fingerprint,
        "spec": {
            "subject": "population",
            "metrics": ["person_count"],
            "scope": {"area_code": area_code},
            "filters": [
                {
                    "field": "person_category",
                    "operator": "eq",
                    "value": "solitary_elderly",
                }
            ],
            "group_by": ["street"],
        },
    }


def test_presenter_supplies_server_owned_catalog_version() -> None:
    catalog = SemanticCatalog.default()
    presentation = SemanticToolPresenter(catalog=catalog).present(
        auth_context=population_auth_context()
    )

    assert presentation is not None
    server_arguments = getattr(presentation, "server_arguments", {})
    assert server_arguments["catalog_version"] == catalog.catalog_version
    assert server_arguments["catalog_fingerprint"].startswith("sha256:")
    assert "catalog_version" not in presentation.input_schema.get("properties", {})
    assert "catalog_fingerprint" not in presentation.input_schema.get("properties", {})


@pytest.mark.asyncio
async def test_model_planner_injects_server_arguments_before_checkpoint() -> None:
    catalog = SemanticCatalog.default()
    advertised_tool = ModelToolDefinition(
        tool_id=SEMANTIC_QUERY_TOOL_ID,
        description="semantic",
        input_schema={"type": "object"},
        server_arguments={
            "catalog_version": catalog.catalog_version,
            "catalog_fingerprint": catalog.execution_fingerprint,
        },
    )

    class _ContextBuilder:
        async def build(self, **_: object) -> ModelRequest:
            return ModelRequest(messages=(), tools=(advertised_tool,))

    class _Provider:
        async def complete(self, request: ModelRequest) -> ModelResponse:
            del request
            return ModelResponse(
                content=None,
                tool_calls=(
                    ModelToolCall(
                        tool_id=SEMANTIC_QUERY_TOOL_ID,
                        arguments={"spec": _semantic_arguments()["spec"]},
                    ),
                ),
                finish_reason="tool_calls",
                usage=ModelUsage(total_tokens=10),
            )

    action = await ModelPlanner(
        provider=_Provider(),
        context_builder=_ContextBuilder(),
        user_id="user-01",
        auth_context=population_auth_context(),
    ).decide(HarnessState())

    assert isinstance(action, ToolAction)
    assert action.arguments["catalog_version"] == catalog.catalog_version
    assert action.arguments["catalog_fingerprint"] == catalog.execution_fingerprint


@pytest.mark.asyncio
async def test_model_cannot_override_server_owned_catalog_version() -> None:
    catalog = SemanticCatalog.default()
    advertised_tool = ModelToolDefinition(
        tool_id=SEMANTIC_QUERY_TOOL_ID,
        description="semantic",
        input_schema={"type": "object"},
        server_arguments={
            "catalog_version": catalog.catalog_version,
            "catalog_fingerprint": catalog.execution_fingerprint,
        },
    )

    class _ContextBuilder:
        async def build(self, **_: object) -> ModelRequest:
            return ModelRequest(messages=(), tools=(advertised_tool,))

    class _Provider:
        async def complete(self, request: ModelRequest) -> ModelResponse:
            del request
            return ModelResponse(
                content=None,
                tool_calls=(
                    ModelToolCall(
                        tool_id=SEMANTIC_QUERY_TOOL_ID,
                        arguments={
                            "catalog_version": "forged",
                            "spec": _semantic_arguments()["spec"],
                        },
                    ),
                ),
                finish_reason="tool_calls",
                usage=ModelUsage(total_tokens=10),
            )

    with pytest.raises(ModelContractError, match="server-owned"):
        await ModelPlanner(
            provider=_Provider(),
            context_builder=_ContextBuilder(),
            user_id="user-01",
            auth_context=population_auth_context(),
        ).decide(HarnessState())


def test_presenter_marks_catalog_required_filters_as_mandatory() -> None:
    presentation = SemanticToolPresenter(catalog=SemanticCatalog.default()).present(
        auth_context=population_auth_context()
    )

    assert presentation is not None
    assert "必填筛选 person_category eq solitary_elderly" in presentation.description
    assert "查询必须携带" in presentation.description


def test_resolver_rejects_stale_server_pinned_catalog_version() -> None:
    catalog = SemanticCatalog.default()
    resolver = SemanticActionResolver(
        catalog=catalog,
        registry=ToolRegistry.default(),
        policy=MinimalPolicyAdapter(),
    )

    resolution = resolver.compile_action(
        _semantic_arguments(catalog_version="semantic-catalog-stale"),
        auth_context=population_auth_context(),
    )

    assert isinstance(resolution, RejectedSemanticAction)
    assert resolution.codes == ("CATALOG_VERSION_MISMATCH",)


def test_resolver_rejects_unversioned_metric_definition_drift() -> None:
    original = SemanticCatalog.default()
    presentation = SemanticToolPresenter(catalog=original).present(
        auth_context=population_auth_context()
    )
    assert presentation is not None
    drifted_subjects = original.subjects
    population = drifted_subjects["population"]
    changed_metric = population.metrics[0].model_copy(
        update={"definition_version": "2.0-unversioned"}
    )
    drifted_subjects["population"] = population.model_copy(
        update={"metrics": (changed_metric,)}
    )
    drifted = SemanticCatalog(
        catalog_version=original.catalog_version,
        supported_spec_versions=original.supported_spec_versions,
        subjects=drifted_subjects,
        bindings=original.bindings,
    )
    resolver = SemanticActionResolver(
        catalog=drifted,
        registry=ToolRegistry.default(),
        policy=MinimalPolicyAdapter(),
    )
    arguments = _semantic_arguments()
    arguments.update(presentation.server_arguments)

    resolution = resolver.compile_action(
        arguments,
        auth_context=population_auth_context(),
    )

    assert isinstance(resolution, RejectedSemanticAction)
    assert resolution.codes == ("CATALOG_FINGERPRINT_MISMATCH",)


@pytest.mark.asyncio
async def test_semantic_area_denial_enters_canonical_denial_ledger() -> None:
    registry = ToolRegistry.default(housing_next_area_enabled=False)
    ledger = InMemoryDenialLedger()
    stack = build_semantic_capability_stack(
        registry=registry,
        adapter=InMemoryGovernanceAdapter(),
        denial_ledger=ledger,
    )
    auth = population_auth_context()

    result = await stack.executor.execute(
        tool_call_id="tcl-semantic-denied",
        tool_id=SEMANTIC_QUERY_TOOL_ID,
        raw_arguments=_semantic_arguments(area_code="330108"),
        auth_context=auth,
    )

    canonical_arguments = QueryPopulationMetricsInput.model_validate(
        {
            "query": {
                "metrics": ["person_count"],
                "scope": {"area_code": "330108"},
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
    manifest = registry.get_manifest("governance.query_population_metrics")

    assert result.status == "denied"
    assert "AREA_OUT_OF_SCOPE" in result.warnings
    assert result.policy is not None
    assert result.policy.decision == "deny"
    canonical_decision = MinimalPolicyAdapter().evaluate(
        manifest=manifest,
        auth_context=auth,
        arguments=canonical_arguments,
    )
    assert (
        result.policy.arguments_fingerprint
        == canonical_decision.arguments_fingerprint
    )
    assert result.policy.request_fingerprint == canonical_decision.request_fingerprint
    assert await ledger.contains(
        manifest=manifest,
        arguments=canonical_arguments,
        auth_context=auth,
    )

    direct_retry = await stack.capability.execute(
        tool_call_id="tcl-direct-retry",
        tool_id=manifest.tool_id,
        raw_arguments=canonical_arguments.model_dump(mode="json"),
        auth_context=auth,
    )
    assert direct_retry.status == "denied"
    assert direct_retry.warnings == ["DENIAL_LEDGER_MATCH"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "auth_update",
    [
        {"entitlements": []},
        {
            "data_scopes": population_auth_context().data_scopes.model_copy(
                update={"datasets": ["housing"]}
            )
        },
    ],
    ids=["missing-entitlement", "missing-dataset"],
)
async def test_semantic_subject_denials_enter_canonical_ledger(
    auth_update: dict[str, object],
) -> None:
    registry = ToolRegistry.default(housing_next_area_enabled=False)
    ledger = InMemoryDenialLedger()
    stack = build_semantic_capability_stack(
        registry=registry,
        adapter=InMemoryGovernanceAdapter(),
        denial_ledger=ledger,
    )
    auth = population_auth_context().model_copy(update=auth_update)
    result = await stack.executor.execute(
        tool_call_id="tcl-semantic-subject-denied",
        tool_id=SEMANTIC_QUERY_TOOL_ID,
        raw_arguments=_semantic_arguments(),
        auth_context=auth,
    )
    canonical_arguments = QueryPopulationMetricsInput.model_validate(
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

    assert result.status == "denied"
    assert await ledger.contains(
        manifest=registry.get_manifest("governance.query_population_metrics"),
        arguments=canonical_arguments,
        auth_context=auth,
    )


@pytest.mark.asyncio
async def test_non_authorization_semantic_failure_does_not_pollute_ledger() -> None:
    registry = ToolRegistry.default(housing_next_area_enabled=False)
    ledger = InMemoryDenialLedger()
    stack = build_semantic_capability_stack(
        registry=registry,
        adapter=InMemoryGovernanceAdapter(),
        denial_ledger=ledger,
    )
    auth = population_auth_context()
    missing_filter = _semantic_arguments()
    assert isinstance(missing_filter["spec"], dict)
    missing_filter["spec"]["filters"] = []

    result = await stack.executor.execute(
        tool_call_id="tcl-semantic-invalid",
        tool_id=SEMANTIC_QUERY_TOOL_ID,
        raw_arguments=missing_filter,
        auth_context=auth,
    )
    valid_arguments = QueryPopulationMetricsInput.model_validate(
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

    assert result.status == "failed"
    assert result.warnings == ["REQUIRED_FILTER_MISSING"]
    assert not await ledger.contains(
        manifest=registry.get_manifest("governance.query_population_metrics"),
        arguments=valid_arguments,
        auth_context=auth,
    )
