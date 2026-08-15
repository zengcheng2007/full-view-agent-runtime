"""Application-centred Agent assembly, validation and release service."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol

from full_view_agent.application.application_management_service import (
    ApplicationRegistry,
    CapabilityReader,
)
from full_view_agent.application.errors import ResourceNotFound, RunStateConflict
from full_view_agent.application.model_config_service import ModelConfigService
from full_view_agent.application.session_run_service import new_id
from full_view_agent.application.tool_registry import PRODUCTION_HTTP_TOOL_IDS
from full_view_agent.domain.agent_definition import (
    AgentDefinition,
    AgentExecutionPolicy,
    AgentModelPolicy,
    AgentModelVersionRef,
    AgentReleaseSnapshot,
    AgentValidationIssue,
    AgentValidationReport,
    AgentVersion,
    RunAgentReleaseSnapshot,
)
from full_view_agent.domain.capability import SkillCapability, WorkflowCapability
from full_view_agent.domain.prompt_template import PromptTemplate

if TYPE_CHECKING:
    from full_view_agent.application.model_config_repository import (
        RunModelBindingRepository,
    )


class AgentRepository(Protocol):
    async def save_agent(self, agent: AgentDefinition) -> AgentDefinition: ...
    async def get_agent(self, app_id: str, agent_id: str) -> AgentDefinition | None: ...
    async def list_agents(self, app_id: str) -> list[AgentDefinition]: ...
    async def save_version(self, version: AgentVersion) -> AgentVersion: ...
    async def get_version(
        self, app_id: str, agent_id: str, version: str
    ) -> AgentVersion | None: ...
    async def list_versions(self, app_id: str, agent_id: str) -> list[AgentVersion]: ...
    async def save_model_policy(
        self, app_id: str, agent_id: str, version: str, policy: AgentModelPolicy
    ) -> AgentModelPolicy: ...
    async def get_model_policy(
        self, app_id: str, agent_id: str, version: str
    ) -> AgentModelPolicy | None: ...
    async def publish(
        self, version: AgentVersion, release: AgentReleaseSnapshot
    ) -> AgentReleaseSnapshot: ...
    async def get_active_release(
        self, app_id: str, agent_id: str
    ) -> AgentReleaseSnapshot | None: ...
    async def bind_run(self, snapshot: RunAgentReleaseSnapshot) -> RunAgentReleaseSnapshot: ...
    async def get_run_snapshot(self, run_id: str) -> RunAgentReleaseSnapshot | None: ...


class PromptVersionReader(Protocol):
    async def get_template(self, prompt_id: str, version: str) -> PromptTemplate | None: ...


class KnowledgeVersionReader(Protocol):
    async def is_ready_version(
        self, *, app_id: str, knowledge_base_id: str, version: int
    ) -> bool: ...


class AgentManagementService:
    _LEGACY_BASELINE_VERSION = "0.0.1"
    _MANAGED_LEGACY_APP_ID = "full_information_view"
    _MANAGED_LEGACY_AGENT_ID = "governance_general_agent"
    _MANAGED_LEGACY_PUBLISHER = "system:migration"
    _MANAGED_LEGACY_REASONS = frozenset(
        {
            "backfill trusted legacy default baseline",
            "refresh trusted legacy default baseline grants",
        }
    )
    _MANAGED_STATIC_TOOL_IDS = frozenset(
        (*PRODUCTION_HTTP_TOOL_IDS, "knowledge.search")
    )

    def __init__(
        self,
        *,
        repository: AgentRepository,
        application_registry: ApplicationRegistry,
        model_config_service: ModelConfigService,
        capability_repository: CapabilityReader,
        prompt_reader: PromptVersionReader | None = None,
        knowledge_reader: KnowledgeVersionReader | None = None,
        model_snapshot_repository: RunModelBindingRepository | None = None,
    ) -> None:
        self._repository = repository
        self._applications = application_registry
        self._models = model_config_service
        self._capabilities = capability_repository
        self._prompts = prompt_reader
        self._knowledge = knowledge_reader
        self._model_snapshots = model_snapshot_repository

    async def create_agent(self, agent: AgentDefinition) -> AgentDefinition:
        if await self._applications.get_application(agent.app_id) is None:
            raise ResourceNotFound("application is not registered")
        return await self._repository.save_agent(agent)

    async def list_agents(self, app_id: str) -> list[AgentDefinition]:
        if await self._applications.get_application(app_id) is None:
            raise ResourceNotFound("application is not registered")
        return await self._repository.list_agents(app_id)

    async def get_agent(self, app_id: str, agent_id: str) -> AgentDefinition:
        agent = await self._repository.get_agent(app_id, agent_id)
        if agent is None:
            raise ResourceNotFound("agent is not registered")
        return agent

    async def set_default_agent(
        self,
        *,
        app_id: str,
        agent_id: str,
        expected_application_etag: int,
        changed_by: str,
        reason: str,
    ):
        await self.get_agent(app_id, agent_id)
        if await self._repository.get_active_release(app_id, agent_id) is None:
            raise RunStateConflict("default agent requires an active release")
        application = await self._applications.get_application(app_id)
        if application is None:
            raise ResourceNotFound("application is not registered")
        if application.etag != expected_application_etag:
            raise RunStateConflict("application etag mismatch")
        updated = application.model_copy(
            update={
                "default_agent_id": agent_id,
                "updated_at": datetime.now(UTC),
                "updated_by": changed_by,
                "last_reason": reason,
                "etag": application.etag + 1,
            }
        )
        return await self._applications.save_application(
            updated, expected_etag=expected_application_etag
        )

    async def ensure_legacy_baseline_release(
        self,
        *,
        app_id: str,
        agent_id: str,
        prompt_ref: str | None = None,
        knowledge_base_refs: tuple[str, ...] = (),
    ) -> AgentReleaseSnapshot | None:
        """Backfill one immutable, least-surprise release for a legacy default.

        Only already-published application grants are included. A model is
        selected only when the public pool is unambiguous, or when all other
        active Agent releases agree on one enabled primary model. Returning
        ``None`` means no trustworthy model can be inferred; callers must keep
        the legacy compatibility path instead of manufacturing a release.
        """

        await self.get_agent(app_id, agent_id)
        active = await self._repository.get_active_release(app_id, agent_id)
        application = await self._applications.get_application(app_id)
        if application is None:
            raise ResourceNotFound("application is not registered")
        if active is not None and not self._is_managed_legacy_release(
            active=active,
            app_id=app_id,
            agent_id=agent_id,
            default_agent_id=application.default_agent_id,
        ):
            return active
        inferred_model_id: str | None = None
        if active is None:
            inferred_model_id = await self._infer_legacy_model_id(app_id, agent_id)
            if inferred_model_id is None:
                return None

        refs: dict[str, list[str]] = {
            "tool": [],
            "skill": [],
            "workflow": [],
        }
        bindings = await self._applications.list_capability_bindings(
            app_id=app_id,
            enabled_only=True,
        )
        for binding in bindings:
            capability = await self._capabilities.get(
                binding.capability_id,
                binding.capability_version,
            )
            if capability is None or capability.status != "published":
                raise RunStateConflict(
                    "legacy baseline cannot verify application capability: "
                    f"{binding.capability_id}@{binding.capability_version}"
                )
            refs[capability.capability_type].append(
                f"{binding.capability_id}@{binding.capability_version}"
            )

        desired_capability_refs = tuple(sorted(refs["tool"]))
        desired_skill_refs = tuple(sorted(refs["skill"]))
        desired_workflow_refs = tuple(sorted(refs["workflow"]))
        if active is not None:
            managed_desired_refs = {
                reference
                for reference in desired_capability_refs
                if reference.rsplit("@", 1)[0] in self._MANAGED_STATIC_TOOL_IDS
            }
            unmanaged_current_refs = {
                reference
                for reference in active.capability_refs
                if reference.rsplit("@", 1)[0] not in self._MANAGED_STATIC_TOOL_IDS
            }
            desired_capability_refs = tuple(
                sorted((*managed_desired_refs, *unmanaged_current_refs))
            )
            desired_skill_refs = active.skill_refs
            desired_workflow_refs = active.workflow_refs
            if (
                active.capability_refs == desired_capability_refs
                and active.skill_refs == desired_skill_refs
                and active.workflow_refs == desired_workflow_refs
            ):
                return active
            ordered_models = sorted(active.model_refs, key=lambda item: item.order)
            primary = next(
                (item for item in ordered_models if item.role == "primary"),
                None,
            )
            if primary is None:
                raise RunStateConflict("managed legacy release has no primary model")
            model_policy = AgentModelPolicy(
                primary_model_config_id=primary.model_config_id,
                fallback_model_config_ids=tuple(
                    item.model_config_id
                    for item in ordered_models
                    if item.role == "fallback"
                ),
            )
            version_number = await self._next_legacy_baseline_version(
                app_id=app_id,
                agent_id=agent_id,
            )
            effective_prompt_ref = active.prompt_ref
            effective_knowledge_refs = active.knowledge_base_refs
            reason = "refresh trusted legacy default baseline grants"
        else:
            assert inferred_model_id is not None
            model_policy = AgentModelPolicy(
                primary_model_config_id=inferred_model_id
            )
            version_number = self._LEGACY_BASELINE_VERSION
            effective_prompt_ref = prompt_ref
            effective_knowledge_refs = tuple(sorted(knowledge_base_refs))
            reason = "backfill trusted legacy default baseline"

        version = AgentVersion(
            app_id=app_id,
            agent_id=agent_id,
            version=version_number,
            prompt_ref=effective_prompt_ref,
            capability_refs=desired_capability_refs,
            skill_refs=desired_skill_refs,
            workflow_refs=desired_workflow_refs,
            knowledge_base_refs=effective_knowledge_refs,
            execution_policy=(
                active.execution_policy
                if active is not None
                else AgentExecutionPolicy()
            ),
        )
        existing = await self._repository.get_version(
            app_id, agent_id, version.version
        )
        if existing is None:
            await self._repository.save_version(version)
        elif existing.model_dump(
            exclude={"status", "created_at", "updated_at", "etag"}
        ) != (
            version.model_dump(
                exclude={"status", "created_at", "updated_at", "etag"}
            )
        ):
            raise RunStateConflict("legacy baseline draft does not match current grants")
        await self._repository.save_model_policy(
            app_id,
            agent_id,
            version.version,
            model_policy,
        )
        return await self.publish_version(
            app_id=app_id,
            agent_id=agent_id,
            version=version.version,
            published_by=self._MANAGED_LEGACY_PUBLISHER,
            reason=reason,
        )

    def _is_managed_legacy_release(
        self,
        *,
        active: AgentReleaseSnapshot,
        app_id: str,
        agent_id: str,
        default_agent_id: str,
    ) -> bool:
        return (
            app_id == self._MANAGED_LEGACY_APP_ID
            and agent_id == self._MANAGED_LEGACY_AGENT_ID
            and default_agent_id == agent_id
            and active.published_by == self._MANAGED_LEGACY_PUBLISHER
            and active.reason in self._MANAGED_LEGACY_REASONS
            and active.agent_version.startswith("0.0.")
        )

    async def _next_legacy_baseline_version(
        self,
        *,
        app_id: str,
        agent_id: str,
    ) -> str:
        patches = [
            int(item.version.rsplit(".", 1)[1])
            for item in await self._repository.list_versions(app_id, agent_id)
            if item.version.startswith("0.0.")
        ]
        return f"0.0.{max(patches, default=0) + 1}"

    async def _infer_legacy_model_id(
        self,
        app_id: str,
        agent_id: str,
    ) -> str | None:
        enabled_models = {
            item.config_id: item
            for item in await self._models.list_configs()
            if item.is_enabled
        }
        released_primary_ids: set[str] = set()
        for agent in await self._repository.list_agents(app_id):
            if agent.agent_id == agent_id:
                continue
            release = await self._repository.get_active_release(
                app_id, agent.agent_id
            )
            if release is None:
                continue
            primary = next(
                (item for item in release.model_refs if item.role == "primary"),
                None,
            )
            if primary is not None and primary.model_config_id in enabled_models:
                released_primary_ids.add(primary.model_config_id)
        if len(released_primary_ids) == 1:
            return next(iter(released_primary_ids))
        if len(enabled_models) == 1:
            return next(iter(enabled_models))
        return None

    async def create_version(self, version: AgentVersion) -> AgentVersion:
        await self.get_agent(version.app_id, version.agent_id)
        if version.status != "draft":
            raise RunStateConflict("agent versions must be created as draft")
        return await self._repository.save_version(version)

    async def list_versions(self, app_id: str, agent_id: str) -> list[AgentVersion]:
        await self.get_agent(app_id, agent_id)
        return await self._repository.list_versions(app_id, agent_id)

    async def set_model_policy(
        self,
        *,
        app_id: str,
        agent_id: str,
        version: str,
        policy: AgentModelPolicy,
    ) -> AgentModelPolicy:
        draft = await self._require_version(app_id, agent_id, version)
        if draft.status != "draft":
            raise RunStateConflict("published agent versions are immutable")
        if policy.primary_model_config_id in policy.fallback_model_config_ids:
            raise RunStateConflict("primary model cannot also be a fallback")
        return await self._repository.save_model_policy(app_id, agent_id, version, policy)

    async def get_model_policy(self, app_id: str, agent_id: str, version: str) -> AgentModelPolicy:
        await self._require_version(app_id, agent_id, version)
        policy = await self._repository.get_model_policy(app_id, agent_id, version)
        if policy is None:
            raise ResourceNotFound("agent model policy not found")
        return policy

    async def validate_version(
        self, *, app_id: str, agent_id: str, version: str
    ) -> AgentValidationReport:
        draft = await self._require_version(app_id, agent_id, version)
        issues: list[AgentValidationIssue] = []
        for field, references in (
            ("capability_refs", draft.capability_refs),
            ("skill_refs", draft.skill_refs),
            ("workflow_refs", draft.workflow_refs),
            ("knowledge_base_refs", draft.knowledge_base_refs),
        ):
            resource_ids = [
                parsed[0]
                for reference in references
                if (parsed := _try_split_ref(reference)) is not None
            ]
            if len(resource_ids) != len(set(resource_ids)):
                issues.append(
                    AgentValidationIssue(
                        code="DUPLICATE_RESOURCE_REFERENCE",
                        field=field,
                        message="one resource id may only be referenced once",
                    )
                )
        policy = await self._repository.get_model_policy(app_id, agent_id, version)
        if policy is None:
            issues.append(
                AgentValidationIssue(
                    code="MODEL_POLICY_MISSING", field="model_policy", message="必须选择主模型"
                )
            )
        else:
            for model_id in (policy.primary_model_config_id, *policy.fallback_model_config_ids):
                try:
                    model_config = await self._models.get_config(model_id)
                except ResourceNotFound:
                    issues.append(
                        AgentValidationIssue(
                            code="MODEL_CONFIG_NOT_FOUND",
                            field="model_policy",
                            message=f"模型配置不存在：{model_id}",
                        )
                    )
                else:
                    if not model_config.is_enabled:
                        issues.append(
                            AgentValidationIssue(
                                code="MODEL_CONFIG_DISABLED",
                                field="model_policy",
                                message=f"模型配置已停用：{model_id}",
                            )
                        )
        for reference in draft.capability_refs:
            capability_id, capability_version = _split_ref(reference)
            capability = await self._capabilities.get(capability_id, capability_version)
            if (
                capability is None
                or capability.status != "published"
                or capability.capability_type != "tool"
            ):
                issues.append(
                    AgentValidationIssue(
                        code="CAPABILITY_NOT_PUBLISHED",
                        field="capability_refs",
                        message=f"能力未发布：{reference}",
                    )
                )
                continue
            authorized = {
                (item.capability_id, item.capability_version)
                for item in await self._applications.list_capability_bindings(
                    app_id=app_id,
                    enabled_only=True,
                )
            }
            if (capability_id, capability_version) not in authorized:
                issues.append(
                    AgentValidationIssue(
                        code="CAPABILITY_NOT_AUTHORIZED",
                        field="capability_refs",
                        message=f"应用未授权该能力：{reference}",
                    )
                )
        authorized = {
            (item.capability_id, item.capability_version)
            for item in await self._applications.list_capability_bindings(
                app_id=app_id,
                enabled_only=True,
            )
        }
        agent_tool_refs = {
            parsed
            for reference in draft.capability_refs
            if (parsed := _try_split_ref(reference)) is not None
        }
        agent_skill_refs = {
            parsed
            for reference in draft.skill_refs
            if (parsed := _try_split_ref(reference)) is not None
        }
        for references, capability_type, field, prefix in (
            (draft.skill_refs, "skill", "skill_refs", "SKILL"),
            (draft.workflow_refs, "workflow", "workflow_refs", "WORKFLOW"),
        ):
            for reference in references:
                parsed = _try_split_ref(reference)
                if parsed is None:
                    issues.append(_invalid_reference(field, reference))
                    continue
                capability_id, capability_version = parsed
                capability = await self._capabilities.get(
                    capability_id, capability_version
                )
                if (
                    capability is None
                    or capability.status != "published"
                    or capability.capability_type != capability_type
                ):
                    issues.append(
                        AgentValidationIssue(
                            code=f"{prefix}_NOT_PUBLISHED",
                            field=field,
                            message=f"referenced {capability_type} is not published: {reference}",
                        )
                    )
                    continue
                if (capability_id, capability_version) not in authorized:
                    issues.append(
                        AgentValidationIssue(
                            code=f"{prefix}_NOT_AUTHORIZED",
                            field=field,
                            message=f"application has not authorized: {reference}",
                        )
                    )
                    continue
                dependency_refs: set[tuple[str, str | None]] = set()
                if isinstance(capability, SkillCapability):
                    dependency_refs.update(
                        (tool_id, None) for tool_id in capability.allowed_tool_ids
                    )
                elif isinstance(capability, WorkflowCapability):
                    dependency_refs.update(
                        (node.tool_capability_id, node.tool_version)
                        for node in capability.nodes
                        if node.node_type == "tool"
                        and node.tool_capability_id is not None
                    )
                    for node in capability.nodes:
                        if (
                            node.node_type == "skill"
                            and node.skill_capability_id is not None
                            and node.skill_version is not None
                            and (
                                node.skill_capability_id,
                                node.skill_version,
                            )
                            not in agent_skill_refs
                        ):
                            issues.append(
                                AgentValidationIssue(
                                    code="DEPENDENCY_SKILL_NOT_REFERENCED",
                                    field=field,
                                    message=(
                                        f"{reference} depends on Agent Skill "
                                        f"{node.skill_capability_id}@{node.skill_version}"
                                    ),
                                )
                            )
                for tool_id, tool_version in dependency_refs:
                    dependency_present = (
                        any(item[0] == tool_id for item in agent_tool_refs)
                        if tool_version is None
                        else (tool_id, tool_version) in agent_tool_refs
                    )
                    if not dependency_present:
                        issues.append(
                            AgentValidationIssue(
                                code="DEPENDENCY_TOOL_NOT_REFERENCED",
                                field=field,
                                message=(
                                    f"{reference} depends on Agent tool "
                                    f"{tool_id}@{tool_version or 'published'}"
                                ),
                            )
                        )

        if draft.prompt_ref is not None:
            parsed = _try_split_ref(draft.prompt_ref)
            if parsed is None:
                issues.append(_invalid_reference("prompt_ref", draft.prompt_ref))
            elif self._prompts is None:
                issues.append(
                    AgentValidationIssue(
                        code="PROMPT_VALIDATOR_UNAVAILABLE",
                        field="prompt_ref",
                        message="prompt reference cannot be verified",
                    )
                )
            else:
                prompt_id, prompt_version = parsed
                prompt = await self._prompts.get_template(prompt_id, prompt_version)
                if (
                    prompt is None
                    or prompt.status != "published"
                    or prompt.app_id != app_id
                ):
                    issues.append(
                        AgentValidationIssue(
                            code="PROMPT_NOT_PUBLISHED",
                            field="prompt_ref",
                            message=(
                                "application-scoped prompt is not published: "
                                f"{draft.prompt_ref}"
                            ),
                        )
                    )
                elif prompt.layer != "agent":
                    issues.append(
                        AgentValidationIssue(
                            code="PROMPT_LAYER_INVALID",
                            field="prompt_ref",
                            message=(
                                "Agent prompt_ref must reference an exact published "
                                f"agent-layer prompt: {draft.prompt_ref}"
                            ),
                        )
                    )

        for reference in draft.knowledge_base_refs:
            parsed = _try_split_ref(reference)
            if parsed is None:
                issues.append(_invalid_reference("knowledge_base_refs", reference))
                continue
            knowledge_base_id, semantic_version = parsed
            knowledge_version = _knowledge_version(semantic_version)
            if knowledge_version is None:
                issues.append(_invalid_reference("knowledge_base_refs", reference))
            elif self._knowledge is None:
                issues.append(
                    AgentValidationIssue(
                        code="KNOWLEDGE_VALIDATOR_UNAVAILABLE",
                        field="knowledge_base_refs",
                        message="knowledge reference cannot be verified",
                    )
                )
            elif not await self._knowledge.is_ready_version(
                app_id=app_id,
                knowledge_base_id=knowledge_base_id,
                version=knowledge_version,
            ):
                issues.append(
                    AgentValidationIssue(
                        code="KNOWLEDGE_NOT_READY",
                        field="knowledge_base_refs",
                        message=f"application-scoped knowledge version is not ready: {reference}",
                    )
                )
        return AgentValidationReport(is_valid=not issues, issues=tuple(issues))

    async def publish_version(
        self,
        *,
        app_id: str,
        agent_id: str,
        version: str,
        published_by: str,
        reason: str,
    ) -> AgentReleaseSnapshot:
        draft = await self._require_version(app_id, agent_id, version)
        existing = await self._repository.get_active_release(app_id, agent_id)
        if existing is not None and existing.agent_version == version:
            return existing
        report = await self.validate_version(app_id=app_id, agent_id=agent_id, version=version)
        if not report.is_valid:
            raise RunStateConflict(
                "agent version is incomplete: " + ",".join(issue.code for issue in report.issues)
            )
        policy = await self.get_model_policy(app_id, agent_id, version)
        ordered_ids = (policy.primary_model_config_id, *policy.fallback_model_config_ids)
        model_refs: list[AgentModelVersionRef] = []
        for index, model_id in enumerate(ordered_ids):
            config = await self._models.get_config(model_id)
            if self._model_snapshots is not None:
                snapshot = await self._models.capture_snapshot_by_id(
                    model_id, config.version
                )
                if snapshot is None:
                    raise RunStateConflict(
                        f"model version cannot be snapshotted: {model_id}@{config.version}"
                    )
                await self._model_snapshots.store_snapshot(snapshot)
            model_refs.append(
                AgentModelVersionRef(
                    model_config_id=model_id,
                    config_version=config.version,
                    role="primary" if index == 0 else "fallback",
                    order=index,
                )
            )
        now = datetime.now(UTC)
        published = draft.model_copy(
            update={"status": "published", "updated_at": now, "etag": draft.etag + 1}
        )
        release = AgentReleaseSnapshot(
            release_id=new_id("arel"),
            app_id=app_id,
            agent_id=agent_id,
            agent_version=version,
            prompt_ref=draft.prompt_ref,
            capability_refs=draft.capability_refs,
            skill_refs=draft.skill_refs,
            workflow_refs=draft.workflow_refs,
            knowledge_base_refs=draft.knowledge_base_refs,
            execution_policy=draft.execution_policy,
            model_refs=tuple(model_refs),
            published_at=now,
            published_by=published_by,
            reason=reason,
        )
        return await self._repository.publish(published, release)

    async def get_active_release(self, app_id: str, agent_id: str) -> AgentReleaseSnapshot:
        release = await self._repository.get_active_release(app_id, agent_id)
        if release is None:
            raise ResourceNotFound("agent has no active release")
        return release

    async def bind_run(
        self,
        *,
        run_id: str,
        app_id: str,
        agent_id: str | None = None,
        tenant_id: str = "legacy",
    ) -> RunAgentReleaseSnapshot:
        existing = await self._repository.get_run_snapshot(run_id)
        if existing is not None:
            return existing
        if agent_id is None:
            application = await self._applications.get_application(app_id)
            if application is None:
                raise ResourceNotFound("application is not registered")
            agent_id = application.default_agent_id
            if await self._repository.get_agent(app_id, agent_id) is None:
                raise ResourceNotFound("default agent is not registered")
        else:
            await self.get_agent(app_id, agent_id)
        release = await self.get_active_release(app_id, agent_id)
        snapshot = RunAgentReleaseSnapshot(
            **release.model_dump(mode="python"),
            run_id=run_id,
            tenant_id=tenant_id,
            bound_at=datetime.now(UTC),
        )
        return await self._repository.bind_run(snapshot)

    async def get_run_snapshot(self, run_id: str) -> RunAgentReleaseSnapshot:
        snapshot = await self._repository.get_run_snapshot(run_id)
        if snapshot is None:
            raise ResourceNotFound("run agent release snapshot not found")
        return snapshot

    async def _require_version(self, app_id: str, agent_id: str, version: str) -> AgentVersion:
        item = await self._repository.get_version(app_id, agent_id, version)
        if item is None:
            raise ResourceNotFound("agent version not found")
        return item


def _split_ref(reference: str) -> tuple[str, str]:
    if "@" not in reference:
        raise RunStateConflict(f"versioned reference required: {reference}")
    return tuple(reference.rsplit("@", 1))  # type: ignore[return-value]


def _try_split_ref(reference: str) -> tuple[str, str] | None:
    if "@" not in reference:
        return None
    resource_id, version = reference.rsplit("@", 1)
    parts = version.split(".")
    if not resource_id or len(parts) != 3 or not all(part.isdigit() for part in parts):
        return None
    return resource_id, version


def _knowledge_version(semantic_version: str) -> int | None:
    major, minor, patch = semantic_version.split(".")
    if minor != "0" or patch != "0" or int(major) < 1:
        return None
    return int(major)


def _invalid_reference(field: str, reference: str) -> AgentValidationIssue:
    return AgentValidationIssue(
        code="INVALID_VERSIONED_REFERENCE",
        field=field,
        message=f"exact semantic version reference required: {reference}",
    )
