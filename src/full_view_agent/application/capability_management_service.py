"""P2-1 capability management service.

Owns the lifecycle of Tool / Skill / Workflow capabilities:
create → test → approve → publish → disable → rollback.
Published snapshots are immutable; the agent runtime reads only published.
"""

from __future__ import annotations

from datetime import UTC, datetime

from full_view_agent.application.errors import ResourceNotFound, RunStateConflict
from full_view_agent.application.session_run_service import new_id
from full_view_agent.domain.capability import (
    CapabilityBase,
    CapabilityLifecycleEvent,
    CapabilitySnapshot,
    CapabilityStatus,
    CapabilityType,
    Connector,
    SkillCapability,
    ToolCapability,
    WorkflowCapability,
    is_valid_transition,
)
from full_view_agent.infrastructure.capability_repository import (
    CapabilityRepository,
)


class CapabilityManagementService:
    """Manages capability lifecycle.  All mutations go through here."""

    def __init__(self, repository: CapabilityRepository) -> None:
        self._repo = repository

    # ---- Tool CRUD ----

    async def create_tool(
        self,
        *,
        capability_id: str,
        name: str,
        owner: str,
        version: str,
        connector_ref: str,
        resource_path: str,
        domain: str = "governance",
        description: str = "",
        risk_level: str = "low",
        http_method: str = "GET",
        input_schema: dict[str, object] | None = None,
        output_schema: dict[str, object] | None = None,
        parameter_mapping: dict[str, object] | None = None,
        result_mapping: dict[str, object] | None = None,
        result_kind: str = "table",
        data_schema_ref: str = "",
        timeout_ms: int = 8000,
        max_attempts: int = 2,
        max_result_rows: int = 1000,
        cache_enabled: bool = True,
        cache_ttl_seconds: int = 60,
        credential_ref: str | None = None,
        required_permissions: list[str] | None = None,
        dataset_ids: list[str] | None = None,
        created_by: str = "system",
    ) -> ToolCapability:
        connector = await self._repo.get_connector(connector_ref)
        if connector is None or not connector.is_active:
            raise ResourceNotFound(f"connector {connector_ref} not found or inactive")
        if not _path_allowed(connector, resource_path):
            raise ResourceNotFound(
                f"resource_path {resource_path} not allowed by connector {connector_ref}"
            )
        tool = ToolCapability(
            capability_id=capability_id,
            name=name,
            domain=domain,
            owner=owner,
            version=version,
            status="draft",
            risk_level=risk_level,  # type: ignore[arg-type]
            required_permissions=required_permissions or [],
            dataset_ids=dataset_ids or [],
            description=description,
            connector_ref=connector_ref,
            http_method=http_method,  # type: ignore[arg-type]
            resource_path=resource_path,
            input_schema=input_schema or {},
            output_schema=output_schema or {},
            parameter_mapping=parameter_mapping or {},
            result_mapping=result_mapping or {},
            result_kind=result_kind,  # type: ignore[arg-type]
            data_schema_ref=data_schema_ref,
            timeout_ms=timeout_ms,
            max_attempts=max_attempts,
            max_result_rows=max_result_rows,
            cache_enabled=cache_enabled,
            cache_ttl_seconds=cache_ttl_seconds,
            credential_ref=credential_ref,
            created_by=created_by,
            updated_by=created_by,
        )
        return await self._repo.save_tool(tool)

    async def update_tool(
        self,
        *,
        capability_id: str,
        version: str,
        updated_by: str,
        **fields: object,
    ) -> ToolCapability:
        existing = await self._repo.get(capability_id, version)
        if existing is None:
            raise ResourceNotFound("tool not found")
        if not isinstance(existing, ToolCapability):
            raise ResourceNotFound("not a tool capability")
        if existing.status in ("published",):
            raise RunStateConflict("cannot modify a published tool version")
        if existing.status == "disabled":
            raise RunStateConflict("cannot modify a disabled tool version")
        updated = existing.model_copy(
            update={
                **{k: v for k, v in fields.items() if v is not None},
                "updated_by": updated_by,
                "updated_at": datetime.now(UTC),
                "etag": existing.etag + 1,
            }
        )
        assert isinstance(updated, ToolCapability)
        return await self._repo.save_tool(updated)

    # ---- Skill CRUD ----

    async def create_skill(
        self,
        *,
        capability_id: str,
        name: str,
        owner: str,
        version: str,
        domain: str = "governance",
        description: str = "",
        guidance: str = "",
        allowed_tool_ids: list[str] | None = None,
        applicable_questions: list[str] | None = None,
        examples: list[dict[str, str]] | None = None,
        counter_examples: list[dict[str, str]] | None = None,
        created_by: str = "system",
    ) -> SkillCapability:
        skill = SkillCapability(
            capability_id=capability_id,
            name=name,
            domain=domain,
            owner=owner,
            version=version,
            status="draft",
            description=description,
            guidance=guidance,
            allowed_tool_ids=allowed_tool_ids or [],
            applicable_questions=applicable_questions or [],
            examples=examples or [],
            counter_examples=counter_examples or [],
            created_by=created_by,
            updated_by=created_by,
        )
        return await self._repo.save_skill(skill)

    # ---- Workflow CRUD ----

    async def create_workflow(
        self,
        *,
        capability_id: str,
        name: str,
        owner: str,
        version: str,
        domain: str = "governance",
        description: str = "",
        nodes: list[dict[str, object]] | None = None,
        edges: list[dict[str, object]] | None = None,
        timeout_seconds: int = 300,
        requires_human_confirmation: bool = False,
        created_by: str = "system",
    ) -> WorkflowCapability:
        from full_view_agent.domain.capability import (
            WorkflowEdgeDefinition,
            WorkflowNodeDefinition,
        )

        parsed_nodes = (
            [WorkflowNodeDefinition.model_validate(n) for n in nodes]
            if nodes
            else []
        )
        parsed_edges = (
            [WorkflowEdgeDefinition.model_validate(e) for e in edges]
            if edges
            else []
        )
        workflow = WorkflowCapability(
            capability_id=capability_id,
            name=name,
            domain=domain,
            owner=owner,
            version=version,
            status="draft",
            description=description,
            nodes=parsed_nodes,
            edges=parsed_edges,
            timeout_seconds=timeout_seconds,
            requires_human_confirmation=requires_human_confirmation,
            created_by=created_by,
            updated_by=created_by,
        )
        return await self._repo.save_workflow(workflow)

    # ---- Lifecycle ----

    async def advance_status(
        self,
        *,
        capability_id: str,
        version: str,
        to_status: CapabilityStatus,
        changed_by: str,
        reason: str = "",
    ) -> CapabilityBase:
        existing = await self._repo.get(capability_id, version)
        if existing is None:
            raise ResourceNotFound("capability not found")
        if not is_valid_transition(existing.status, to_status):
            raise RunStateConflict(
                f"invalid transition {existing.status} -> {to_status}"
            )
        updated = existing.model_copy(
            update={
                "status": to_status,
                "updated_by": changed_by,
                "updated_at": datetime.now(UTC),
                "etag": existing.etag + 1,
            }
        )
        if isinstance(updated, ToolCapability):
            await self._repo.save_tool(updated)
        elif isinstance(updated, SkillCapability):
            await self._repo.save_skill(updated)
        elif isinstance(updated, WorkflowCapability):
            await self._repo.save_workflow(updated)
        await self._repo.record_lifecycle_event(
            CapabilityLifecycleEvent(
                event_id=new_id("cle"),
                capability_id=capability_id,
                from_status=existing.status,
                to_status=to_status,
                version=version,
                changed_by=changed_by,
                reason=reason,
            )
        )
        return updated

    async def publish(
        self,
        *,
        capability_id: str,
        version: str,
        published_by: str,
        reason: str = "",
    ) -> CapabilitySnapshot:
        capability = await self.advance_status(
            capability_id=capability_id,
            version=version,
            to_status="published",
            changed_by=published_by,
            reason=reason,
        )
        snapshot = CapabilitySnapshot(
            snapshot_id=new_id("snap"),
            capability_id=capability_id,
            capability_type=capability.capability_type,
            version=version,
            published_by=published_by,
            content=capability.model_dump(mode="json"),
        )
        await self._repo.put_snapshot(snapshot)
        return snapshot

    async def disable(
        self,
        *,
        capability_id: str,
        version: str,
        changed_by: str,
        reason: str = "",
    ) -> CapabilityBase:
        result = await self.advance_status(
            capability_id=capability_id,
            version=version,
            to_status="disabled",
            changed_by=changed_by,
            reason=reason,
        )
        await self._repo.deactivate_snapshots(capability_id)
        return result

    async def rollback(
        self,
        *,
        capability_id: str,
        to_version: str,
        changed_by: str,
        reason: str = "",
    ) -> CapabilitySnapshot:
        current = await self._repo.get_active_snapshot(capability_id)
        target = await self._repo.get(capability_id, to_version)
        if target is None:
            raise ResourceNotFound("target version not found")
        if target.status == "published":
            pass
        else:
            target = target.model_copy(
                update={
                    "status": "published",
                    "updated_by": changed_by,
                    "updated_at": datetime.now(UTC),
                    "etag": target.etag + 1,
                }
            )
            if isinstance(target, ToolCapability):
                await self._repo.save_tool(target)
            elif isinstance(target, SkillCapability):
                await self._repo.save_skill(target)
            elif isinstance(target, WorkflowCapability):
                await self._repo.save_workflow(target)
        snapshot = CapabilitySnapshot(
            snapshot_id=new_id("snap"),
            capability_id=capability_id,
            capability_type=target.capability_type,
            version=to_version,
            published_by=changed_by,
            content=target.model_dump(mode="json"),
        )
        await self._repo.put_snapshot(snapshot)
        if current is not None:
            await self.advance_status(
                capability_id=capability_id,
                version=current.version,
                to_status="disabled",
                changed_by=changed_by,
                reason=f"rollback to {to_version}: {reason}",
            )
        return snapshot

    # ---- Queries ----

    async def get(
        self, capability_id: str, version: str
    ) -> CapabilityBase | None:
        return await self._repo.get(capability_id, version)

    async def list_capabilities(
        self,
        *,
        capability_type: CapabilityType | None = None,
        status: CapabilityStatus | None = None,
    ) -> list[CapabilityBase]:
        return await self._repo.list_capabilities(
            capability_type=capability_type, status=status
        )

    async def get_active_snapshot(
        self, capability_id: str
    ) -> CapabilitySnapshot | None:
        return await self._repo.get_active_snapshot(capability_id)

    async def get_runtime_tools(self) -> list[ToolCapability]:
        """Return only published tools for runtime discovery."""
        all_tools = await self._repo.list_capabilities(
            capability_type="tool", status="published"
        )
        return [t for t in all_tools if isinstance(t, ToolCapability)]

    async def list_lifecycle_events(
        self, capability_id: str
    ) -> list[CapabilityLifecycleEvent]:
        return await self._repo.list_lifecycle_events(capability_id)

    # ---- Connectors ----

    async def create_connector(
        self,
        *,
        connector_id: str,
        name: str,
        base_url: str,
        description: str = "",
        allowed_path_prefixes: list[str] | None = None,
        denied_hosts: list[str] | None = None,
        timeout_ms: int = 8000,
        credential_ref: str | None = None,
    ) -> Connector:
        connector = Connector(
            connector_id=connector_id,
            name=name,
            base_url=base_url,
            description=description,
            allowed_path_prefixes=allowed_path_prefixes or [],
            denied_hosts=denied_hosts or [],
            credential_ref=credential_ref,
            timeout_ms=timeout_ms,
        )
        await self._repo.save_connector(connector)
        return connector

    async def list_connectors(
        self, *, active_only: bool = True
    ) -> list[Connector]:
        return await self._repo.list_connectors(active_only=active_only)


def _path_allowed(connector: Connector, resource_path: str) -> bool:
    """Check if resource_path is within connector's allowed prefixes."""
    if not connector.allowed_path_prefixes:
        return True
    return any(
        resource_path.startswith(prefix)
        for prefix in connector.allowed_path_prefixes
    )
