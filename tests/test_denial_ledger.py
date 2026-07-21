import pytest

from full_view_agent.application.policy import MinimalPolicyAdapter
from full_view_agent.application.tool_registry import ToolRegistry
from full_view_agent.domain import models
from full_view_agent.infrastructure.denial_ledger import InMemoryDenialLedger

from .test_policy import population_auth_context


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_id", "dataset_id", "entitlement", "input_model"),
    [
        (
            "governance.query_housing_metrics",
            "housing",
            "governance.housing.aggregate.read",
            models.QueryHousingMetricsInput,
        ),
        (
            "governance.query_event_metrics",
            "event",
            "governance.event.aggregate.read",
            models.QueryEventMetricsInput,
        ),
    ],
)
async def test_denial_ledger_separates_metric_queries_by_area(
    tool_id: str,
    dataset_id: str,
    entitlement: str,
    input_model: type[models.ContractModel],
) -> None:
    base_context = population_auth_context()
    auth_context = base_context.model_copy(
        update={
            "entitlements": [entitlement],
            "data_scopes": base_context.data_scopes.model_copy(
                update={"datasets": [dataset_id]}
            ),
        }
    )
    manifest = ToolRegistry.default().get_manifest(tool_id)
    denied_arguments = input_model.model_validate(
        {"query": {"scope": {"area_code": "330108"}}}
    )
    authorized_arguments = input_model.model_validate(
        {"query": {"scope": {"area_code": "330106"}}}
    )
    decision = MinimalPolicyAdapter().evaluate(
        manifest=manifest,
        auth_context=auth_context,
        arguments=denied_arguments,
    )
    ledger = InMemoryDenialLedger()

    await ledger.record(
        manifest=manifest,
        arguments=denied_arguments,
        auth_context=auth_context,
        decision=decision,
    )

    assert await ledger.contains(
        manifest=manifest,
        arguments=denied_arguments,
        auth_context=auth_context,
    )
    assert not await ledger.contains(
        manifest=manifest,
        arguments=authorized_arguments,
        auth_context=auth_context,
    )
