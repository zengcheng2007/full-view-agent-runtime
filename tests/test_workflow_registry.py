import pytest
from pydantic import SecretStr

from full_view_agent.application.errors import WorkflowNotAvailable
from full_view_agent.application.workflow_registry import WorkflowRegistry
from full_view_agent.domain.models import Principal, WorkflowDefinition, WorkflowRef
from full_view_agent.infrastructure.legacy_identity import HashedLegacyIdentityAdapter


async def _identity():
    return await HashedLegacyIdentityAdapter().resolve(SecretStr("workflow-user"))


@pytest.mark.asyncio
async def test_workflow_registry_returns_active_authorized_definition() -> None:
    definition = WorkflowDefinition(
        workflow_id="area_governance_brief",
        workflow_version="1.0.0",
        required_roles=["governance_analyst"],
        executor_ref="workflow://governance/area-brief/1.0.0",
    )
    registry = WorkflowRegistry([definition])

    available = registry.require_available(
        workflow_ref=WorkflowRef(
            workflow_id=definition.workflow_id,
            workflow_version=definition.workflow_version,
        ),
        identity=await _identity(),
    )

    assert available == definition


@pytest.mark.asyncio
async def test_workflow_registry_hides_disabled_or_unauthorized_definition() -> None:
    active = WorkflowDefinition(
        workflow_id="area_governance_brief",
        workflow_version="1.0.0",
        required_roles=["governance_analyst"],
        executor_ref="workflow://governance/area-brief/1.0.0",
    )
    disabled = active.model_copy(
        update={"workflow_version": "0.9.0", "status": "disabled"}
    )
    registry = WorkflowRegistry([active, disabled])
    identity = await _identity()
    unauthorized = identity.model_copy(
        update={
            "principal": Principal(
                tenant_id=identity.principal.tenant_id,
                user_id=identity.principal.user_id,
                org_id=identity.principal.org_id,
                roles=[],
            )
        }
    )

    with pytest.raises(WorkflowNotAvailable):
        registry.require_available(
            workflow_ref=WorkflowRef(
                workflow_id=disabled.workflow_id,
                workflow_version=disabled.workflow_version,
            ),
            identity=identity,
        )
    with pytest.raises(WorkflowNotAvailable):
        registry.require_available(
            workflow_ref=WorkflowRef(
                workflow_id=active.workflow_id,
                workflow_version=active.workflow_version,
            ),
            identity=unauthorized,
        )
