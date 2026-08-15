"""P2-1 capability management service.

Owns the lifecycle of Tool / Skill / Workflow capabilities:
create → test → approve → publish → disable → rollback.
Published snapshots are immutable; the agent runtime reads only published.
"""

from __future__ import annotations

from datetime import UTC, datetime

from full_view_agent.application.errors import (
    ConnectorConfigurationInvalid,
    ResourceNotFound,
    RunStateConflict,
    UpstreamUnavailable,
)
from full_view_agent.application.session_run_service import new_id
from full_view_agent.domain.capability import (
    CapabilityBase,
    CapabilityLifecycleEvent,
    CapabilitySnapshot,
    CapabilityStatus,
    CapabilityType,
    Connector,
    ConnectorAuditEvent,
    SkillCapability,
    ToolCapability,
    ToolSemanticContract,
    WorkflowCapability,
    WorkflowEdgeDefinition,
    WorkflowNodeDefinition,
    is_valid_transition,
)
from full_view_agent.infrastructure.capability_repository import (
    CapabilityRepository,
)
from full_view_agent.infrastructure.http_connector_executor import (
    HttpConnectorExecutor,
    SSRFProtectionError,
    configured_connector_allowed_private_hosts,
)
from full_view_agent.semantic.contract_intent_resolver import (
    ContractSemanticIntentResolver,
)


class CapabilityManagementService:
    """Manages capability lifecycle.  All mutations go through here."""

    def __init__(
        self,
        repository: CapabilityRepository,
        *,
        allowed_private_hosts: frozenset[str] | None = None,
    ) -> None:
        self._repo = repository
        self._allowed_private_hosts = (
            configured_connector_allowed_private_hosts()
            if allowed_private_hosts is None
            else allowed_private_hosts
        )

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
        semantic_contract: ToolSemanticContract | dict[str, object] | None = None,
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
            semantic_contract=(
                ToolSemanticContract.model_validate(semantic_contract)
                if semantic_contract is not None
                else None
            ),
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
        expected_etag: int | None = None,
        **fields: object,
    ) -> ToolCapability:
        existing = await self._repo.get(capability_id, version)
        if existing is None:
            raise ResourceNotFound("tool not found")
        if not isinstance(existing, ToolCapability):
            raise ResourceNotFound("not a tool capability")
        if expected_etag is not None and existing.etag != expected_etag:
            raise RunStateConflict("capability etag mismatch")
        if existing.status in ("published",):
            raise RunStateConflict("cannot modify a published tool version")
        if existing.status == "disabled":
            raise RunStateConflict("cannot modify a disabled tool version")
        if fields.get("semantic_contract") is not None:
            fields["semantic_contract"] = ToolSemanticContract.model_validate(
                fields["semantic_contract"]
            )
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
        nodes: list[WorkflowNodeDefinition] | None = None,
        edges: list[WorkflowEdgeDefinition] | None = None,
        timeout_seconds: int = 300,
        requires_human_confirmation: bool = False,
        created_by: str = "system",
    ) -> WorkflowCapability:
        parsed_nodes = [WorkflowNodeDefinition.model_validate(n) for n in nodes] if nodes else []
        parsed_edges = [WorkflowEdgeDefinition.model_validate(e) for e in edges] if edges else []
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
        expected_etag: int | None = None,
    ) -> CapabilityBase:
        existing = await self._repo.get(capability_id, version)
        if existing is None:
            raise ResourceNotFound("capability not found")
        if expected_etag is not None and existing.etag != expected_etag:
            raise RunStateConflict("capability etag mismatch")
        if not is_valid_transition(existing.status, to_status):
            raise RunStateConflict(f"invalid transition {existing.status} -> {to_status}")
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

    async def mark_testing(
        self,
        *,
        capability_id: str,
        version: str,
        changed_by: str,
        reason: str = "",
        expected_etag: int | None = None,
    ) -> CapabilityBase:
        return await self.advance_status(
            capability_id=capability_id,
            version=version,
            to_status="testing",
            changed_by=changed_by,
            reason=reason,
            expected_etag=expected_etag,
        )

    async def publish(
        self,
        *,
        capability_id: str,
        version: str,
        published_by: str,
        reason: str = "",
        expected_etag: int | None = None,
    ) -> CapabilitySnapshot:
        candidate = await self._repo.get(capability_id, version)
        if (
            isinstance(candidate, ToolCapability)
            and candidate.capability_id == "governance.query_population_metrics"
            and candidate.semantic_contract is None
        ):
            raise RunStateConflict(
                "tool semantic contract is required before publish"
            )
        if isinstance(candidate, ToolCapability) and candidate.semantic_contract is not None:
            ToolSemanticContract.model_validate(
                candidate.semantic_contract.model_dump(mode="python")
            )
            coverage = ContractSemanticIntentResolver().validate_coverage(
                candidate.semantic_contract
            )
            if not coverage.valid:
                raise RunStateConflict(
                    "tool semantic contract intent examples do not resolve uniquely: "
                    + "; ".join(coverage.issues)
                )
        capability = await self.advance_status(
            capability_id=capability_id,
            version=version,
            to_status="published",
            changed_by=published_by,
            reason=reason,
            expected_etag=expected_etag,
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
        expected_etag: int | None = None,
    ) -> CapabilityBase:
        result = await self.advance_status(
            capability_id=capability_id,
            version=version,
            to_status="disabled",
            changed_by=changed_by,
            reason=reason,
            expected_etag=expected_etag,
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
        expected_etag: int | None = None,
    ) -> CapabilitySnapshot:
        current = await self._repo.get_active_snapshot(capability_id)
        target = await self._repo.get(capability_id, to_version)
        if target is None:
            raise ResourceNotFound("target version not found")
        if expected_etag is not None and target.etag != expected_etag:
            raise RunStateConflict("capability etag mismatch")
        if target.status not in ("published", "disabled"):
            raise RunStateConflict("rollback requires a previously published version")
        if target.status == "published":
            pass
        else:
            previous_status = target.status
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
            await self._repo.record_lifecycle_event(
                CapabilityLifecycleEvent(
                    event_id=new_id("cle"),
                    capability_id=capability_id,
                    from_status=previous_status,
                    to_status="published",
                    version=to_version,
                    changed_by=changed_by,
                    reason=f"rollback restore: {reason}",
                )
            )
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

    async def get(self, capability_id: str, version: str) -> CapabilityBase | None:
        return await self._repo.get(capability_id, version)

    async def list_capabilities(
        self,
        *,
        capability_type: CapabilityType | None = None,
        status: CapabilityStatus | None = None,
    ) -> list[CapabilityBase]:
        return await self._repo.list_capabilities(capability_type=capability_type, status=status)

    async def get_active_snapshot(self, capability_id: str) -> CapabilitySnapshot | None:
        return await self._repo.get_active_snapshot(capability_id)

    async def get_runtime_tools(self) -> list[ToolCapability]:
        """Return only published tools for runtime discovery."""
        all_tools = await self._repo.list_capabilities(capability_type="tool", status="published")
        return [t for t in all_tools if isinstance(t, ToolCapability)]

    async def list_lifecycle_events(self, capability_id: str) -> list[CapabilityLifecycleEvent]:
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
        created_by: str = "system",
    ) -> Connector:
        if await self._repo.get_connector(connector_id) is not None:
            raise RunStateConflict("connector already exists")
        connector = Connector(
            connector_id=connector_id,
            name=name,
            base_url=base_url,
            description=description,
            allowed_path_prefixes=allowed_path_prefixes or [],
            denied_hosts=denied_hosts or [],
            credential_ref=credential_ref,
            timeout_ms=timeout_ms,
            created_by=created_by,
            updated_by=created_by,
        )
        await self._validate_connector_configuration(connector)
        await self._repo.save_connector(connector)
        return connector

    async def list_connectors(self, *, active_only: bool = True) -> list[Connector]:
        return await self._repo.list_connectors(active_only=active_only)

    async def get_connector(self, connector_id: str) -> Connector:
        connector = await self._repo.get_connector(connector_id)
        if connector is None:
            raise ResourceNotFound("connector not found")
        return connector

    async def update_connector(
        self,
        *,
        connector_id: str,
        expected_etag: int,
        actor: str,
        reason: str,
        **fields: object,
    ) -> Connector:
        existing = await self.get_connector(connector_id)
        if existing.etag != expected_etag:
            raise RunStateConflict("connector etag conflict")
        allowed_fields = {
            "name", "base_url", "description", "allowed_path_prefixes",
            "denied_hosts", "timeout_ms", "credential_ref",
        }
        unknown = set(fields).difference(allowed_fields)
        if unknown:
            raise ValueError(f"unsupported connector fields: {sorted(unknown)}")
        changes = {
            key: value
            for key, value in fields.items()
            if value is not None or key == "credential_ref"
        }
        candidate = existing.model_copy(update=changes)
        await self._validate_connector_configuration(candidate)
        changed_fields = sorted(
            key for key in changes if getattr(existing, key) != getattr(candidate, key)
        )
        candidate = candidate.model_copy(
            update={
                "updated_by": actor,
                "updated_at": datetime.now(UTC),
                "etag": existing.etag + 1,
            }
        )
        event = ConnectorAuditEvent(
            event_id=new_id("cae"), connector_id=connector_id,
            action="update", actor=actor, reason=reason,
            previous_etag=existing.etag, new_etag=candidate.etag,
            changed_fields=changed_fields, from_active=existing.is_active,
            to_active=candidate.is_active,
        )
        try:
            return await self._repo.update_connector(
                candidate, expected_etag=expected_etag, event=event
            )
        except KeyError as exc:
            raise ResourceNotFound("connector not found") from exc

    async def set_connector_active(
        self,
        *,
        connector_id: str,
        is_active: bool,
        expected_etag: int,
        actor: str,
        reason: str,
    ) -> Connector:
        existing = await self.get_connector(connector_id)
        if existing.etag != expected_etag:
            raise RunStateConflict("connector etag conflict")
        if existing.is_active == is_active:
            raise RunStateConflict("connector already has requested state")
        if is_active:
            await self._validate_connector_configuration(existing)
        candidate = existing.model_copy(
            update={
                "is_active": is_active,
                "updated_by": actor,
                "updated_at": datetime.now(UTC),
                "etag": existing.etag + 1,
            }
        )
        event = ConnectorAuditEvent(
            event_id=new_id("cae"), connector_id=connector_id,
            action="enable" if is_active else "disable",
            actor=actor, reason=reason, previous_etag=existing.etag,
            new_etag=candidate.etag, changed_fields=["is_active"],
            from_active=existing.is_active, to_active=is_active,
        )
        try:
            return await self._repo.update_connector(
                candidate, expected_etag=expected_etag, event=event
            )
        except KeyError as exc:
            raise ResourceNotFound("connector not found") from exc

    async def list_connector_audit_events(
        self, connector_id: str
    ) -> list[ConnectorAuditEvent]:
        await self.get_connector(connector_id)
        return await self._repo.list_connector_audit_events(connector_id)

    async def _validate_connector_configuration(self, connector: Connector) -> None:
        if not connector.allowed_path_prefixes:
            raise ConnectorConfigurationInvalid(
                "允许路径前缀不能为空"
            )
        import posixpath

        for prefix in connector.allowed_path_prefixes:
            if (
                not prefix.startswith("/")
                or "//" in prefix
                or ".." in prefix.split("/")
                or posixpath.normpath(prefix) != prefix
            ):
                raise ConnectorConfigurationInvalid(
                    "允许路径前缀包含不安全或非规范路径"
                )
        validator = HttpConnectorExecutor(
            self._repo, allowed_private_hosts=self._allowed_private_hosts
        )
        try:
            validator._validate_url_ssrf(connector.base_url, connector.denied_hosts)  # noqa: SLF001
            await validator._validate_dns(connector.base_url)  # noqa: SLF001
        except (SSRFProtectionError, UpstreamUnavailable) as exc:
            raise ConnectorConfigurationInvalid(
                "基础地址未通过 SSRF 安全校验"
            ) from exc


def _path_allowed(connector: Connector, resource_path: str) -> bool:
    """F4: Check if resource_path is within connector's allowed prefixes.

    Security requirements:
    - Path whitelist must be non-empty (enforced at creation)
    - Normalize paths to prevent ../ and // bypasses
    - Exact prefix matching only
    """
    # Normalize the resource path
    import posixpath

    normalized_path = posixpath.normpath(resource_path)

    # Reject paths with directory traversal attempts
    if ".." in normalized_path or "//" in resource_path:
        return False

    # Must have at least one allowed prefix (enforced at creation, but double-check)
    if not connector.allowed_path_prefixes:
        return False

    # Check if normalized path starts with any allowed prefix
    for prefix in connector.allowed_path_prefixes:
        normalized_prefix = posixpath.normpath(prefix)
        if normalized_prefix == "/" or normalized_path == normalized_prefix:
            return True
        if normalized_path.startswith(f"{normalized_prefix.rstrip('/')}/"):
            return True
    return False
