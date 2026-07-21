from datetime import UTC, datetime

import pytest

from full_view_agent.application.tool_registry import ToolRegistry
from full_view_agent.domain import models


def population_auth_context() -> models.AuthContext:
    return models.AuthContext.model_validate(
        {
            "auth_context_id": "authctx-01",
            "auth_context_fingerprint": "sha256:auth-context-01",
            "principal": {
                "tenant_id": "tenant-hz",
                "user_id": "user-01",
                "org_id": "org-01",
                "roles": ["governance_analyst"],
            },
            "application": {
                "app_id": "full_information_view",
                "agent_id": "governance_general_agent",
            },
            "entitlements": ["governance.population.aggregate.read"],
            "data_scopes": {
                "areas": [
                    {"area_code": "330106", "include_descendants": True}
                ],
                "datasets": ["population"],
                "field_policy_set": "governance_analyst_v1",
            },
            "purpose": "interactive_analysis",
            "session_id": "session-01",
            "run_id": "run-01",
            "credential_ref": "cred-01",
            "issued_at": datetime(2099, 1, 1, tzinfo=UTC),
            "expires_at": datetime(2099, 1, 1, 0, 5, tzinfo=UTC),
            "policy_version": "test-v1",
        }
    )


def test_policy_denies_population_query_outside_authorized_area() -> None:
    from full_view_agent.application.policy import MinimalPolicyAdapter

    policy = MinimalPolicyAdapter()
    manifest = ToolRegistry.default().get_manifest(
        "governance.query_population_metrics"
    )
    arguments = models.QueryPopulationMetricsInput.model_validate(
        {
            "query": {
                "metrics": ["person_count"],
                "scope": {"area_code": "330108"},
            }
        }
    )

    decision = policy.evaluate(
        manifest=manifest,
        auth_context=population_auth_context(),
        arguments=arguments,
    )

    assert decision.decision == "deny"
    assert decision.reason_codes == ["AREA_OUT_OF_SCOPE"]
    assert decision.tool_id == manifest.tool_id
    assert decision.auth_context_fingerprint == "sha256:auth-context-01"
    assert decision.arguments_fingerprint.startswith("sha256:")


@pytest.mark.parametrize(
    ("tool_id", "dataset_id", "entitlement", "arguments"),
    [
        (
            "governance.query_housing_metrics",
            "housing",
            "governance.housing.aggregate.read",
            models.QueryHousingMetricsInput.model_validate(
                {"query": {"scope": {"area_code": "330108"}}}
            ),
        ),
        (
            "governance.query_event_metrics",
            "event",
            "governance.event.aggregate.read",
            models.QueryEventMetricsInput.model_validate(
                {"query": {"scope": {"area_code": "330108"}}}
            ),
        ),
    ],
)
def test_policy_denies_every_metric_query_outside_authorized_area(
    tool_id: str,
    dataset_id: str,
    entitlement: str,
    arguments: models.ContractModel,
) -> None:
    from full_view_agent.application.policy import MinimalPolicyAdapter

    base_context = population_auth_context()
    auth_context = base_context.model_copy(
        update={
            "entitlements": [entitlement],
            "data_scopes": base_context.data_scopes.model_copy(
                update={"datasets": [dataset_id]}
            ),
        }
    )

    decision = MinimalPolicyAdapter().evaluate(
        manifest=ToolRegistry.default().get_manifest(tool_id),
        auth_context=auth_context,
        arguments=arguments,
    )

    assert decision.decision == "deny"
    assert decision.reason_codes == ["AREA_OUT_OF_SCOPE"]


def test_policy_denies_object_query_whose_declared_scope_is_unauthorized() -> None:
    from full_view_agent.application.policy import MinimalPolicyAdapter

    base_context = population_auth_context()
    auth_context = base_context.model_copy(
        update={
            "entitlements": ["governance.object.profile.read"],
            "data_scopes": base_context.data_scopes.model_copy(
                update={"datasets": ["governance_objects"]}
            ),
        }
    )
    arguments = models.GetObjectProfileInput.model_validate(
        {
            "object_ref": {
                "object_type": "building",
                "object_id": "330108001001001000001",
            },
            "scope": {"area_code": "330108"},
            "field_sets": ["summary"],
        }
    )

    decision = MinimalPolicyAdapter().evaluate(
        manifest=ToolRegistry.default().get_manifest(
            "governance.get_object_profile"
        ),
        auth_context=auth_context,
        arguments=arguments,
    )

    assert decision.decision == "deny"
    assert decision.reason_codes == ["AREA_OUT_OF_SCOPE"]


def test_policy_denies_object_result_outside_declared_request_scope() -> None:
    from full_view_agent.application.policy import MinimalPolicyAdapter

    base_context = population_auth_context()
    auth_context = base_context.model_copy(
        update={
            "entitlements": ["governance.object.profile.read"],
            "data_scopes": models.AuthDataScopes(
                areas=[
                    models.AuthorizedAreaScope(
                        area_code="330106",
                        include_descendants=True,
                    ),
                    models.AuthorizedAreaScope(
                        area_code="330108",
                        include_descendants=True,
                    ),
                ],
                datasets=["governance_objects"],
                field_policy_set="governance_analyst_v1",
            ),
        }
    )
    arguments = models.GetObjectProfileInput.model_validate(
        {
            "object_ref": {
                "object_type": "building",
                "object_id": "330108001001001000001",
            },
            "scope": {"area_code": "330106"},
            "field_sets": ["summary"],
        }
    )
    result = models.ObjectProfileResult.model_validate(
        {
            "result_id": "res-object-01",
            "data_schema_ref": "schema://data/object-profile/1.0.0",
            "result_fingerprint": "sha256:object-01",
            "data": {
                "object_ref": arguments.object_ref.model_dump(mode="json"),
                "area_code": "330108001",
                "title": "测试楼栋",
                "fields": [],
            },
        }
    )

    decision = MinimalPolicyAdapter().evaluate_post_result(
        manifest=ToolRegistry.default().get_manifest(
            "governance.get_object_profile"
        ),
        auth_context=auth_context,
        arguments=arguments,
        result=result,
    )

    assert decision.decision == "deny"
    assert decision.reason_codes == ["RESULT_AREA_OUTSIDE_REQUEST_SCOPE"]


def test_policy_masks_contact_field_set_without_contact_entitlement() -> None:
    from full_view_agent.application.policy import MinimalPolicyAdapter

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
    arguments = models.GetObjectProfileInput.model_validate(
        {
            "object_ref": {
                "object_type": "person",
                "object_id": "person-01",
            },
            "scope": {"area_code": "330106"},
            "field_sets": ["summary", "contact"],
        }
    )
    manifest = ToolRegistry.default().get_manifest(
        "governance.get_object_profile"
    )

    decision = MinimalPolicyAdapter().evaluate(
        manifest=manifest,
        auth_context=auth_context,
        arguments=arguments,
    )

    assert decision.decision == "mask"
    assert decision.reason_codes == ["FIELD_RESTRICTED"]
    assert decision.effective_scope.allowed_field_sets == ["summary"]
    assert decision.effective_scope.denied_field_sets == ["contact"]
