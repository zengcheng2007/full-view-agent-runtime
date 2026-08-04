"""Production execution port for checkpointed regional analysis graphs."""

from __future__ import annotations

from typing import Literal, cast

from full_view_agent.application.analysis_graph import (
    AnalysisGraphPreparation,
    AnalysisReduction,
    AnalysisRunOutcome,
    AnalysisStepCheckpoint,
)
from full_view_agent.application.analysis_plan_repository import AnalysisPlanRepository
from full_view_agent.application.analysis_planner import AnalysisPlanner
from full_view_agent.application.analysis_report import AnalysisReportAssembler
from full_view_agent.application.analysis_run_binding import AnalysisRunBindingStore
from full_view_agent.application.analysis_semantic_spec import AnalysisSemanticSpecFactory
from full_view_agent.application.analysis_step_ledger import (
    AnalysisStepLedgerEntry,
    AnalysisStepLedgerStore,
    analysis_step_tool_call_id,
)
from full_view_agent.application.errors import (
    ReauthenticationRequired,
    ResourceNotFound,
    RunStateConflict,
)
from full_view_agent.application.fingerprints import canonical_fingerprint
from full_view_agent.application.harness import AgentHarness, HarnessLimits, ToolAction
from full_view_agent.application.ports import AgentStore
from full_view_agent.application.semantic_executor import SemanticToolExecutor
from full_view_agent.application.tool_observation_service import (
    ToolObservationService,
    durable_tool_result_id,
)
from full_view_agent.application.trusted_analysis_plan import TrustedAnalysisPlanLoader
from full_view_agent.domain.analysis_execution import (
    AnalysisExecutionResult,
    AnalysisExecutionStatus,
    AnalysisStepExecution,
)
from full_view_agent.domain.analysis_plan import AnalysisPlan, AnalysisStep
from full_view_agent.domain.analysis_report import AnalysisReportDataResult
from full_view_agent.domain.models import AuthContext, TableDataResult, ToolResult
from full_view_agent.semantic.action_resolver import (
    SEMANTIC_QUERY_TOOL_ID,
    ResolvedSemanticAction,
    SemanticActionResolver,
)
from full_view_agent.semantic.catalog import SemanticCatalog


class AnalysisGraphExecutionService:
    """Execute one trusted analysis step at a time through the common Harness."""

    def __init__(
        self,
        *,
        catalog: SemanticCatalog,
        planner: AnalysisPlanner,
        plan_repository: AnalysisPlanRepository,
        resolver: SemanticActionResolver,
        semantic_executor: SemanticToolExecutor,
        result_store: AgentStore,
        observation_service: ToolObservationService,
        binding_store: AnalysisRunBindingStore,
        step_ledger: AnalysisStepLedgerStore,
    ) -> None:
        if resolver.catalog is not catalog:
            raise ValueError("analysis execution service must share the catalog")
        if semantic_executor.resolver is not resolver:
            raise ValueError("analysis execution service must share the resolver")
        self._catalog = catalog
        self._resolver = resolver
        self._semantic_executor = semantic_executor
        self._result_store = result_store
        self._observations = observation_service
        self._bindings = binding_store
        self._steps = step_ledger
        self._loader = TrustedAnalysisPlanLoader(
            catalog=catalog, planner=planner, repository=plan_repository
        )
        self._specs = AnalysisSemanticSpecFactory(catalog)
        self._reports = AnalysisReportAssembler(
            catalog=catalog,
            planner=planner,
            plan_repository=plan_repository,
            resolver=resolver,
            result_store=result_store,
        )

    async def prepare(
        self,
        *,
        analysis_run_id: str,
        plan_id: str,
        request_id: str,
        auth_context: AuthContext,
    ) -> AnalysisGraphPreparation:
        self._require_run(analysis_run_id, auth_context)
        plan = await self._load(plan_id, request_id, auth_context)
        fingerprint = self._invocation_fingerprint(plan, auth_context)
        binding = await self._bindings.ensure_binding(
            tenant_id=auth_context.principal.tenant_id,
            user_id=auth_context.principal.user_id,
            session_id=auth_context.session_id,
            run_id=analysis_run_id,
            plan_id=plan.plan_id,
            request_id=plan.request_id,
            invocation_fingerprint=fingerprint,
        )
        if binding.status == "pending":
            await self._bindings.update_binding(
                tenant_id=binding.tenant_id,
                user_id=binding.user_id,
                run_id=binding.run_id,
                invocation_fingerprint=fingerprint,
                expected_version=binding.version,
                status="running",
                report_result_id=None,
            )
        elif binding.status not in {"running", "waiting_input"}:
            raise RunStateConflict("analysis run binding is already terminal")
        return AnalysisGraphPreparation(
            expected_step_count=len(plan.steps),
            max_parallel=plan.constraints.max_parallel,
            max_tool_calls=plan.constraints.max_tool_calls,
            total_timeout_ms=plan.constraints.total_timeout_ms,
        )

    async def reduce(
        self,
        *,
        analysis_run_id: str,
        plan_id: str,
        request_id: str,
        completed: tuple[AnalysisStepCheckpoint, ...],
        tool_call_count: int,
        deadline_exceeded: bool,
        auth_context: AuthContext,
    ) -> AnalysisReduction:
        self._require_run(analysis_run_id, auth_context)
        plan = await self._load(plan_id, request_id, auth_context)
        fingerprint = await self._require_binding(plan, auth_context)
        completed_by_id = self._validated_completed(plan, completed)
        pending = [step for step in plan.steps if step.step_id not in completed_by_id]
        if not pending:
            return AnalysisReduction(terminal=True)

        additions: list[AnalysisStepCheckpoint] = []
        if deadline_exceeded or tool_call_count >= plan.constraints.max_tool_calls:
            reason = (
                "ANALYSIS_DEADLINE_EXCEEDED"
                if deadline_exceeded
                else "ANALYSIS_TOOL_BUDGET_EXHAUSTED"
            )
            for step in pending:
                additions.append(
                    AnalysisStepCheckpoint(
                        step_id=step.step_id,
                        status="timeout" if deadline_exceeded else "skipped",
                        reason_code=reason,
                        tool_call_consumed=False,
                    )
                )
            await self._persist_synthetic_checkpoints(
                plan=plan,
                checkpoints=tuple(additions),
                invocation_fingerprint=fingerprint,
                auth_context=auth_context,
            )
            return AnalysisReduction(additions=tuple(additions), terminal=True)

        ready: list[str] = []
        for step in pending:
            dependencies = [completed_by_id.get(item) for item in step.depends_on]
            if any(
                item is not None
                and item.status not in {"success", "partial"}
                for item in dependencies
            ):
                additions.append(
                    AnalysisStepCheckpoint(
                        step_id=step.step_id,
                        status="skipped",
                        reason_code="DEPENDENCY_NOT_USABLE",
                        tool_call_consumed=False,
                    )
                )
            elif all(item is not None for item in dependencies):
                ready.append(step.step_id)
        if additions:
            await self._persist_synthetic_checkpoints(
                plan=plan,
                checkpoints=tuple(additions),
                invocation_fingerprint=fingerprint,
                auth_context=auth_context,
            )
            return AnalysisReduction(additions=tuple(additions), terminal=False)
        if not ready:
            raise RunStateConflict("analysis graph cannot make deterministic progress")
        remaining_calls = plan.constraints.max_tool_calls - tool_call_count
        return AnalysisReduction(
            ready_step_ids=tuple(
                ready[: min(plan.constraints.max_parallel, remaining_calls)]
            ),
            terminal=False,
        )

    async def execute_step(
        self,
        *,
        analysis_run_id: str,
        plan_id: str,
        request_id: str,
        step_id: str,
        auth_context: AuthContext,
    ) -> AnalysisStepCheckpoint:
        self._require_run(analysis_run_id, auth_context)
        plan = await self._load(plan_id, request_id, auth_context)
        fingerprint = await self._require_binding(plan, auth_context)
        step = next((item for item in plan.steps if item.step_id == step_id), None)
        if step is None:
            raise RunStateConflict("analysis step is absent from the trusted plan")
        tool_call_id = analysis_step_tool_call_id(
            tenant_id=auth_context.principal.tenant_id,
            run_id=analysis_run_id,
            plan_id=plan.plan_id,
            step_id=step.step_id,
        )
        ledger = await self._steps.reserve_step(
            tenant_id=auth_context.principal.tenant_id,
            user_id=auth_context.principal.user_id,
            run_id=analysis_run_id,
            plan_id=plan.plan_id,
            request_id=plan.request_id,
            step_id=step.step_id,
            tool_call_id=tool_call_id,
            invocation_fingerprint=fingerprint,
        )
        if ledger.status == "persisted":
            return await self._checkpoint_from_persisted(step, ledger, auth_context)
        if ledger.status in {"failed", "indeterminate"}:
            if ledger.status == "failed":
                if ledger.result_status not in {"denied", "failed"}:
                    raise RunStateConflict("failed analysis step has no terminal status")
                terminal_status = ledger.result_status
            else:
                terminal_status = "failed"
            return AnalysisStepCheckpoint(
                step_id=step.step_id,
                status=terminal_status,
                reason_code=(
                    "STEP_EXECUTION_INDETERMINATE"
                    if ledger.status == "indeterminate"
                    else ledger.reason_code or "STEP_EXECUTION_FAILED"
                ),
                tool_call_consumed=True,
            )
        if ledger.status == "synthetic":
            if ledger.result_status not in {"skipped", "timeout"}:
                raise RunStateConflict("synthetic analysis step has no terminal status")
            return AnalysisStepCheckpoint(
                step_id=step.step_id,
                status=ledger.result_status,
                reason_code=ledger.reason_code or "STEP_SYNTHETIC",
                tool_call_consumed=False,
            )
        if ledger.status == "observed":
            recovered = await self._recover_observation(step, ledger, auth_context)
            if recovered is not None:
                return recovered
            indeterminate = await self._steps.mark_indeterminate_if_unfinished(
                tenant_id=ledger.tenant_id,
                user_id=ledger.user_id,
                run_id=ledger.run_id,
                step_id=ledger.step_id,
                invocation_fingerprint=ledger.invocation_fingerprint,
                expected_version=ledger.version,
            )
            return AnalysisStepCheckpoint(
                step_id=step.step_id,
                status="failed",
                reason_code="STEP_EXECUTION_INDETERMINATE",
                tool_call_consumed=indeterminate.status == "indeterminate",
            )
        if ledger.status == "executing":
            indeterminate = await self._steps.mark_indeterminate_if_unfinished(
                tenant_id=ledger.tenant_id,
                user_id=ledger.user_id,
                run_id=ledger.run_id,
                step_id=ledger.step_id,
                invocation_fingerprint=ledger.invocation_fingerprint,
                expected_version=ledger.version,
            )
            return AnalysisStepCheckpoint(
                step_id=step.step_id,
                status="failed",
                reason_code="STEP_EXECUTION_INDETERMINATE",
                tool_call_consumed=indeterminate.status == "indeterminate",
            )

        action = self._action(plan, step, auth_context)
        harness = AgentHarness(
            tool_executor=self._semantic_executor,
            limits=HarnessLimits(max_tool_calls=1, max_model_turns=1),
            tool_call_id_factory=lambda: tool_call_id,
        )
        executing = await self._steps.transition_step(
            tenant_id=ledger.tenant_id,
            user_id=ledger.user_id,
            run_id=ledger.run_id,
            step_id=ledger.step_id,
            invocation_fingerprint=ledger.invocation_fingerprint,
            expected_version=ledger.version,
            status="executing",
            result_id=None,
            evidence_ids=(),
        )
        control = harness.begin()
        try:
            execution = await harness.authorize_and_execute_once(
                action=action,
                auth_context=auth_context,
                control=control,
            )
            harness.observe_once(execution=execution, control=control)
        except ReauthenticationRequired:
            await self._steps.transition_step(
                tenant_id=executing.tenant_id,
                user_id=executing.user_id,
                run_id=executing.run_id,
                step_id=executing.step_id,
                invocation_fingerprint=executing.invocation_fingerprint,
                expected_version=executing.version,
                status="waiting_reauth",
                result_id=None,
                evidence_ids=(),
            )
            raise
        except Exception:
            await self._steps.transition_step(
                tenant_id=executing.tenant_id,
                user_id=executing.user_id,
                run_id=executing.run_id,
                step_id=executing.step_id,
                invocation_fingerprint=executing.invocation_fingerprint,
                expected_version=executing.version,
                status="failed",
                result_status="failed",
                reason_code="STEP_EXECUTION_ERROR",
                result_id=None,
                evidence_ids=(),
            )
            return AnalysisStepCheckpoint(
                step_id=step.step_id,
                status="failed",
                reason_code="STEP_EXECUTION_ERROR",
            )
        result = execution.result
        if result.status not in {"success", "partial"} or result.data_result is None:
            failure_status = cast(Literal["denied", "failed"], result.status)
            failure_reason = result.warnings[0] if result.warnings else "STEP_FAILED"
            failed = await self._steps.transition_step(
                tenant_id=executing.tenant_id,
                user_id=executing.user_id,
                run_id=executing.run_id,
                step_id=executing.step_id,
                invocation_fingerprint=executing.invocation_fingerprint,
                expected_version=executing.version,
                status="failed",
                result_status=failure_status,
                reason_code=failure_reason,
                result_id=None,
                evidence_ids=(),
            )
            return AnalysisStepCheckpoint(
                step_id=step.step_id,
                status=failure_status,
                reason_code=failure_reason,
                tool_call_consumed=failed.status == "failed",
            )
        usable_status = cast(Literal["success", "partial"], result.status)
        usable_reason = (
            result.warnings[0]
            if result.warnings
            else "STEP_SUCCEEDED" if usable_status == "success" else "STEP_PARTIAL"
        )
        observed = await self._steps.transition_step(
            tenant_id=executing.tenant_id,
            user_id=executing.user_id,
            run_id=executing.run_id,
            step_id=executing.step_id,
            invocation_fingerprint=executing.invocation_fingerprint,
            expected_version=executing.version,
            status="observed",
            result_status=usable_status,
            reason_code=usable_reason,
            result_id=None,
            evidence_ids=(),
        )
        persisted = await self._observations.persist(
            user_id=auth_context.principal.user_id,
            run=await self._result_store.get_run(
                user_id=auth_context.principal.user_id, run_id=analysis_run_id
            ),
            action=action,
            tool_result=result,
        )
        terminal = await self._steps.transition_step(
            tenant_id=observed.tenant_id,
            user_id=observed.user_id,
            run_id=observed.run_id,
            step_id=observed.step_id,
            invocation_fingerprint=observed.invocation_fingerprint,
            expected_version=observed.version,
            status="persisted",
            result_status=observed.result_status,
            reason_code=observed.reason_code,
            result_id=persisted.data_result.result_id,
            evidence_ids=(persisted.evidence.evidence_id,),
        )
        return AnalysisStepCheckpoint(
            step_id=step.step_id,
            status=usable_status,
            reason_code=usable_reason,
            result_id=terminal.result_id,
            evidence_ids=terminal.evidence_ids,
        )

    async def finalize(
        self,
        *,
        analysis_run_id: str,
        plan_id: str,
        request_id: str,
        completed: tuple[AnalysisStepCheckpoint, ...],
        auth_context: AuthContext,
    ) -> AnalysisRunOutcome:
        self._require_run(analysis_run_id, auth_context)
        plan = await self._load(plan_id, request_id, auth_context)
        fingerprint = await self._require_binding(plan, auth_context)
        completed_by_id = self._validated_completed(plan, completed)
        if len(completed_by_id) != len(plan.steps):
            raise RunStateConflict("analysis cannot finalize with unfinished steps")
        await self._validate_step_attestations(
            plan=plan,
            completed=completed_by_id,
            invocation_fingerprint=fingerprint,
            auth_context=auth_context,
        )
        status = self._overall_status(tuple(completed_by_id.values()), bool(plan.omissions))
        binding = await self._bindings.get_binding(
            tenant_id=auth_context.principal.tenant_id,
            user_id=auth_context.principal.user_id,
            run_id=analysis_run_id,
            invocation_fingerprint=fingerprint,
        )
        if binding.status in {"completed", "partial", "failed"}:
            terminal_status = cast(AnalysisExecutionStatus, binding.status)
            return AnalysisRunOutcome(
                analysis_run_id=analysis_run_id,
                plan_id=plan.plan_id,
                request_id=plan.request_id,
                status=terminal_status,
                reason_code=f"ANALYSIS_{terminal_status.upper()}",
                report_result_id=binding.report_result_id,
            )
        reason_code = f"ANALYSIS_{status.upper()}"
        if status == "failed":
            await self._bindings.update_binding(
                tenant_id=binding.tenant_id,
                user_id=binding.user_id,
                run_id=binding.run_id,
                invocation_fingerprint=fingerprint,
                expected_version=binding.version,
                status="failed",
                report_result_id=None,
            )
            return AnalysisRunOutcome(
                analysis_run_id=analysis_run_id,
                plan_id=plan.plan_id,
                request_id=plan.request_id,
                status="failed",
                reason_code=reason_code,
            )
        execution = await self._execution_result(plan, completed_by_id, auth_context)
        report = await self._reports.assemble_and_save(
            plan_id=plan.plan_id,
            request_id=plan.request_id,
            auth_context=auth_context,
            execution=execution,
        )
        stored = await self._result_store.get_result_for_run(
            user_id=auth_context.principal.user_id,
            run_id=analysis_run_id,
            result_id=report.result_id,
        )
        if not isinstance(stored, AnalysisReportDataResult) or stored != report:
            raise RunStateConflict("analysis report is not bound to this run")
        await self._bindings.update_binding(
            tenant_id=binding.tenant_id,
            user_id=binding.user_id,
            run_id=binding.run_id,
            invocation_fingerprint=fingerprint,
            expected_version=binding.version,
            status=cast(AnalysisExecutionStatus, status),
            report_result_id=report.result_id,
        )
        return AnalysisRunOutcome(
            analysis_run_id=analysis_run_id,
            plan_id=plan.plan_id,
            request_id=plan.request_id,
            status=cast(AnalysisExecutionStatus, status),
            reason_code=reason_code,
            report_result_id=report.result_id,
        )

    async def _load(
        self, plan_id: str, request_id: str, auth_context: AuthContext
    ) -> AnalysisPlan:
        return await self._loader.load(
            plan_id=plan_id, request_id=request_id, auth_context=auth_context
        )

    async def _require_binding(
        self, plan: AnalysisPlan, auth_context: AuthContext
    ) -> str:
        fingerprint = self._invocation_fingerprint(plan, auth_context)
        binding = await self._bindings.get_binding(
            tenant_id=auth_context.principal.tenant_id,
            user_id=auth_context.principal.user_id,
            run_id=auth_context.run_id,
            invocation_fingerprint=fingerprint,
        )
        if binding.plan_id != plan.plan_id or binding.request_id != plan.request_id:
            raise RunStateConflict("analysis binding no longer matches the trusted plan")
        return fingerprint

    def _action(
        self, plan: AnalysisPlan, step: AnalysisStep, auth_context: AuthContext
    ) -> ToolAction:
        spec = self._specs.build(plan, step)
        raw = {
            "catalog_version": plan.catalog_version,
            "catalog_fingerprint": plan.catalog_fingerprint,
            "spec": spec.model_dump(mode="json"),
        }
        resolution = self._resolver.compile_action(raw, auth_context=auth_context)
        if not isinstance(resolution, ResolvedSemanticAction):
            raise RunStateConflict("trusted analysis step no longer resolves")
        return ToolAction(tool_id=SEMANTIC_QUERY_TOOL_ID, arguments=raw)

    async def _recover_observation(
        self,
        step: AnalysisStep,
        ledger: AnalysisStepLedgerEntry,
        auth_context: AuthContext,
    ) -> AnalysisStepCheckpoint | None:
        if ledger.result_status is None:
            raise RunStateConflict("observed analysis step has no outcome status")
        if ledger.reason_code is None:
            raise RunStateConflict("observed analysis step has no reason code")
        result_id = durable_tool_result_id(
            run_id=ledger.run_id, tool_call_id=ledger.tool_call_id
        )
        try:
            result = await self._result_store.get_result_for_run(
                user_id=auth_context.principal.user_id,
                run_id=ledger.run_id,
                result_id=result_id,
            )
        except ResourceNotFound:
            return None
        if not result.evidence_ids:
            raise RunStateConflict("recovered analysis result has no evidence")
        persisted = await self._steps.transition_step(
            tenant_id=ledger.tenant_id,
            user_id=ledger.user_id,
            run_id=ledger.run_id,
            step_id=ledger.step_id,
            invocation_fingerprint=ledger.invocation_fingerprint,
            expected_version=ledger.version,
            status="persisted",
            result_status=ledger.result_status,
            reason_code=ledger.reason_code,
            result_id=result.result_id,
            evidence_ids=tuple(result.evidence_ids),
        )
        return AnalysisStepCheckpoint(
            step_id=step.step_id,
            status=ledger.result_status,
            reason_code=ledger.reason_code,
            result_id=persisted.result_id,
            evidence_ids=persisted.evidence_ids,
        )

    async def _checkpoint_from_persisted(
        self,
        step: AnalysisStep,
        ledger: AnalysisStepLedgerEntry,
        auth_context: AuthContext,
    ) -> AnalysisStepCheckpoint:
        if ledger.result_id is None:
            raise RunStateConflict("persisted analysis step has no result")
        if ledger.result_status is None:
            raise RunStateConflict("persisted analysis step has no outcome status")
        if ledger.reason_code is None:
            raise RunStateConflict("persisted analysis step has no reason code")
        await self._result_store.get_result_for_run(
            user_id=auth_context.principal.user_id,
            run_id=ledger.run_id,
            result_id=ledger.result_id,
        )
        return AnalysisStepCheckpoint(
            step_id=step.step_id,
            status=ledger.result_status,
            reason_code=ledger.reason_code,
            result_id=ledger.result_id,
            evidence_ids=ledger.evidence_ids,
        )

    async def _execution_result(
        self,
        plan: AnalysisPlan,
        completed: dict[str, AnalysisStepCheckpoint],
        auth_context: AuthContext,
    ) -> AnalysisExecutionResult:
        steps: list[AnalysisStepExecution] = []
        for planned in plan.steps:
            item = completed[planned.step_id]
            tool_result: ToolResult | None = None
            if item.status in {"success", "partial"}:
                if item.result_id is None:
                    raise RunStateConflict("usable analysis step has no result")
                result = await self._result_store.get_result_for_run(
                    user_id=auth_context.principal.user_id,
                    run_id=auth_context.run_id,
                    result_id=item.result_id,
                )
                if not isinstance(result, TableDataResult):
                    raise RunStateConflict("analysis child result must be a table")
                action = self._action(plan, planned, auth_context)
                resolution = self._resolver.compile_action(
                    action.arguments, auth_context=auth_context
                )
                if not isinstance(resolution, ResolvedSemanticAction):
                    raise RunStateConflict("analysis child lineage cannot be rebuilt")
                tool_status: Literal["success", "partial"] = (
                    "success" if item.status == "success" else "partial"
                )
                tool_result = ToolResult(
                    tool_call_id=analysis_step_tool_call_id(
                        tenant_id=auth_context.principal.tenant_id,
                        run_id=auth_context.run_id,
                        plan_id=plan.plan_id,
                        step_id=planned.step_id,
                    ),
                    tool_id=resolution.plan.steps[0].capability_id,
                    tool_version=resolution.plan.steps[0].capability_version,
                    status=tool_status,
                    summary=item.reason_code,
                    data_result=result,
                    semantic_lineage=resolution.lineage,
                )
            steps.append(
                AnalysisStepExecution(
                    step_id=planned.step_id,
                    subject=planned.subject,
                    status=item.status,
                    reason_code=item.reason_code,
                    detail=item.reason_code,
                    tool_result=tool_result,
                )
            )
        status = self._overall_status(tuple(completed.values()), bool(plan.omissions))
        return AnalysisExecutionResult(
            plan_id=plan.plan_id,
            request_id=plan.request_id,
            status=status,
            reason_code=f"ANALYSIS_{status.upper()}",
            steps=tuple(steps),
            omissions=plan.omissions,
            tool_call_count=sum(item.tool_call_consumed for item in completed.values()),
        )

    async def _validate_step_attestations(
        self,
        *,
        plan: AnalysisPlan,
        completed: dict[str, AnalysisStepCheckpoint],
        invocation_fingerprint: str,
        auth_context: AuthContext,
    ) -> None:
        """Reject caller checkpoints that are not backed by the durable step ledger."""
        for step in plan.steps:
            checkpoint = completed[step.step_id]
            try:
                ledger = await self._steps.get_step(
                    tenant_id=auth_context.principal.tenant_id,
                    user_id=auth_context.principal.user_id,
                    run_id=auth_context.run_id,
                    step_id=step.step_id,
                    invocation_fingerprint=invocation_fingerprint,
                )
            except ResourceNotFound:
                raise RunStateConflict(
                    "analysis checkpoint has no durable step attestation"
                ) from None
            if ledger.status == "persisted":
                if (
                    checkpoint.status != ledger.result_status
                    or checkpoint.result_id != ledger.result_id
                    or checkpoint.evidence_ids != ledger.evidence_ids
                ):
                    raise RunStateConflict(
                        "analysis checkpoint differs from its durable step attestation"
                    )
            elif ledger.status == "failed":
                if (
                    checkpoint.status != ledger.result_status
                    or checkpoint.reason_code != ledger.reason_code
                    or checkpoint.result_id is not None
                    or checkpoint.evidence_ids
                ):
                    raise RunStateConflict(
                        "failed analysis checkpoint differs from its step ledger"
                    )
            elif ledger.status == "indeterminate":
                if (
                    checkpoint.status != "failed"
                    or checkpoint.reason_code != "STEP_EXECUTION_INDETERMINATE"
                    or checkpoint.result_id is not None
                    or checkpoint.evidence_ids
                ):
                    raise RunStateConflict(
                        "indeterminate checkpoint differs from its step ledger"
                    )
            elif ledger.status == "synthetic":
                if (
                    checkpoint.status != ledger.result_status
                    or checkpoint.reason_code != ledger.reason_code
                    or checkpoint.result_id is not None
                    or checkpoint.evidence_ids
                ):
                    raise RunStateConflict(
                        "synthetic checkpoint differs from its step ledger"
                    )
            else:
                raise RunStateConflict("analysis step is not terminal")

    async def _persist_synthetic_checkpoints(
        self,
        *,
        plan: AnalysisPlan,
        checkpoints: tuple[AnalysisStepCheckpoint, ...],
        invocation_fingerprint: str,
        auth_context: AuthContext,
    ) -> None:
        for checkpoint in checkpoints:
            if checkpoint.status not in {"skipped", "timeout"}:
                raise RunStateConflict("synthetic checkpoint has an invalid status")
            tool_call_id = analysis_step_tool_call_id(
                tenant_id=auth_context.principal.tenant_id,
                run_id=auth_context.run_id,
                plan_id=plan.plan_id,
                step_id=checkpoint.step_id,
            )
            ledger = await self._steps.reserve_step(
                tenant_id=auth_context.principal.tenant_id,
                user_id=auth_context.principal.user_id,
                run_id=auth_context.run_id,
                plan_id=plan.plan_id,
                request_id=plan.request_id,
                step_id=checkpoint.step_id,
                tool_call_id=tool_call_id,
                invocation_fingerprint=invocation_fingerprint,
            )
            await self._steps.transition_step(
                tenant_id=ledger.tenant_id,
                user_id=ledger.user_id,
                run_id=ledger.run_id,
                step_id=ledger.step_id,
                invocation_fingerprint=ledger.invocation_fingerprint,
                expected_version=ledger.version,
                status="synthetic",
                result_status=checkpoint.status,
                reason_code=checkpoint.reason_code,
                result_id=None,
                evidence_ids=(),
            )

    @staticmethod
    def _validated_completed(
        plan: AnalysisPlan, completed: tuple[AnalysisStepCheckpoint, ...]
    ) -> dict[str, AnalysisStepCheckpoint]:
        known = {step.step_id for step in plan.steps}
        result: dict[str, AnalysisStepCheckpoint] = {}
        for item in completed:
            if item.step_id not in known:
                raise RunStateConflict("analysis checkpoint contains an unknown step")
            existing = result.get(item.step_id)
            if existing is not None and existing != item:
                raise RunStateConflict("analysis checkpoint step changed after persistence")
            result[item.step_id] = item
        return result

    @staticmethod
    def _overall_status(
        completed: tuple[AnalysisStepCheckpoint, ...], has_omissions: bool
    ) -> AnalysisExecutionStatus:
        if completed and all(item.status == "success" for item in completed) and not has_omissions:
            return "completed"
        if any(item.status in {"success", "partial"} for item in completed):
            return "partial"
        return "failed"

    @staticmethod
    def _require_run(analysis_run_id: str, auth_context: AuthContext) -> None:
        if auth_context.run_id != analysis_run_id:
            raise RunStateConflict("analysis run does not match the live auth context")

    @staticmethod
    def _invocation_fingerprint(
        plan: AnalysisPlan, auth_context: AuthContext
    ) -> str:
        return canonical_fingerprint(
            domain="analysis-graph-invocation:1.0",
            value={
                "tenant_id": auth_context.principal.tenant_id,
                "user_id": auth_context.principal.user_id,
                "session_id": auth_context.session_id,
                "analysis_run_id": auth_context.run_id,
                "plan_id": plan.plan_id,
                "request_id": plan.request_id,
            },
        )
