"""Tests for P2-1 capability center."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from full_view_agent.application.capability_management_service import (
    CapabilityManagementService,
)
from full_view_agent.application.errors import ResourceNotFound, RunStateConflict
from full_view_agent.application.session_run_service import new_id
from full_view_agent.domain.capability import (
    CapabilityLifecycleEvent,
    CapabilitySnapshot,
    Connector,
    SkillCapability,
    ToolCapability,
    WorkflowNodeDefinition,
    is_valid_transition,
)
from full_view_agent.infrastructure.capability_repository import (
    InMemoryCapabilityRepository,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _connector(
    *,
    connector_id: str = "conn.governance",
    name: str = "Governance API",
    base_url: str = "https://gov.api.local",
    allowed_path_prefixes: list[str] | None = None,
) -> Connector:
    return Connector(
        connector_id=connector_id,
        name=name,
        base_url=base_url,
        allowed_path_prefixes=allowed_path_prefixes or ["/v1/"],
    )


def _tool_kwargs(**overrides: Any) -> dict[str, Any]:
    defaults: dict[str, Any] = {
        "capability_id": "tool.resolve_area",
        "name": "Resolve Area",
        "owner": "team-governance",
        "version": "1.0.0",
        "connector_ref": "conn.governance",
        "resource_path": "/v1/areas",
    }
    defaults.update(overrides)
    return defaults


async def _seed_connector(
    repo: InMemoryCapabilityRepository,
    *,
    connector_id: str = "conn.governance",
    allowed_path_prefixes: list[str] | None = None,
) -> None:
    await repo.save_connector(
        _connector(
            connector_id=connector_id,
            allowed_path_prefixes=allowed_path_prefixes,
        )
    )


# ===================================================================
# 1. Domain validation
# ===================================================================


class TestDomainValidation:
    """Test capability domain model validation rules."""

    async def test_capability_id_format_validation(self) -> None:
        """Valid IDs follow dotted-lowercase pattern; invalid ones are rejected."""
        # Valid: dotted lowercase, at least one dot
        valid_ids = [
            "tool.resolve_area",
            "skill.data_analysis",
            "workflow.case-review",
            "tool.my-tool_v2",
        ]
        for cid in valid_ids:
            tool = ToolCapability(**_tool_kwargs(capability_id=cid))
            assert tool.capability_id == cid

        # Invalid: uppercase, no dot, leading dot, etc.
        invalid_ids = [
            "NoDot",
            ".leading_dot",
            "UPPER.case",
            "has spaces.case",
            "123.starts_with_digit",
        ]
        for cid in invalid_ids:
            with pytest.raises(ValidationError):
                ToolCapability(**_tool_kwargs(capability_id=cid))

    async def test_version_must_be_semver(self) -> None:
        """Version must match semver pattern N.N.N."""
        # Valid
        for ver in ["1.0.0", "0.1.0", "2.3.4"]:
            tool = ToolCapability(**_tool_kwargs(version=ver))
            assert tool.version == ver

        # Invalid
        for ver in ["1.0", "v1.0.0", "1", "1.0.0.0", "abc"]:
            with pytest.raises(ValidationError):
                ToolCapability(**_tool_kwargs(version=ver))

    async def test_resource_path_must_start_with_slash(self) -> None:
        """resource_path must start with '/' for SSRF protection."""
        # Valid
        tool = ToolCapability(**_tool_kwargs(resource_path="/v1/areas"))
        assert tool.resource_path == "/v1/areas"

        # Invalid
        for path in ["v1/areas", "areas", "", "http://evil.com"]:
            with pytest.raises(ValidationError):
                ToolCapability(**_tool_kwargs(resource_path=path))

    async def test_resource_path_rejects_traversal(self) -> None:
        """resource_path must not contain '..' or '//' to prevent path traversal."""
        # Valid
        tool = ToolCapability(**_tool_kwargs(resource_path="/v1/areas/detail"))
        assert tool.resource_path == "/v1/areas/detail"

        # Invalid - path traversal
        for path in ["/v1/../admin", "/v1//admin", "/../etc/passwd", "/v1//"]:
            with pytest.raises(ValidationError):
                ToolCapability(**_tool_kwargs(resource_path=path))

    async def test_published_skill_requires_allowed_tools(self) -> None:
        """Published skills must declare at least one allowed tool."""
        # Valid: published with tools
        skill = SkillCapability(
            capability_id="skill.data_analysis",
            name="Data Analysis",
            owner="team",
            version="1.0.0",
            status="published",
            allowed_tool_ids=["tool.query"],
        )
        assert skill.status == "published"

        # Invalid: published without tools
        with pytest.raises(ValidationError):
            SkillCapability(
                capability_id="skill.empty",
                name="Empty Skill",
                owner="team",
                version="1.0.0",
                status="published",
                allowed_tool_ids=[],
            )

        # Valid: draft without tools (not yet published)
        skill_draft = SkillCapability(
            capability_id="skill.draft",
            name="Draft Skill",
            owner="team",
            version="1.0.0",
            status="draft",
            allowed_tool_ids=[],
        )
        assert skill_draft.status == "draft"


# ===================================================================
# 2. Lifecycle transitions
# ===================================================================


class TestLifecycleTransitions:
    """Test capability lifecycle state machine."""

    async def test_valid_lifecycle_transitions(self) -> None:
        """Test all valid lifecycle transitions."""
        # draft -> testing
        assert is_valid_transition("draft", "testing")

        # testing -> pending_approval or back to draft
        assert is_valid_transition("testing", "pending_approval")
        assert is_valid_transition("testing", "draft")

        # pending_approval -> published or back to testing
        assert is_valid_transition("pending_approval", "published")
        assert is_valid_transition("pending_approval", "testing")

        # published -> disabled
        assert is_valid_transition("published", "disabled")

        # disabled -> draft (can be redrafted)
        assert is_valid_transition("disabled", "draft")

    async def test_invalid_lifecycle_transitions_rejected(self) -> None:
        """Test that invalid transitions are rejected."""
        # Can't skip stages
        assert not is_valid_transition("draft", "published")
        assert not is_valid_transition("draft", "pending_approval")
        assert not is_valid_transition("draft", "disabled")

        # Can't go backwards arbitrarily
        assert not is_valid_transition("published", "draft")
        assert not is_valid_transition("published", "testing")
        assert not is_valid_transition("published", "pending_approval")

        # Can't stay in same state
        assert not is_valid_transition("draft", "draft")
        assert not is_valid_transition("published", "published")

        # Disabled can't go directly to published
        assert not is_valid_transition("disabled", "published")
        assert not is_valid_transition("disabled", "testing")

    async def test_published_cannot_be_modified(self) -> None:
        """Published capabilities cannot be modified directly."""
        repo = InMemoryCapabilityRepository()
        await _seed_connector(repo)
        svc = CapabilityManagementService(repo)

        tool = await svc.create_tool(**_tool_kwargs())
        tool = await svc.advance_status(
            capability_id=tool.capability_id,
            version=tool.version,
            to_status="testing",
            changed_by="tester",
        )
        tool = await svc.advance_status(
            capability_id=tool.capability_id,
            version=tool.version,
            to_status="pending_approval",
            changed_by="reviewer",
        )
        await svc.publish(
            capability_id=tool.capability_id,
            version=tool.version,
            published_by="publisher",
        )

        # Attempt to modify published tool
        with pytest.raises(RunStateConflict, match="cannot modify a published"):
            await svc.update_tool(
                capability_id=tool.capability_id,
                version=tool.version,
                updated_by="hacker",
                name="Hacked Name",
            )

    async def test_disabled_can_be_redrafted(self) -> None:
        """Disabled capabilities can transition back to draft."""
        repo = InMemoryCapabilityRepository()
        await _seed_connector(repo)
        svc = CapabilityManagementService(repo)

        tool = await svc.create_tool(**_tool_kwargs())
        tool = await svc.advance_status(
            capability_id=tool.capability_id,
            version=tool.version,
            to_status="testing",
            changed_by="tester",
        )
        tool = await svc.advance_status(
            capability_id=tool.capability_id,
            version=tool.version,
            to_status="pending_approval",
            changed_by="reviewer",
        )
        tool = await svc.advance_status(
            capability_id=tool.capability_id,
            version=tool.version,
            to_status="published",
            changed_by="publisher",
        )
        tool = await svc.disable(
            capability_id=tool.capability_id,
            version=tool.version,
            changed_by="admin",
            reason="decommissioned",
        )

        assert tool.status == "disabled"

        # Can transition back to draft
        tool = await svc.advance_status(
            capability_id=tool.capability_id,
            version=tool.version,
            to_status="draft",
            changed_by="developer",
            reason="redrafting",
        )
        assert tool.status == "draft"


# ===================================================================
# 3. Repository (InMemory)
# ===================================================================


class TestInMemoryRepository:
    """Test InMemoryCapabilityRepository operations."""

    async def test_save_and_get_tool(self) -> None:
        """Test saving and retrieving a tool."""
        repo = InMemoryCapabilityRepository()
        tool = ToolCapability(**_tool_kwargs())
        saved = await repo.save_tool(tool)
        assert saved.capability_id == tool.capability_id

        retrieved = await repo.get(tool.capability_id, tool.version)
        assert retrieved is not None
        assert isinstance(retrieved, ToolCapability)
        assert retrieved.capability_id == tool.capability_id
        assert retrieved.version == tool.version

    async def test_list_capabilities_by_type(self) -> None:
        """Test listing capabilities filtered by type."""
        repo = InMemoryCapabilityRepository()
        await _seed_connector(repo)

        # Create tool
        tool = ToolCapability(**_tool_kwargs())
        await repo.save_tool(tool)

        # Create skill
        skill = SkillCapability(
            capability_id="skill.analysis",
            name="Analysis Skill",
            owner="team",
            version="1.0.0",
            allowed_tool_ids=["tool.resolve_area"],
        )
        await repo.save_skill(skill)

        # List all
        all_caps = await repo.list_capabilities()
        assert len(all_caps) == 2

        # List tools only
        tools = await repo.list_capabilities(capability_type="tool")
        assert len(tools) == 1
        assert isinstance(tools[0], ToolCapability)

        # List skills only
        skills = await repo.list_capabilities(capability_type="skill")
        assert len(skills) == 1
        assert isinstance(skills[0], SkillCapability)

    async def test_list_capabilities_by_status(self) -> None:
        """Test listing capabilities filtered by status."""
        repo = InMemoryCapabilityRepository()
        await _seed_connector(repo)

        tool1 = ToolCapability(**_tool_kwargs(capability_id="tool.one"))
        await repo.save_tool(tool1)

        tool2 = ToolCapability(
            **_tool_kwargs(
                capability_id="tool.two",
                version="1.0.0",
                status="published",
            )
        )
        await repo.save_tool(tool2)

        # List all
        all_tools = await repo.list_capabilities()
        assert len(all_tools) == 2

        # List drafts
        drafts = await repo.list_capabilities(status="draft")
        assert len(drafts) == 1
        assert drafts[0].capability_id == "tool.one"

        # List published
        published = await repo.list_capabilities(status="published")
        assert len(published) == 1
        assert published[0].capability_id == "tool.two"

    async def test_concurrent_modification_detected(self) -> None:
        """Test that etag-based optimistic concurrency control works."""
        repo = InMemoryCapabilityRepository()
        tool = ToolCapability(**_tool_kwargs())
        await repo.save_tool(tool)

        # Simulate concurrent modification: etag mismatch
        stale = tool.model_copy(update={"etag": 999})
        with pytest.raises(RunStateConflict, match="concurrent modification"):
            await repo.save_tool(stale)

    async def test_snapshot_created_on_publish(self) -> None:
        """Test that publishing creates a snapshot."""
        repo = InMemoryCapabilityRepository()
        tool = ToolCapability(**_tool_kwargs())
        await repo.save_tool(tool)

        snapshot = CapabilitySnapshot(
            snapshot_id=new_id("snap"),
            capability_id=tool.capability_id,
            capability_type=tool.capability_type,
            version=tool.version,
            published_by="tester",
            content=tool.model_dump(mode="json"),
        )
        await repo.put_snapshot(snapshot)

        active = await repo.get_active_snapshot(tool.capability_id)
        assert active is not None
        assert active.version == tool.version
        assert active.is_active

    async def test_only_one_active_snapshot_per_capability(self) -> None:
        """Test that only one snapshot per capability can be active."""
        repo = InMemoryCapabilityRepository()
        tool = ToolCapability(**_tool_kwargs())
        await repo.save_tool(tool)

        # Create first snapshot
        snap1 = CapabilitySnapshot(
            snapshot_id=new_id("snap"),
            capability_id=tool.capability_id,
            capability_type=tool.capability_type,
            version="1.0.0",
            published_by="tester",
            content=tool.model_dump(mode="json"),
        )
        await repo.put_snapshot(snap1)

        # Create second snapshot
        snap2 = CapabilitySnapshot(
            snapshot_id=new_id("snap"),
            capability_id=tool.capability_id,
            capability_type=tool.capability_type,
            version="1.0.1",
            published_by="tester",
            content=tool.model_dump(mode="json"),
        )
        await repo.put_snapshot(snap2)

        # Only second should be active
        active = await repo.get_active_snapshot(tool.capability_id)
        assert active is not None
        assert active.version == "1.0.1"

    async def test_deactivate_snapshots(self) -> None:
        """Test deactivating all snapshots for a capability."""
        repo = InMemoryCapabilityRepository()
        tool = ToolCapability(**_tool_kwargs())
        await repo.save_tool(tool)

        snapshot = CapabilitySnapshot(
            snapshot_id=new_id("snap"),
            capability_id=tool.capability_id,
            capability_type=tool.capability_type,
            version=tool.version,
            published_by="tester",
            content=tool.model_dump(mode="json"),
        )
        await repo.put_snapshot(snapshot)

        # Verify active
        active = await repo.get_active_snapshot(tool.capability_id)
        assert active is not None

        # Deactivate
        await repo.deactivate_snapshots(tool.capability_id)

        # Verify no active snapshot
        active = await repo.get_active_snapshot(tool.capability_id)
        assert active is None

    async def test_lifecycle_events_recorded(self) -> None:
        """Test that lifecycle events are recorded and retrievable."""
        repo = InMemoryCapabilityRepository()
        event1 = CapabilityLifecycleEvent(
            event_id=new_id("cle"),
            capability_id="tool.test",
            from_status="draft",
            to_status="testing",
            version="1.0.0",
            changed_by="tester",
            reason="ready for testing",
        )
        event2 = CapabilityLifecycleEvent(
            event_id=new_id("cle"),
            capability_id="tool.test",
            from_status="testing",
            to_status="published",
            version="1.0.0",
            changed_by="publisher",
            reason="approved",
        )

        await repo.record_lifecycle_event(event1)
        await repo.record_lifecycle_event(event2)

        events = await repo.list_lifecycle_events("tool.test")
        assert len(events) == 2
        assert events[0].to_status == "testing"
        assert events[1].to_status == "published"

    async def test_connector_save_and_list(self) -> None:
        """Test saving and listing connectors."""
        repo = InMemoryCapabilityRepository()

        conn1 = _connector(connector_id="conn.one", name="API One")
        conn2 = _connector(connector_id="conn.two", name="API Two")
        conn2_inactive = conn2.model_copy(update={"is_active": False})

        await repo.save_connector(conn1)
        await repo.save_connector(conn2_inactive)

        # List active only
        active = await repo.list_connectors(active_only=True)
        assert len(active) == 1
        assert active[0].connector_id == "conn.one"

        # List all
        all_conns = await repo.list_connectors(active_only=False)
        assert len(all_conns) == 2

        # Get specific connector
        retrieved = await repo.get_connector("conn.one")
        assert retrieved is not None
        assert retrieved.connector_id == "conn.one"


# ===================================================================
# 4. Management service
# ===================================================================


class TestCapabilityManagementService:
    """Test CapabilityManagementService operations."""

    async def test_create_tool_with_valid_connector(self) -> None:
        """Test creating a tool with a valid connector."""
        repo = InMemoryCapabilityRepository()
        await _seed_connector(repo)
        svc = CapabilityManagementService(repo)

        tool = await svc.create_tool(**_tool_kwargs())
        assert tool.capability_id == "tool.resolve_area"
        assert tool.status == "draft"
        assert tool.connector_ref == "conn.governance"

    async def test_create_tool_rejects_unknown_connector(self) -> None:
        """Test that creating a tool with unknown connector is rejected."""
        repo = InMemoryCapabilityRepository()
        svc = CapabilityManagementService(repo)

        with pytest.raises(ResourceNotFound, match="connector.*not found"):
            await svc.create_tool(**_tool_kwargs(connector_ref="conn.unknown"))

    async def test_create_tool_rejects_unapproved_path(self) -> None:
        """Test that creating a tool with unapproved path is rejected."""
        repo = InMemoryCapabilityRepository()
        await _seed_connector(repo, allowed_path_prefixes=["/v1/"])
        svc = CapabilityManagementService(repo)

        with pytest.raises(ResourceNotFound, match="not allowed by connector"):
            await svc.create_tool(
                **_tool_kwargs(resource_path="/v2/areas"),
            )

    async def test_publish_creates_snapshot(self) -> None:
        """Test that publishing a capability creates a snapshot."""
        repo = InMemoryCapabilityRepository()
        await _seed_connector(repo)
        svc = CapabilityManagementService(repo)

        tool = await svc.create_tool(**_tool_kwargs())
        tool = await svc.advance_status(
            capability_id=tool.capability_id,
            version=tool.version,
            to_status="testing",
            changed_by="tester",
        )
        tool = await svc.advance_status(
            capability_id=tool.capability_id,
            version=tool.version,
            to_status="pending_approval",
            changed_by="reviewer",
        )

        snapshot = await svc.publish(
            capability_id=tool.capability_id,
            version=tool.version,
            published_by="publisher",
        )

        assert snapshot.capability_id == tool.capability_id
        assert snapshot.version == tool.version
        assert snapshot.is_active

        # Verify snapshot is retrievable
        active = await svc.get_active_snapshot(tool.capability_id)
        assert active is not None
        assert active.version == tool.version

    async def test_disable_deactivates_snapshot(self) -> None:
        """Test that disabling a capability deactivates its snapshot."""
        repo = InMemoryCapabilityRepository()
        await _seed_connector(repo)
        svc = CapabilityManagementService(repo)

        tool = await svc.create_tool(**_tool_kwargs())
        tool = await svc.advance_status(
            capability_id=tool.capability_id,
            version=tool.version,
            to_status="testing",
            changed_by="tester",
        )
        tool = await svc.advance_status(
            capability_id=tool.capability_id,
            version=tool.version,
            to_status="pending_approval",
            changed_by="reviewer",
        )
        await svc.publish(
            capability_id=tool.capability_id,
            version=tool.version,
            published_by="publisher",
        )

        # Verify snapshot exists
        active = await svc.get_active_snapshot(tool.capability_id)
        assert active is not None

        # Disable
        disabled = await svc.disable(
            capability_id=tool.capability_id,
            version=tool.version,
            changed_by="admin",
        )
        assert disabled.status == "disabled"

        # Verify snapshot is deactivated
        active = await svc.get_active_snapshot(tool.capability_id)
        assert active is None

    async def test_rollback_to_previous_version(self) -> None:
        """Test rolling back to a previous version."""
        repo = InMemoryCapabilityRepository()
        await _seed_connector(repo)
        svc = CapabilityManagementService(repo)

        # Create v1
        v1 = await svc.create_tool(**_tool_kwargs(version="1.0.0"))
        v1 = await svc.advance_status(
            capability_id=v1.capability_id,
            version=v1.version,
            to_status="testing",
            changed_by="tester",
        )
        v1 = await svc.advance_status(
            capability_id=v1.capability_id,
            version=v1.version,
            to_status="pending_approval",
            changed_by="reviewer",
        )
        await svc.publish(
            capability_id=v1.capability_id,
            version=v1.version,
            published_by="publisher",
        )

        # Create v2
        v2 = await svc.create_tool(**_tool_kwargs(version="1.0.1"))
        v2 = await svc.advance_status(
            capability_id=v2.capability_id,
            version=v2.version,
            to_status="testing",
            changed_by="tester",
        )
        v2 = await svc.advance_status(
            capability_id=v2.capability_id,
            version=v2.version,
            to_status="pending_approval",
            changed_by="reviewer",
        )
        await svc.publish(
            capability_id=v2.capability_id,
            version=v2.version,
            published_by="publisher",
        )

        # Verify v2 is active
        active = await svc.get_active_snapshot(v2.capability_id)
        assert active is not None
        assert active.version == "1.0.1"

        # Rollback to v1
        rollback_snap = await svc.rollback(
            capability_id=v2.capability_id,
            to_version="1.0.0",
            changed_by="admin",
            reason="v2 has issues",
        )

        assert rollback_snap.version == "1.0.0"

        # Verify v1 is now active
        active = await svc.get_active_snapshot(v2.capability_id)
        assert active is not None
        assert active.version == "1.0.0"

    async def test_get_runtime_tools_returns_only_published(self) -> None:
        """Test that get_runtime_tools returns only published tools."""
        repo = InMemoryCapabilityRepository()
        await _seed_connector(repo)
        svc = CapabilityManagementService(repo)

        # Create draft tool
        draft = await svc.create_tool(**_tool_kwargs(capability_id="tool.draft"))
        assert draft.status == "draft"

        # Create published tool
        pub = await svc.create_tool(**_tool_kwargs(capability_id="tool.published"))
        pub = await svc.advance_status(
            capability_id=pub.capability_id,
            version=pub.version,
            to_status="testing",
            changed_by="tester",
        )
        pub = await svc.advance_status(
            capability_id=pub.capability_id,
            version=pub.version,
            to_status="pending_approval",
            changed_by="reviewer",
        )
        await svc.publish(
            capability_id=pub.capability_id,
            version=pub.version,
            published_by="publisher",
        )

        # Get runtime tools
        runtime_tools = await svc.get_runtime_tools()
        assert len(runtime_tools) == 1
        assert runtime_tools[0].capability_id == "tool.published"

    async def test_skill_create_and_publish(self) -> None:
        """Test creating and publishing a skill."""
        repo = InMemoryCapabilityRepository()
        svc = CapabilityManagementService(repo)

        skill = await svc.create_skill(
            capability_id="skill.analysis",
            name="Data Analysis Skill",
            owner="team-analytics",
            version="1.0.0",
            guidance="Use this skill for data analysis tasks",
            allowed_tool_ids=["tool.query", "tool.aggregate"],
        )

        assert skill.capability_id == "skill.analysis"
        assert skill.status == "draft"

        # Advance to testing
        skill = await svc.advance_status(
            capability_id=skill.capability_id,
            version=skill.version,
            to_status="testing",
            changed_by="tester",
        )

        # Advance to pending_approval
        skill = await svc.advance_status(
            capability_id=skill.capability_id,
            version=skill.version,
            to_status="pending_approval",
            changed_by="reviewer",
        )

        # Publish
        snapshot = await svc.publish(
            capability_id=skill.capability_id,
            version=skill.version,
            published_by="publisher",
        )

        assert snapshot.capability_id == "skill.analysis"
        assert snapshot.version == "1.0.0"

    async def test_workflow_create_with_nodes(self) -> None:
        """Test creating a workflow with nodes and edges."""
        repo = InMemoryCapabilityRepository()
        svc = CapabilityManagementService(repo)

        workflow = await svc.create_workflow(
            capability_id="workflow.case_review",
            name="Case Review Workflow",
            owner="team-workflow",
            version="1.0.0",
            nodes=[
                {
                    "node_id": "start",
                    "node_type": "start",
                },
                {
                    "node_id": "review",
                    "node_type": "tool",
                    "tool_capability_id": "tool.resolve_area",
                    "tool_version": "1.0.0",
                },
                {
                    "node_id": "end",
                    "node_type": "end",
                },
            ],
            edges=[
                {
                    "source_node_id": "start",
                    "target_node_id": "review",
                },
                {
                    "source_node_id": "review",
                    "target_node_id": "end",
                },
            ],
        )

        assert workflow.capability_id == "workflow.case_review"
        assert workflow.status == "draft"
        assert len(workflow.nodes) == 3
        assert len(workflow.edges) == 2


# ===================================================================
# 5. Security
# ===================================================================


class TestSecurity:
    """Test security constraints."""

    async def test_tool_cannot_skip_lifecycle(self) -> None:
        """Test that tools cannot skip lifecycle stages."""
        repo = InMemoryCapabilityRepository()
        await _seed_connector(repo)
        svc = CapabilityManagementService(repo)

        tool = await svc.create_tool(**_tool_kwargs())

        # Try to skip directly to published
        with pytest.raises(RunStateConflict, match="invalid transition"):
            await svc.advance_status(
                capability_id=tool.capability_id,
                version=tool.version,
                to_status="published",
                changed_by="hacker",
            )

        # Try to skip to disabled
        with pytest.raises(RunStateConflict, match="invalid transition"):
            await svc.advance_status(
                capability_id=tool.capability_id,
                version=tool.version,
                to_status="disabled",
                changed_by="hacker",
            )

    async def test_snapshot_content_is_immutable_copy(self) -> None:
        """Test that snapshot content is an immutable copy of the capability."""
        repo = InMemoryCapabilityRepository()
        await _seed_connector(repo)
        svc = CapabilityManagementService(repo)

        tool = await svc.create_tool(
            **_tool_kwargs(name="Original Name"),
        )
        tool = await svc.advance_status(
            capability_id=tool.capability_id,
            version=tool.version,
            to_status="testing",
            changed_by="tester",
        )
        tool = await svc.advance_status(
            capability_id=tool.capability_id,
            version=tool.version,
            to_status="pending_approval",
            changed_by="reviewer",
        )

        snapshot = await svc.publish(
            capability_id=tool.capability_id,
            version=tool.version,
            published_by="publisher",
        )

        # Snapshot content should have the original name
        assert snapshot.content["name"] == "Original Name"

        # Modify the original tool's metadata (create new version with different id)
        v2 = await svc.create_tool(
            **_tool_kwargs(
                capability_id="tool.resolve_area_v2",
                name="Updated Name",
                version="2.0.0",
            ),
        )

        # Snapshot should still have original name (immutability)
        active = await svc.get_active_snapshot(tool.capability_id)
        assert active is not None
        assert active.content["name"] == "Original Name"

        # The v2 tool is independent
        assert v2.name == "Updated Name"


# ===================================================================
# Additional edge cases
# ===================================================================


class TestWorkflowNodeValidation:
    """Test workflow node validation."""

    async def test_tool_node_requires_capability_id(self) -> None:
        """Test that tool nodes require tool_capability_id."""
        # Valid tool node
        node = WorkflowNodeDefinition(
            node_id="review",
            node_type="tool",
            tool_capability_id="tool.resolve_area",
        )
        assert node.node_type == "tool"

        # Invalid: tool node without capability_id
        with pytest.raises(ValidationError, match="tool node requires"):
            WorkflowNodeDefinition(
                node_id="review",
                node_type="tool",
            )

        # Valid: non-tool node without capability_id
        node = WorkflowNodeDefinition(
            node_id="start",
            node_type="start",
        )
        assert node.node_type == "start"


class TestConnectorValidation:
    """Test connector validation."""

    async def test_connector_base_url_must_be_http(self) -> None:
        """Test that connector base_url must start with http:// or https://."""
        # Valid
        conn = Connector(
            connector_id="conn.valid",
            name="Valid",
            base_url="https://api.example.com",
        )
        assert conn.base_url == "https://api.example.com"

        # Invalid
        for url in ["ftp://api.example.com", "api.example.com", ""]:
            with pytest.raises(ValidationError):
                Connector(
                    connector_id="conn.invalid",
                    name="Invalid",
                    base_url=url,
                )
