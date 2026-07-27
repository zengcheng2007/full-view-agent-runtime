import pytest

from full_view_agent.application import errors
from full_view_agent.application.policy import MinimalPolicyAdapter
from full_view_agent.application.tool_registry import ToolRegistry

from .test_policy import population_auth_context


class RecordingAdapter:
    def __init__(self) -> None:
        self.calls = 0

    async def execute(self, **_kwargs):
        self.calls += 1
        raise AssertionError("denied capability must not call its adapter")


class MismatchedPolicy:
    def evaluate(self, **kwargs):
        decision = MinimalPolicyAdapter().evaluate(**kwargs)
        return decision.model_copy(
            update={"arguments_fingerprint": "sha256:mismatched"}
        )


class StaticAuthContextRefresher:
    def __init__(self, auth_context) -> None:
        self.auth_context = auth_context
        self.calls = 0

    async def refresh(self, _auth_context):
        self.calls += 1
        return self.auth_context


class CountingPolicy(MinimalPolicyAdapter):
    def __init__(self) -> None:
        self.calls = 0

    def evaluate(self, **kwargs):
        self.calls += 1
        return super().evaluate(**kwargs)


class TimeoutAdapter:
    async def execute(self, **_kwargs):
        raise errors.UpstreamTimeout("legacy request timed out")


class SemanticFailureAdapter:
    async def execute(self, **_kwargs):
        raise errors.SemanticValidationError(
            "filters.value must be solitary_elderly and group_by must be street"
        )


@pytest.mark.asyncio
async def test_capability_returns_recoverable_failure_for_invalid_model_arguments() -> None:
    from full_view_agent.application.capability_service import CapabilityService

    adapter = RecordingAdapter()
    capability = CapabilityService(
        registry=ToolRegistry.default(),
        policy=MinimalPolicyAdapter(),
        adapter=adapter,
    )

    result = await capability.execute(
        tool_call_id="tcl-invalid-arguments-01",
        tool_id="governance.query_population_metrics",
        raw_arguments={"query": "not-json"},
        auth_context=population_auth_context(),
    )

    assert result.status == "failed"
    assert result.warnings == ["TOOL_ARGUMENT_VALIDATION_FAILED"]
    assert "query" in result.summary
    assert "JSON 对象" in result.summary
    assert adapter.calls == 0


@pytest.mark.asyncio
async def test_capability_normalizes_stringified_json_for_typed_object_field() -> None:
    from full_view_agent.application.capability_service import CapabilityService
    from full_view_agent.infrastructure.governance_adapter import (
        InMemoryGovernanceAdapter,
    )

    capability = CapabilityService(
        registry=ToolRegistry.default(),
        policy=MinimalPolicyAdapter(),
        adapter=InMemoryGovernanceAdapter(),
    )

    result = await capability.execute(
        tool_call_id="tcl-stringified-object-01",
        tool_id="governance.query_population_metrics",
        raw_arguments={
            "query": '{"metrics":["person_count"],"scope":{"area_code":"330106"}}'
        },
        auth_context=population_auth_context(),
    )

    assert result.status == "success"
    assert result.data_result.data.rows[0].person_count == 128


@pytest.mark.asyncio
async def test_capability_normalizes_upstream_failure_as_tool_result() -> None:
    from full_view_agent.application.capability_service import CapabilityService

    capability = CapabilityService(
        registry=ToolRegistry.default(),
        policy=MinimalPolicyAdapter(),
        adapter=TimeoutAdapter(),
    )

    result = await capability.execute(
        tool_call_id="tcl-timeout-01",
        tool_id="governance.query_population_metrics",
        raw_arguments={
            "query": {
                "metrics": ["person_count"],
                "scope": {"area_code": "330106"},
            }
        },
        auth_context=population_auth_context(),
    )

    assert result.status == "failed"
    assert result.summary == "现有业务服务暂时不可用。"
    assert result.warnings == ["upstream_timeout"]


@pytest.mark.asyncio
async def test_capability_returns_actionable_semantic_failure_to_model() -> None:
    from full_view_agent.application.capability_service import CapabilityService

    capability = CapabilityService(
        registry=ToolRegistry.default(),
        policy=MinimalPolicyAdapter(),
        adapter=SemanticFailureAdapter(),
    )

    result = await capability.execute(
        tool_call_id="tcl-semantic-failure-01",
        tool_id="governance.query_population_metrics",
        raw_arguments={
            "query": {
                "metrics": ["person_count"],
                "scope": {"area_code": "330106"},
            }
        },
        auth_context=population_auth_context(),
    )

    assert result.status == "failed"
    assert "solitary_elderly" in result.summary
    assert "group_by" in result.summary
    assert result.warnings == ["semantic_validation_error"]


@pytest.mark.asyncio
async def test_capability_denial_does_not_call_downstream_adapter() -> None:
    from full_view_agent.application.capability_service import CapabilityService
    from full_view_agent.infrastructure.denial_ledger import InMemoryDenialLedger

    adapter = RecordingAdapter()
    policy = CountingPolicy()
    capability = CapabilityService(
        registry=ToolRegistry.default(),
        policy=policy,
        adapter=adapter,
        denial_ledger=InMemoryDenialLedger(),
    )

    result = await capability.execute(
        tool_call_id="tcl-denied-01",
        tool_id="governance.query_population_metrics",
        raw_arguments={
            "query": {
                "metrics": ["person_count"],
                "scope": {"area_code": "330108"},
            }
        },
        auth_context=population_auth_context(),
    )

    assert result.status == "denied"
    assert result.data_result is None
    assert result.warnings == ["AREA_OUT_OF_SCOPE"]
    assert adapter.calls == 0

    repeated = await capability.execute(
        tool_call_id="tcl-denied-02",
        tool_id="governance.query_population_metrics",
        raw_arguments={
            "query": {
                "metrics": ["person_count"],
                "scope": {"area_code": "330108"},
            }
        },
        auth_context=population_auth_context(),
    )

    assert repeated.status == "denied"
    assert repeated.warnings == ["DENIAL_LEDGER_MATCH"]
    assert policy.calls == 1
    assert adapter.calls == 0


@pytest.mark.asyncio
async def test_capability_rejects_policy_decision_with_mismatched_binding() -> None:
    from full_view_agent.application.capability_service import CapabilityService

    adapter = RecordingAdapter()
    capability = CapabilityService(
        registry=ToolRegistry.default(),
        policy=MismatchedPolicy(),
        adapter=adapter,
    )

    with pytest.raises(errors.PolicyBindingMismatch):
        await capability.execute(
            tool_call_id="tcl-binding-01",
            tool_id="governance.query_population_metrics",
            raw_arguments={
                "query": {
                    "metrics": ["person_count"],
                    "scope": {"area_code": "330106"},
                }
            },
            auth_context=population_auth_context(),
        )

    assert adapter.calls == 0


@pytest.mark.asyncio
async def test_resolve_area_returns_only_authorized_candidates() -> None:
    from full_view_agent.application.capability_service import CapabilityService
    from full_view_agent.infrastructure.governance_adapter import (
        InMemoryGovernanceAdapter,
    )

    base_context = population_auth_context()
    auth_context = base_context.model_copy(
        update={
            "entitlements": ["governance.area.read"],
            "data_scopes": base_context.data_scopes.model_copy(
                update={"datasets": ["administrative_area"]}
            ),
        }
    )
    capability = CapabilityService(
        registry=ToolRegistry.default(),
        policy=MinimalPolicyAdapter(),
        adapter=InMemoryGovernanceAdapter(),
    )

    result = await capability.execute(
        tool_call_id="tcl-area-01",
        tool_id="governance.resolve_area",
        raw_arguments={"query": "西湖区"},
        auth_context=auth_context,
    )

    assert result.status == "success"
    assert result.data_result.kind == "area_candidates"
    assert result.data_result.data.resolved_area_code == "330106"
    assert [
        candidate.area_code for candidate in result.data_result.data.candidates
    ] == ["330106"]


@pytest.mark.asyncio
async def test_resolve_area_returns_authorized_street_candidate() -> None:
    from full_view_agent.application.capability_service import CapabilityService
    from full_view_agent.infrastructure.governance_adapter import (
        InMemoryGovernanceAdapter,
    )

    base_context = population_auth_context()
    auth_context = base_context.model_copy(
        update={
            "entitlements": ["governance.area.read"],
            "data_scopes": base_context.data_scopes.model_copy(
                update={"datasets": ["administrative_area"]}
            ),
        }
    )
    result = await CapabilityService(
        registry=ToolRegistry.default(),
        policy=MinimalPolicyAdapter(),
        adapter=InMemoryGovernanceAdapter(),
    ).execute(
        tool_call_id="tcl-cuiyuan-01",
        tool_id="governance.resolve_area",
        raw_arguments={"query": "翠苑"},
        auth_context=auth_context,
    )

    assert result.status == "success"
    assert result.data_result.data.resolved_area_code == "330106001"
    assert [
        candidate.area_code for candidate in result.data_result.data.candidates
    ] == ["330106001"]


@pytest.mark.asyncio
async def test_population_metrics_adapter_returns_typed_aggregate_table() -> None:
    from full_view_agent.application.capability_service import CapabilityService
    from full_view_agent.infrastructure.governance_adapter import (
        InMemoryGovernanceAdapter,
    )

    capability = CapabilityService(
        registry=ToolRegistry.default(),
        policy=MinimalPolicyAdapter(),
        adapter=InMemoryGovernanceAdapter(),
    )

    result = await capability.execute(
        tool_call_id="tcl-population-01",
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
        auth_context=population_auth_context(),
    )

    assert result.status == "success"
    assert result.data_result.kind == "table"
    assert result.data_result.data.rows[0].person_count == 128
    assert result.data_result.data_schema_ref == (
        "schema://data/population-metric-table/1.0.0"
    )
    assert result.policy.decision == "allow"
    assert result.policy.auth_context_fingerprint == "sha256:auth-context-01"
    assert result.policy.arguments_fingerprint.startswith("sha256:")


@pytest.mark.asyncio
async def test_object_profile_adapter_applies_policy_field_mask() -> None:
    from full_view_agent.application.capability_service import CapabilityService
    from full_view_agent.domain import models
    from full_view_agent.infrastructure.governance_adapter import (
        InMemoryGovernanceAdapter,
    )

    base_context = population_auth_context()
    auth_context = base_context.model_copy(
        update={
            "entitlements": [
                "governance.object.profile.read",
                "governance.object.contact.read",
            ],
            "data_scopes": models.AuthDataScopes(
                areas=[
                    models.AuthorizedAreaScope(
                        area_code="330106",
                        include_descendants=True,
                    )
                ],
                datasets=["governance_objects"],
                field_policy_set="governance_analyst_v1",
            ),
        }
    )
    refreshed_context = auth_context.model_copy(
        update={
            "auth_context_id": "authctx-refreshed-01",
            "auth_context_fingerprint": "sha256:auth-context-refreshed-01",
            "entitlements": ["governance.object.profile.read"],
        }
    )
    refresher = StaticAuthContextRefresher(refreshed_context)
    capability = CapabilityService(
        registry=ToolRegistry.default(),
        policy=MinimalPolicyAdapter(),
        adapter=InMemoryGovernanceAdapter(),
        auth_context_refresher=refresher,
    )

    result = await capability.execute(
        tool_call_id="tcl-profile-01",
        tool_id="governance.get_object_profile",
        raw_arguments={
            "object_ref": {
                "object_type": "person",
                "object_id": "person-01",
            },
            "scope": {"area_code": "330106"},
            "field_sets": ["summary", "contact"],
        },
        auth_context=auth_context,
    )

    assert result.status == "success"
    assert result.data_result.kind == "object_profile"
    assert result.data_result.data.area_code == "330106001"
    assert result.warnings == ["FIELD_RESTRICTED"]
    assert [field.field_id for field in result.data_result.data.fields] == [
        "display_name",
        "object_type",
    ]
    assert refresher.calls == 1
    assert result.policy.pre_policy_decision_id is not None
    assert result.policy.post_policy_decision_id is not None
    assert (
        result.policy.auth_context_fingerprint
        == refreshed_context.auth_context_fingerprint
    )


@pytest.mark.asyncio
async def test_object_profile_post_policy_denies_actual_object_outside_area() -> None:
    from full_view_agent.application.capability_service import CapabilityService
    from full_view_agent.domain import models

    class OutsideAreaAdapter:
        async def execute(self, **_kwargs):
            data = models.ObjectProfileData(
                object_ref=models.GovernanceObjectRef(
                    object_type="person",
                    object_id="person-outside",
                ),
                area_code="330108001",
                title="越权对象",
                fields=[],
            )
            return models.ObjectProfileResult(
                result_id="res-outside-area",
                data_schema_ref="schema://data/object-profile/1.0.0",
                result_fingerprint="sha256:outside-area",
                data=data,
            )

    base_context = population_auth_context()
    auth_context = base_context.model_copy(
        update={
            "entitlements": ["governance.object.profile.read"],
            "data_scopes": models.AuthDataScopes(
                areas=[
                    models.AuthorizedAreaScope(
                        area_code="330106",
                        include_descendants=True,
                    )
                ],
                datasets=["governance_objects"],
                field_policy_set="governance_analyst_v1",
            ),
        }
    )
    capability = CapabilityService(
        registry=ToolRegistry.default(),
        policy=MinimalPolicyAdapter(),
        adapter=OutsideAreaAdapter(),
        auth_context_refresher=StaticAuthContextRefresher(auth_context),
    )

    result = await capability.execute(
        tool_call_id="tcl-profile-outside-01",
        tool_id="governance.get_object_profile",
        raw_arguments={
            "object_ref": {
                "object_type": "person",
                "object_id": "person-outside",
            },
            "scope": {"area_code": "330106"},
            "field_sets": ["summary"],
        },
        auth_context=auth_context,
    )

    assert result.status == "denied"
    assert result.data_result is None
    assert result.warnings == ["RESULT_AREA_OUTSIDE_REQUEST_SCOPE"]
    assert result.policy.post_policy_decision_id is not None
