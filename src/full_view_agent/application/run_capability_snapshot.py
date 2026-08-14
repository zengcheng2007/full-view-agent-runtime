"""P2-2 Run Capability Snapshot Service.

This service provides per-run capability snapshots, ensuring that:
1. Each new run gets a snapshot of published capabilities at creation time
2. Publish/deactivate/rollback only affects subsequently created runs
3. Already running runs keep their original capability version

This implements the hot-publish requirement where capability changes don't
affect in-flight runs.

Cross-process durability
------------------------

The in-memory ``_snapshots`` cache is the fast path within a process.
When the cache is empty (process restart), the service consults the
optional ``RunCapabilitySnapshotStore`` (DB-backed in production). If a
persisted snapshot exists for the run_id, the registry is rebuilt from
the stored (tool_id -> version) map plus the capability repository —
this ensures the Run keeps exactly the versions that were pinned when
it started, even across restarts.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol

from full_view_agent.application.run_capability_snapshot_store import (
    PersistedRunCapabilitySnapshot,
    RunCapabilitySnapshotStore,
)
from full_view_agent.application.runtime_prompt_registry import RuntimePromptRegistry
from full_view_agent.application.runtime_skill_registry import RuntimeSkillRegistry
from full_view_agent.application.runtime_workflow_registry import RuntimeWorkflowRegistry
from full_view_agent.application.tool_registry import ToolRegistry
from full_view_agent.domain.agent_definition import AgentReleaseSnapshot
from full_view_agent.domain.application import ApplicationCapabilityBinding
from full_view_agent.domain.capability import (
    SkillCapability,
    ToolCapability,
    WorkflowCapability,
)
from full_view_agent.domain.prompt_template import RuntimePromptSnapshot

if TYPE_CHECKING:
    from full_view_agent.infrastructure.capability_repository import (
        CapabilityRepository,
    )

logger = logging.getLogger(__name__)


class ApplicationCapabilityBindingReader(Protocol):
    async def list_capability_bindings(
        self, *, app_id: str, enabled_only: bool = False
    ) -> list[ApplicationCapabilityBinding]: ...


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
    runtime_skill_registry: RuntimeSkillRegistry = field(
        default_factory=RuntimeSkillRegistry
    )
    runtime_workflow_registry: RuntimeWorkflowRegistry = field(
        default_factory=RuntimeWorkflowRegistry
    )
    runtime_prompt_snapshot: RuntimePromptSnapshot | None = None
    knowledge_base_versions: dict[str, int] = field(default_factory=dict)
    agent_scoped: bool = False


class RunCapabilitySnapshotService:
    """Manages per-run capability snapshots.

    This service creates immutable snapshots of the capability registry
    for each new run. Runs use their snapshot throughout their lifetime,
    ensuring consistency even if capabilities are published/deactivated/
    rolled back after the run starts.
    """

    def __init__(
        self,
        repository: CapabilityRepository,
        store: RunCapabilitySnapshotStore | None = None,
        application_registry: ApplicationCapabilityBindingReader | None = None,
        runtime_skill_registry: RuntimeSkillRegistry | None = None,
        runtime_workflow_registry: RuntimeWorkflowRegistry | None = None,
        runtime_prompt_registry: RuntimePromptRegistry | None = None,
        prompt_snapshot_loader=None,
    ) -> None:
        self._repository = repository
        self._snapshots: dict[str, RunCapabilitySnapshot] = {}
        self._store = store
        self._application_registry = application_registry
        self._runtime_skill_registry = runtime_skill_registry or RuntimeSkillRegistry()
        self._runtime_workflow_registry = (
            runtime_workflow_registry or RuntimeWorkflowRegistry()
        )
        self._runtime_prompt_registry = runtime_prompt_registry or RuntimePromptRegistry()
        self._prompt_snapshot_loader = prompt_snapshot_loader

    async def create_snapshot_for_run(
        self,
        run_id: str,
        base_registry: ToolRegistry,
        app_id: str | None = None,
        agent_release: AgentReleaseSnapshot | None = None,
    ) -> RunCapabilitySnapshot:
        """Create a capability snapshot for a new run.

        This loads all currently published tools and merges them into
        the base registry, creating an immutable snapshot for the run.

        Idempotent within a process: when a snapshot already exists for
        ``run_id`` (e.g. the Run is being resumed after a
        waiting-for-reauth pause), the cached snapshot is returned
        unchanged so the Run keeps the exact same pinned registry.

        Cross-process durability: when the in-memory cache is empty
        (process restart), the service consults the optional
        ``RunCapabilitySnapshotStore`` (DB-backed). If a persisted
        snapshot exists for the run_id, the registry is rebuilt from
        the stored (tool_id -> version) map plus the capability
        repository.

        Args:
            run_id: The ID of the run being created
            base_registry: The base ToolRegistry (with static tools)

        Returns:
            A RunCapabilitySnapshot with the merged registry
        """
        existing = self.get_snapshot_for_run(run_id)
        if existing is not None:
            logger.debug(
                "Reusing existing capability snapshot for run %s "
                "(created_at=%s)",
                run_id,
                existing.created_at.isoformat(),
            )
            return existing

        # Cross-process recovery: consult the persistent store.
        # Fail closed: if a persisted snapshot exists for this run_id
        # but cannot be rebuilt (e.g. a pinned tool version has been
        # purged from the capability repository), raise instead of
        # silently falling back to the live capability set. Substituting
        # the live set would silently break the Run-pinned semantic.
        if self._store is not None:
            persisted = await self._store.load(run_id)
            if persisted is not None:
                rebuilt = await self._rebuild_from_persisted(
                    run_id=run_id,
                    persisted=persisted,
                    base_registry=base_registry,
                )
                if rebuilt is None:
                    raise RuntimeError(
                        f"cannot rebuild capability snapshot for run"
                        f" {run_id}: one or more pinned tool versions"
                        f" are no longer available in the capability"
                        f" repository; refusing to substitute the live"
                        f" capability set"
                    )
                self._snapshots[run_id] = rebuilt
                logger.info(
                    "Recovered capability snapshot for run %s from"
                    " persistent store (%d dynamic tools)",
                    run_id,
                    len(rebuilt.tool_versions),
                )
                return rebuilt

        if agent_release is not None:
            if app_id is None or agent_release.app_id != app_id:
                raise RuntimeError("agent release does not belong to the run application")
            release_tools = _reference_map(agent_release.capability_refs)
            release_skills = _reference_map(agent_release.skill_refs)
            release_workflows = _reference_map(agent_release.workflow_refs)
            release_capabilities = set(
                (*release_tools.items(), *release_skills.items(), *release_workflows.items())
            )
        else:
            release_tools = release_skills = release_workflows = {}
            release_capabilities = None

        allowed_capabilities: set[tuple[str, str]] | None = None
        if app_id is not None:
            if self._application_registry is None:
                raise RuntimeError("application-scoped snapshot requires a registry")
            bindings = await self._application_registry.list_capability_bindings(
                app_id=app_id,
                enabled_only=True,
            )
            allowed_capabilities = {
                (
                    str(binding.capability_id),
                    str(binding.capability_version),
                )
                for binding in bindings
            }
            if release_capabilities is not None:
                allowed_capabilities.intersection_update(release_capabilities)
            base_ids = set(base_registry.list_tool_ids())
            allowed_ids = {item[0] for item in allowed_capabilities}
            base_registry = base_registry.subset(base_ids.intersection(allowed_ids))

        # Load currently published tools, then apply the application grant.
        published_tools = await self._load_published_tools()
        if allowed_capabilities is not None:
            published_tools = [
                tool
                for tool in published_tools
                if (tool.capability_id, tool.version) in allowed_capabilities
            ]

        logger.info(
            f"Creating capability snapshot for run {run_id}: "
            f"{len(published_tools)} published tools"
        )

        # Convert to registry entries
        from full_view_agent.application.dynamic_tool_bridge import (
            build_dynamic_input_schemas,
            build_dynamic_tool_registry_entries,
        )

        manifests, descriptors = build_dynamic_tool_registry_entries(
            published_tools,
            base_registry=base_registry,
        )

        # Extract input schemas
        dynamic_input_schemas = build_dynamic_input_schemas(published_tools)

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
        runtime_skills = self._runtime_skill_registry.list()
        runtime_workflows = self._runtime_workflow_registry.list()
        if allowed_capabilities is not None:
            runtime_skills = tuple(
                skill
                for skill in runtime_skills
                if (skill.skill_id, skill.version) in allowed_capabilities
            )
            runtime_workflows = tuple(
                workflow
                for workflow in runtime_workflows
                if (workflow.workflow_id, workflow.version) in allowed_capabilities
            )
        skill_versions = {
            skill.skill_id: skill.version for skill in runtime_skills
        }
        workflow_versions = {
            workflow.workflow_id: workflow.version for workflow in runtime_workflows
        }
        prompt_snapshot = self._runtime_prompt_registry.snapshot()
        if agent_release is not None:
            prompt_snapshot = None
            if agent_release.prompt_ref is not None:
                if self._prompt_snapshot_loader is None:
                    raise RuntimeError("agent release prompt cannot be loaded")
                prompt_id, prompt_version = _one_reference(agent_release.prompt_ref)
                prompt_snapshot = await self._prompt_snapshot_loader(
                    prompt_id=prompt_id,
                    version=prompt_version,
                )
                if (
                    prompt_snapshot is None
                    or prompt_snapshot.app_id != app_id
                    or prompt_snapshot.version != prompt_version
                ):
                    raise RuntimeError("agent release prompt version is unavailable")
        if (
            prompt_snapshot is not None
            and app_id is not None
            and prompt_snapshot.app_id != app_id
        ):
            prompt_snapshot = None
        prompt_versions = (
            {prompt_snapshot.prompt_id: prompt_snapshot.version}
            if prompt_snapshot is not None
            else {}
        )
        knowledge_base_versions = (
            {
                knowledge_base_id: _knowledge_major(version)
                for knowledge_base_id, version in _reference_map(
                    agent_release.knowledge_base_refs
                ).items()
            }
            if agent_release is not None
            else {}
        )

        # Create immutable snapshot
        snapshot = RunCapabilitySnapshot(
            run_id=run_id,
            created_at=datetime.now(UTC),
            tool_registry=snapshot_registry,
            tool_versions=tool_versions,
            runtime_skill_registry=RuntimeSkillRegistry(runtime_skills),
            runtime_workflow_registry=RuntimeWorkflowRegistry(runtime_workflows),
            runtime_prompt_snapshot=prompt_snapshot,
            knowledge_base_versions=knowledge_base_versions,
            agent_scoped=agent_release is not None,
        )

        # Store snapshot in-memory
        self._snapshots[run_id] = snapshot

        # Persist snapshot for cross-process recovery. Atomic
        # first-write-wins: if another process persisted a snapshot for
        # this run_id between our `load` and our `store`, the other
        # process's snapshot is the winner. We adopt the winner so that
        # all subsequent reads (including in-memory caching) agree on
        # the same pinned versions across processes.
        if self._store is not None:
            static_tool_versions = {
                tool_id: snapshot_registry.get_manifest(tool_id).tool_version
                for tool_id in base_registry.list_tool_ids()
            }
            persisted_candidate = PersistedRunCapabilitySnapshot(
                run_id=run_id,
                tool_versions=tool_versions,
                captured_at=snapshot.created_at,
                skill_versions=skill_versions,
                workflow_versions=workflow_versions,
                prompt_versions=prompt_versions,
                knowledge_base_versions=knowledge_base_versions,
                static_tool_versions=static_tool_versions,
                application_scoped=app_id is not None,
                agent_scoped=agent_release is not None,
            )
            winner = await self._store.store_if_absent(persisted_candidate)
            if winner != persisted_candidate:
                # Lost the race — another process won. Rebuild from the
                # winner's version map so the in-memory cache matches
                # the persisted authority.
                rebuilt = await self._rebuild_from_persisted(
                    run_id=run_id,
                    persisted=winner,
                    base_registry=base_registry,
                )
                if rebuilt is None:
                    raise RuntimeError(
                        f"cannot rebuild capability snapshot for run"
                        f" {run_id}: concurrent snapshot has tool"
                        f" versions no longer available in the"
                        f" capability repository"
                    )
                snapshot = rebuilt
                self._snapshots[run_id] = snapshot

        logger.info(
            f"Created snapshot for run {run_id} with "
            f"{len(tool_versions)} dynamic tools"
        )

        return snapshot

    async def _rebuild_from_persisted(
        self,
        *,
        run_id: str,
        persisted: PersistedRunCapabilitySnapshot,
        base_registry: ToolRegistry,
        app_id: str | None = None,
    ) -> RunCapabilitySnapshot | None:
        """Rebuild a ``RunCapabilitySnapshot`` from persisted version triples.

        For each (tool_id, version) in the persisted map, load the
        matching ``ToolCapability`` from the capability repository and
        merge it into ``base_registry``. Returns ``None`` if any pinned
        version can no longer be loaded (fail closed — the Run should
        not silently fall back to the live capability set).
        """
        from full_view_agent.application.dynamic_tool_bridge import (
            build_dynamic_input_schemas,
            build_dynamic_tool_registry_entries,
        )

        if persisted.application_scoped:
            for tool_id, version in persisted.static_tool_versions.items():
                try:
                    manifest = base_registry.get_manifest(tool_id)
                except KeyError:
                    return None
                if manifest.tool_version != version:
                    return None
            base_registry = base_registry.subset(
                set(persisted.static_tool_versions)
            )

        rebuilt_tools: list[ToolCapability] = []
        for capability_id, version in persisted.tool_versions.items():
            loaded = await self._repository.get(
                capability_id=capability_id, version=version
            )
            if not isinstance(loaded, ToolCapability):
                logger.warning(
                    "Cannot rebuild snapshot for run %s: capability"
                    " %s v%s no longer available as a ToolCapability"
                    " in repository; failing closed.",
                    run_id,
                    capability_id,
                    version,
                )
                return None
            rebuilt_tools.append(loaded)

        manifests, descriptors = build_dynamic_tool_registry_entries(
            rebuilt_tools,
            base_registry=base_registry,
        )
        dynamic_input_schemas = build_dynamic_input_schemas(rebuilt_tools)
        snapshot_registry = base_registry.merge_dynamic(
            manifests=manifests,
            descriptors=descriptors,
            dynamic_input_schemas=dynamic_input_schemas,
        )
        from full_view_agent.application.dynamic_skill_workflow_bridge import (
            build_runtime_skill_contract,
            build_runtime_workflow_snapshot,
        )

        rebuilt_skills = []
        for capability_id, version in persisted.skill_versions.items():
            loaded = await self._repository.get(capability_id, version)
            if not isinstance(loaded, SkillCapability):
                return None
            rebuilt_skills.append(build_runtime_skill_contract(loaded))

        allowed_tool_refs = {
            (
                tool_id,
                snapshot_registry.get_manifest(tool_id).tool_version,
            )
            for tool_id in snapshot_registry.list_tool_ids()
        }
        rebuilt_workflows = []
        allowed_skill_refs = {
            (skill.skill_id, skill.version) for skill in rebuilt_skills
        }
        for capability_id, version in persisted.workflow_versions.items():
            loaded = await self._repository.get(capability_id, version)
            if not isinstance(loaded, WorkflowCapability):
                return None
            rebuilt_workflows.append(
                build_runtime_workflow_snapshot(
                    loaded,
                    allowed_tool_refs=allowed_tool_refs,
                    allowed_skill_refs=allowed_skill_refs,
                )
            )
        rebuilt_prompt = None
        if persisted.prompt_versions:
            if len(persisted.prompt_versions) != 1 or self._prompt_snapshot_loader is None:
                return None
            prompt_id, version = next(iter(persisted.prompt_versions.items()))
            rebuilt_prompt = await self._prompt_snapshot_loader(
                prompt_id=prompt_id, version=version
            )
            if rebuilt_prompt is None:
                return None
        return RunCapabilitySnapshot(
            run_id=run_id,
            created_at=persisted.captured_at,
            tool_registry=snapshot_registry,
            tool_versions=dict(persisted.tool_versions),
            runtime_skill_registry=RuntimeSkillRegistry(tuple(rebuilt_skills)),
            runtime_workflow_registry=RuntimeWorkflowRegistry(
                tuple(rebuilt_workflows)
            ),
            runtime_prompt_snapshot=rebuilt_prompt,
            knowledge_base_versions=dict(persisted.knowledge_base_versions),
            agent_scoped=persisted.agent_scoped,
        )

    async def get_or_create_snapshot_for_run(
        self,
        run_id: str,
        base_registry: ToolRegistry,
        app_id: str | None = None,
    ) -> RunCapabilitySnapshot:
        """Return the existing snapshot for a run if present, else create one.

        Resume path: a Run that entered the ``waiting`` state (e.g. awaiting
        reauthentication) keeps its snapshot pinned in memory so that
        resume() reuses the exact same registry. A process restart drops
        the in-memory cache; in that case we rebuild from the persisted
        snapshot (or the current published capability set if nothing was
        persisted — which only happens for Runs that started before the
        persistence migration ran).
        """
        existing = self.get_snapshot_for_run(run_id)
        if existing is not None:
            logger.debug(
                "Reusing existing capability snapshot for run %s "
                "(created_at=%s)",
                run_id,
                existing.created_at.isoformat(),
            )
            return existing
        return await self.create_snapshot_for_run(
            run_id,
            base_registry,
            app_id=app_id,
        )

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

    async def remove_snapshot(self, run_id: str) -> None:
        """Remove a snapshot when a run completes.

        This frees memory by removing snapshots for completed runs.
        Also removes the persisted row (if any) so a subsequent Run
        reusing the same run_id is not constrained by the previous
        Run's snapshot.

        Args:
            run_id: The ID of the completed run
        """
        if run_id in self._snapshots:
            del self._snapshots[run_id]
            logger.debug(f"Removed snapshot for completed run {run_id}")
        if self._store is not None:
            await self._store.delete(run_id)

    def list_active_snapshots(self) -> list[str]:
        """List all run IDs with active snapshots.

        Returns:
            List of run IDs
        """
        return list(self._snapshots.keys())


def _one_reference(reference: str) -> tuple[str, str]:
    if "@" not in reference:
        raise RuntimeError(f"invalid release reference: {reference}")
    resource_id, version = reference.rsplit("@", 1)
    parts = version.split(".")
    if not resource_id or len(parts) != 3 or not all(part.isdigit() for part in parts):
        raise RuntimeError(f"invalid release reference: {reference}")
    return resource_id, version


def _reference_map(references: tuple[str, ...]) -> dict[str, str]:
    result: dict[str, str] = {}
    for reference in references:
        resource_id, version = _one_reference(reference)
        if resource_id in result:
            raise RuntimeError(f"duplicate release reference: {resource_id}")
        result[resource_id] = version
    return result


def _knowledge_major(version: str) -> int:
    major, minor, patch = version.split(".")
    if minor != "0" or patch != "0" or int(major) < 1:
        raise RuntimeError(f"invalid knowledge release version: {version}")
    return int(major)
