"""P2-2 Run Capability Snapshot Service.

This service provides per-run capability snapshots, ensuring that:
1. Each new run gets a snapshot of published capabilities at creation time
2. Publish/deactivate/rollback only affects subsequently created runs
3. Already running runs keep their original capability version

This implements the hot-publish requirement where capability changes don't
affect in-flight runs.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from full_view_agent.application.tool_registry import ToolRegistry
from full_view_agent.domain.capability import ToolCapability

if TYPE_CHECKING:
    from full_view_agent.infrastructure.capability_repository import (
        CapabilityRepository,
    )

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RunCapabilitySnapshot:
    """Immutable snapshot of capabilities for a specific run.

    This snapshot captures the state of published tools at the moment
    the run was created. The run uses this snapshot throughout its
    lifetime, regardless of subsequent capability changes.
    """

    run_id: str
    created_at: datetime
    tool_registry: ToolRegistry
    tool_versions: dict[str, str] = field(default_factory=dict)
    # Maps tool_id -> version at snapshot time


class RunCapabilitySnapshotService:
    """Manages per-run capability snapshots.

    This service creates immutable snapshots of the capability registry
    for each new run. Runs use their snapshot throughout their lifetime,
    ensuring consistency even if capabilities are published/deactivated/
    rolled back after the run starts.
    """

    def __init__(self, repository: CapabilityRepository) -> None:
        self._repository = repository
        self._snapshots: dict[str, RunCapabilitySnapshot] = {}

    async def create_snapshot_for_run(
        self,
        run_id: str,
        base_registry: ToolRegistry,
    ) -> RunCapabilitySnapshot:
        """Create a capability snapshot for a new run.

        This loads all currently published tools and merges them into
        the base registry, creating an immutable snapshot for the run.

        Args:
            run_id: The ID of the run being created
            base_registry: The base ToolRegistry (with static tools)

        Returns:
            A RunCapabilitySnapshot with the merged registry
        """
        # Load all currently published tools
        published_tools = await self._load_published_tools()

        logger.info(
            f"Creating capability snapshot for run {run_id}: "
            f"{len(published_tools)} published tools"
        )

        # Convert to registry entries
        from full_view_agent.application.dynamic_tool_bridge import (
            build_dynamic_tool_registry_entries,
        )

        manifests, descriptors = build_dynamic_tool_registry_entries(published_tools)

        # Extract input schemas
        dynamic_input_schemas = {
            tool.capability_id: tool.input_schema
            for tool in published_tools
            if tool.input_schema
        }

        # Merge into base registry
        snapshot_registry = base_registry.merge_dynamic(
            manifests=manifests,
            descriptors=descriptors,
            dynamic_input_schemas=dynamic_input_schemas,
        )

        # Build version map
        tool_versions = {
            tool.capability_id: tool.version for tool in published_tools
        }

        # Create immutable snapshot
        snapshot = RunCapabilitySnapshot(
            run_id=run_id,
            created_at=datetime.now(UTC),
            tool_registry=snapshot_registry,
            tool_versions=tool_versions,
        )

        # Store snapshot
        self._snapshots[run_id] = snapshot

        logger.info(
            f"Created snapshot for run {run_id} with "
            f"{len(tool_versions)} dynamic tools"
        )

        return snapshot

    def get_snapshot_for_run(self, run_id: str) -> RunCapabilitySnapshot | None:
        """Get the capability snapshot for an existing run.

        Args:
            run_id: The ID of the run

        Returns:
            The RunCapabilitySnapshot if it exists, None otherwise
        """
        return self._snapshots.get(run_id)

    def get_registry_for_run(
        self, run_id: str, base_registry: ToolRegistry
    ) -> ToolRegistry:
        """Get the ToolRegistry for a run.

        If the run has a snapshot, return its registry. Otherwise,
        return the base registry (for runs created before snapshots
        were implemented).

        Args:
            run_id: The ID of the run
            base_registry: The base ToolRegistry to use if no snapshot exists

        Returns:
            The ToolRegistry for the run
        """
        snapshot = self.get_snapshot_for_run(run_id)
        if snapshot is not None:
            return snapshot.tool_registry
        # Fallback to base registry for runs without snapshots
        return base_registry

    async def _load_published_tools(self) -> list[ToolCapability]:
        """Load all currently published tools from the repository."""
        from full_view_agent.application.dynamic_tool_bridge import load_published_tools

        return await load_published_tools(self._repository)

    def remove_snapshot(self, run_id: str) -> None:
        """Remove a snapshot when a run completes.

        This frees memory by removing snapshots for completed runs.

        Args:
            run_id: The ID of the completed run
        """
        if run_id in self._snapshots:
            del self._snapshots[run_id]
            logger.debug(f"Removed snapshot for completed run {run_id}")

    def list_active_snapshots(self) -> list[str]:
        """List all run IDs with active snapshots.

        Returns:
            List of run IDs
        """
        return list(self._snapshots.keys())
