from __future__ import annotations

import pytest
from pydantic import ValidationError

from full_view_agent.application.capability_management_service import (
    CapabilityManagementService,
)
from full_view_agent.application.errors import RunStateConflict
from full_view_agent.domain.capability import SkillCapability, WorkflowCapability
from full_view_agent.infrastructure.capability_repository import (
    InMemoryCapabilityRepository,
)


def test_management_transition_body_cannot_self_approve() -> None:
    from full_view_agent.api.capability_routes import StatusTransitionBody

    with pytest.raises(ValidationError):
        StatusTransitionBody(to_status="pending_approval")  # type: ignore[arg-type]


def test_tool_create_requires_an_explicit_dataset_grant() -> None:
    from full_view_agent.api.capability_routes import ToolCreateBody

    with pytest.raises(ValidationError):
        ToolCreateBody(
            capability_id="demo.population",
            name="人口演示能力",
            owner="platform",
            version="1.0.0",
            connector_ref="demo.connector",
            resource_path="/population",
            dataset_ids=[],
        )


@pytest.mark.parametrize(
    "body_type,payload",
    [
        ("lifecycle", {"expected_etag": 1, "reason": ""}),
        ("application", {"expected_etag": 1, "reason": "   "}),
        (
            "rollback",
            {"to_version": "1.0.0", "expected_etag": 1, "reason": ""},
        ),
    ],
)
def test_lifecycle_audit_reason_is_required(body_type: str, payload: dict[str, object]) -> None:
    from full_view_agent.api.capability_routes import (
        ApplicationLifecycleBody,
        LifecycleActionBody,
        RollbackBody,
    )

    body_class = {
        "lifecycle": LifecycleActionBody,
        "application": ApplicationLifecycleBody,
        "rollback": RollbackBody,
    }[body_type]
    with pytest.raises(ValidationError):
        body_class.model_validate(payload)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["skill", "workflow"])
async def test_skill_and_workflow_testing_actions_are_real_lifecycle_transitions(
    kind: str,
) -> None:
    repository = InMemoryCapabilityRepository()
    service = CapabilityManagementService(repository)
    if kind == "skill":
        capability = SkillCapability(
            capability_id="governance.area_analysis",
            name="区域研判",
            owner="platform",
            version="1.0.0",
            allowed_tool_ids=["governance.resolve_area"],
        )
        await repository.save_skill(capability)
    else:
        capability = WorkflowCapability(
            capability_id="governance.area_analysis_workflow",
            name="区域研判流程",
            owner="platform",
            version="1.0.0",
        )
        await repository.save_workflow(capability)

    testing = await service.mark_testing(
        capability_id=capability.capability_id,
        version=capability.version,
        expected_etag=capability.etag,
        changed_by="tester",
        reason="contract tests passed",
    )

    assert testing.status == "testing"
    assert testing.etag == 2
    events = await service.list_lifecycle_events(capability.capability_id)
    assert events[-1].changed_by == "tester"
    assert events[-1].reason == "contract tests passed"


@pytest.mark.asyncio
async def test_capability_lifecycle_rejects_stale_etag() -> None:
    repository = InMemoryCapabilityRepository()
    service = CapabilityManagementService(repository)
    skill = SkillCapability(
        capability_id="governance.area_analysis",
        name="区域研判",
        owner="platform",
        version="1.0.0",
        allowed_tool_ids=["governance.resolve_area"],
    )
    await repository.save_skill(skill)
    await service.mark_testing(
        capability_id=skill.capability_id,
        version=skill.version,
        expected_etag=skill.etag,
        changed_by="tester",
    )

    with pytest.raises(RunStateConflict, match="etag"):
        await service.advance_status(
            capability_id=skill.capability_id,
            version=skill.version,
            to_status="pending_approval",
            expected_etag=skill.etag,
            changed_by="reviewer",
        )


@pytest.mark.asyncio
async def test_rollback_cannot_publish_a_never_approved_draft() -> None:
    repository = InMemoryCapabilityRepository()
    service = CapabilityManagementService(repository)
    draft = SkillCapability(
        capability_id="governance.area_analysis",
        name="区域研判",
        owner="platform",
        version="1.0.0",
        allowed_tool_ids=["governance.resolve_area"],
    )
    await repository.save_skill(draft)

    with pytest.raises(RunStateConflict, match="previously published"):
        await service.rollback(
            capability_id=draft.capability_id,
            to_version=draft.version,
            expected_etag=draft.etag,
            changed_by="publisher",
            reason="must not bypass approval",
        )


def test_tool_skill_workflow_expose_consistent_lifecycle_routes() -> None:
    from full_view_agent.api.app import RuntimeContainer, create_app

    app = create_app(RuntimeContainer())
    routes = list(app.routes)
    for route in app.routes:
        original_router = getattr(route, "original_router", None)
        if original_router is not None:
            routes.extend(original_router.routes)
    paths = {
        path
        for route in routes
        if isinstance((path := getattr(route, "path", None)), str)
    }
    for collection in ("tools", "skills", "workflows"):
        assert f"/capability-api/v1/{collection}/{{capability_id}}/{{version}}/testing" in paths
        assert f"/capability-api/v1/{collection}/{{capability_id}}/{{version}}/approve" in paths
        assert f"/capability-api/v1/{collection}/{{capability_id}}/{{version}}/publish" in paths
        assert f"/capability-api/v1/{collection}/{{capability_id}}/{{version}}/disable" in paths
        assert f"/capability-api/v1/{collection}/{{capability_id}}/rollback" in paths


def test_skill_and_workflow_expose_version_definition_and_dry_run_routes() -> None:
    from full_view_agent.api.app import RuntimeContainer, create_app

    app = create_app(RuntimeContainer())
    routes = list(app.routes)
    for route in app.routes:
        original_router = getattr(route, "original_router", None)
        if original_router is not None:
            routes.extend(original_router.routes)
    paths = {
        path
        for route in routes
        if isinstance((path := getattr(route, "path", None)), str)
    }
    for collection in ("skills", "workflows"):
        version_path = (
            f"/capability-api/v1/{collection}/{{capability_id}}/{{version}}"
        )
        assert version_path in paths
        assert f"{version_path}/dry-run" in paths
