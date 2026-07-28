"""Record semantic authorization denials in the canonical refusal ledger."""

from dataclasses import dataclass

from pydantic import BaseModel, ValidationError

from full_view_agent.application.capability_service import (
    TOOL_INPUT_MODELS,
    DenialLedger,
    PolicyEvaluator,
)
from full_view_agent.application.errors import ResourceNotFound
from full_view_agent.application.tool_registry import ToolRegistry
from full_view_agent.domain.models import (
    AuthContext,
    InternalToolManifest,
    PolicyDecision,
)
from full_view_agent.semantic.action_resolver import SemanticQueryInput
from full_view_agent.semantic.catalog import SemanticCatalog
from full_view_agent.semantic.compiler import SemanticCompiler
from full_view_agent.semantic.errors import SemanticKernelError, SemanticQueryRejected


@dataclass(frozen=True)
class CanonicalDenialTarget:
    manifest: InternalToolManifest
    arguments: BaseModel


class SemanticDenialRecorder:
    """Derive the real capability scope and reuse production Policy + ledger."""

    def __init__(
        self,
        *,
        catalog: SemanticCatalog,
        registry: ToolRegistry,
        policy: PolicyEvaluator,
        ledger: DenialLedger,
    ) -> None:
        self._catalog = catalog
        self._registry = registry
        self._policy = policy
        self._ledger = ledger
        self._compiler = SemanticCompiler(catalog)

    async def record_if_denied(
        self,
        raw_arguments: dict[str, object],
        *,
        auth_context: AuthContext,
    ) -> PolicyDecision | None:
        target = self._derive_target(raw_arguments)
        if target is None:
            return None
        decision = self._policy.evaluate(
            manifest=target.manifest,
            auth_context=auth_context,
            arguments=target.arguments,
        )
        if decision.decision != "deny":
            return None
        await self._ledger.record(
            manifest=target.manifest,
            arguments=target.arguments,
            auth_context=auth_context,
            decision=decision,
        )
        return decision

    def _derive_target(
        self, raw_arguments: dict[str, object]
    ) -> CanonicalDenialTarget | None:
        try:
            parsed = SemanticQueryInput.model_validate(raw_arguments)
        except ValidationError:
            return None
        if parsed.catalog_version != self._catalog.catalog_version:
            return None
        if parsed.catalog_fingerprint != self._catalog.execution_fingerprint:
            return None
        try:
            plan = self._compiler.compile(parsed.spec, authorization=None)
        except (SemanticQueryRejected, SemanticKernelError):
            return None
        if len(plan.steps) != 1:
            return None
        step = plan.steps[0]
        input_model = TOOL_INPUT_MODELS.get(step.capability_id)
        if input_model is None:
            return None
        try:
            arguments = input_model.model_validate(step.arguments)
            manifest = self._registry.get_manifest(step.capability_id)
        except (ValidationError, ResourceNotFound):
            return None
        if (
            manifest.tool_version != step.capability_version
            or manifest.dataset_id != plan.logical_dataset_id
        ):
            return None
        return CanonicalDenialTarget(manifest=manifest, arguments=arguments)
