"""受控 AnalysisPlan 的确定性 DAG 执行器。

执行器只把计划步骤编译为 ``governance.semantic_query``，实际
解析、授权、能力调用、结果校验仍由现有 ``SemanticToolExecutor``
安全链负责。本模块不知道 adapter、URL、SQL 或物理字段。
"""

import asyncio
from typing import Protocol

from pydantic import ValidationError

from full_view_agent.application.analysis_plan_integrity import (
    recompute_analysis_plan_id,
)
from full_view_agent.application.analysis_plan_repository import (
    AnalysisPlanRepository,
)
from full_view_agent.application.analysis_planner import AnalysisPlanner
from full_view_agent.application.analysis_semantic_spec import (
    AnalysisSemanticSpecError,
    AnalysisSemanticSpecFactory,
)
from full_view_agent.application.errors import ReauthenticationRequired
from full_view_agent.application.fingerprints import canonical_fingerprint
from full_view_agent.application.ports import AgentStore
from full_view_agent.application.semantic_executor import SemanticToolExecutor
from full_view_agent.application.trusted_analysis_plan import (
    AnalysisExecutionRejected,
    TrustedAnalysisPlanLoader,
)
from full_view_agent.domain.analysis_execution import (
    AnalysisExecutionResult,
    AnalysisExecutionStatus,
    AnalysisStepExecution,
    AnalysisStepExecutionStatus,
)
from full_view_agent.domain.analysis_plan import (
    AnalysisPlan,
    AnalysisRequest,
    AnalysisStep,
    AreaScopeRef,
)
from full_view_agent.domain.models import AuthContext, ToolResult
from full_view_agent.semantic.action_resolver import (
    SEMANTIC_QUERY_TOOL_ID,
    SemanticActionResolver,
)
from full_view_agent.semantic.authorization import SubjectAuthorization
from full_view_agent.semantic.catalog import SemanticCatalog, SubjectDefinition


class AnalysisPlanExecutionPort(Protocol):
    """Native/LangGraph 后续接线时共用的应用层端口。"""

    async def execute(
        self,
        *,
        plan_id: str,
        request_id: str,
        auth_context: AuthContext,
    ) -> AnalysisExecutionResult: ...


class AnalysisPlanExecutor:
    """按 DAG ready set 执行计划，并严格实施并发与时间预算。"""

    def __init__(
        self,
        *,
        catalog: SemanticCatalog,
        resolver: SemanticActionResolver,
        semantic_executor: SemanticToolExecutor,
        planner: AnalysisPlanner,
        plan_repository: AnalysisPlanRepository,
        result_store: AgentStore,
    ) -> None:
        self._catalog = catalog
        self._result_store = result_store
        # 要求组合根显式传入与 SemanticToolExecutor 共用的 resolver。
        # 执行时仍由 semantic_executor 内部调用它；这里校验实例
        # 与 Catalog 一致，防止组合根误配。
        if resolver.catalog is not catalog:
            raise ValueError("analysis executor resolver must share the catalog instance")
        if semantic_executor.resolver is not resolver:
            raise ValueError("analysis executor and semantic executor must share the same resolver")
        if planner.catalog is not catalog:
            raise ValueError("analysis executor and planner must share the same catalog")
        self._semantic_executor = semantic_executor
        self._planner = planner
        self._plan_loader = TrustedAnalysisPlanLoader(
            catalog=catalog,
            planner=planner,
            repository=plan_repository,
        )
        self._semantic_spec_factory = AnalysisSemanticSpecFactory(catalog)

    async def execute(
        self,
        *,
        plan_id: str,
        request_id: str,
        auth_context: AuthContext,
    ) -> AnalysisExecutionResult:
        plan = await self._plan_loader.load(
            plan_id=plan_id,
            request_id=request_id,
            auth_context=auth_context,
        )
        semantic_arguments = {
            step.step_id: self._semantic_arguments(plan, step)
            for step in plan.steps
        }
        completed: dict[str, AnalysisStepExecution] = {}
        call_counter = [0]
        try:
            async with asyncio.timeout(plan.constraints.total_timeout_ms / 1000):
                await self._run_dag(
                    plan,
                    auth_context=auth_context,
                    completed=completed,
                    call_counter=call_counter,
                    semantic_arguments=semantic_arguments,
                )
        except TimeoutError:
            # asyncio.timeout 只在自己的 deadline 到期时转为 TimeoutError。
            # 外部 task.cancel() 仍以 CancelledError 透传。
            for step in plan.steps:
                completed.setdefault(
                    step.step_id,
                    self._step_result(
                        step,
                        status="timeout",
                        reason_code="TOTAL_TIMEOUT_EXCEEDED",
                        detail="analysis total timeout budget was exhausted",
                    ),
                )

        ordered = tuple(completed[step.step_id] for step in plan.steps)
        status, reason_code = self._overall_status(ordered, has_omissions=bool(plan.omissions))
        return AnalysisExecutionResult(
            plan_id=plan.plan_id,
            request_id=plan.request_id,
            status=status,
            reason_code=reason_code,
            steps=ordered,
            omissions=plan.omissions,
            tool_call_count=call_counter[0],
        )

    def _validate_replanning(
        self,
        plan: AnalysisPlan,
        *,
        auth_context: AuthContext,
    ) -> None:
        expected = self._planner.plan(
            AnalysisRequest(
                request_id=plan.request_id,
                goals=plan.goals,
                scope_ref=plan.scope_ref,
                budget=plan.constraints,
            ),
            authorization=SubjectAuthorization.from_auth_context(auth_context),
        )
        if expected != plan:
            raise AnalysisExecutionRejected(
                "PLAN_REPLANNING_MISMATCH",
                "loaded plan differs from the current authorized deterministic plan",
            )

    @staticmethod
    def _revalidate_plan(plan: AnalysisPlan) -> AnalysisPlan:
        try:
            return AnalysisPlan.model_validate(
                plan.model_dump(mode="python", warnings="none")
            )
        except ValidationError as exc:
            raise AnalysisExecutionRejected(
                "PLAN_CONTRACT_INVALID",
                "plan does not satisfy the current AnalysisPlan contract",
            ) from exc

    def _validate_plan_snapshot(self, plan: AnalysisPlan) -> None:
        if plan.catalog_version != self._catalog.catalog_version:
            raise AnalysisExecutionRejected(
                "CATALOG_VERSION_MISMATCH",
                "plan catalog version does not match current catalog",
            )
        if plan.catalog_fingerprint != self._catalog.execution_fingerprint:
            raise AnalysisExecutionRejected(
                "CATALOG_FINGERPRINT_MISMATCH",
                "plan catalog fingerprint does not match current catalog",
            )
        for step in plan.steps:
            binding = self._catalog.binding(step.subject)
            if binding is None or (
                binding.capability_id != step.capability_id
                or binding.capability_version != step.capability_version
            ):
                raise AnalysisExecutionRejected(
                    "CAPABILITY_BINDING_MISMATCH",
                    f"step {step.step_id} no longer matches its catalog binding",
                )
            if not isinstance(step.scope_ref, AreaScopeRef):
                raise AnalysisExecutionRejected(
                    "SCOPE_KIND_UNSUPPORTED",
                    f"step {step.step_id} does not use an executable area scope",
                )
        if plan.plan_id != recompute_analysis_plan_id(plan):
            raise AnalysisExecutionRejected(
                "PLAN_ID_MISMATCH",
                "plan id does not match the canonical plan content",
            )
        for step in plan.steps:
            if step.scope_ref != plan.scope_ref:
                raise AnalysisExecutionRejected(
                    "PLAN_SCOPE_MISMATCH",
                    f"step {step.step_id} scope differs from the plan scope",
                )
            if any(goal != "overview" and goal != step.subject for goal in step.goals):
                raise AnalysisExecutionRejected(
                    "PLAN_GOAL_MISMATCH",
                    f"step {step.step_id} carries a goal for another subject",
                )
            if any(goal not in plan.goals for goal in step.goals):
                raise AnalysisExecutionRejected(
                    "PLAN_GOAL_MISMATCH",
                    f"step {step.step_id} carries a goal absent from the plan",
                )
            if step.step_id != f"step-{step.subject}":
                raise AnalysisExecutionRejected(
                    "PLAN_STEP_ID_MISMATCH",
                    f"step id {step.step_id} does not match its subject",
                )

    async def _run_dag(
        self,
        plan: AnalysisPlan,
        *,
        auth_context: AuthContext,
        completed: dict[str, AnalysisStepExecution],
        call_counter: list[int],
        semantic_arguments: dict[str, dict[str, object]],
    ) -> None:
        by_id = {step.step_id: step for step in plan.steps}
        active: dict[asyncio.Task[AnalysisStepExecution], str] = {}
        scheduled_count = 0
        try:
            while len(completed) < len(plan.steps):
                active_ids = set(active.values())
                for step in plan.steps:
                    if step.step_id in completed or step.step_id in active_ids:
                        continue
                    dependency_results = [completed.get(dep) for dep in step.depends_on]
                    if any(
                        result is not None
                        and result.status not in {"success", "partial"}
                        for result in dependency_results
                    ):
                        completed[step.step_id] = self._step_result(
                            step,
                            status="skipped",
                            reason_code="DEPENDENCY_NOT_SUCCESSFUL",
                            detail="one or more dependency steps did not complete successfully",
                        )

                active_ids = set(active.values())
                ready = [
                    step
                    for step in plan.steps
                    if step.step_id not in completed
                    and step.step_id not in active_ids
                    and all(
                        completed.get(dep) is not None
                        and completed[dep].status in {"success", "partial"}
                        for dep in step.depends_on
                    )
                ]
                available = plan.constraints.max_parallel - len(active)
                for step in ready[:available]:
                    if scheduled_count >= plan.constraints.max_tool_calls:
                        break
                    task = asyncio.create_task(
                        self._execute_step(
                            plan,
                            step,
                            auth_context=auth_context,
                            raw_arguments=semantic_arguments[step.step_id],
                            call_counter=call_counter,
                        )
                    )
                    active[task] = step.step_id
                    scheduled_count += 1

                if scheduled_count >= plan.constraints.max_tool_calls:
                    active_ids = set(active.values())
                    for step in plan.steps:
                        if step.step_id not in completed and step.step_id not in active_ids:
                            completed[step.step_id] = self._step_result(
                                step,
                                status="skipped",
                                reason_code="TOOL_CALL_BUDGET_EXCEEDED",
                                detail="analysis tool call budget was exhausted",
                            )

                if not active:
                    # AnalysisPlan 已经校验 DAG；若仍无 ready step，必须安全收敛。
                    for step_id, step in by_id.items():
                        completed.setdefault(
                            step_id,
                            self._step_result(
                                step,
                                status="failed",
                                reason_code="EXECUTION_GRAPH_STALLED",
                                detail="analysis execution graph could not make progress",
                            ),
                        )
                    break

                done, _pending = await asyncio.wait(
                    active,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in done:
                    step_id = active.pop(task)
                    completed[step_id] = task.result()
        finally:
            # 总超时或外部取消都必须终止在途步骤，且等待清理完成。
            for task in active:
                task.cancel()
            if active:
                await asyncio.gather(*active, return_exceptions=True)

    async def _execute_step(
        self,
        plan: AnalysisPlan,
        step: AnalysisStep,
        *,
        auth_context: AuthContext,
        raw_arguments: dict[str, object],
        call_counter: list[int],
    ) -> AnalysisStepExecution:
        tool_call_id = canonical_fingerprint(
            domain="analysis-step-tool-call:1.0",
            value={"plan_id": plan.plan_id, "step_id": step.step_id},
        )
        try:
            async with asyncio.timeout(step.timeout_ms / 1000):
                call_counter[0] += 1
                result = await self._semantic_executor.execute(
                    tool_call_id=tool_call_id,
                    tool_id=SEMANTIC_QUERY_TOOL_ID,
                    raw_arguments=raw_arguments,
                    auth_context=auth_context,
                )
        except TimeoutError:
            return self._step_result(
                step,
                status="timeout",
                reason_code="STEP_TIMEOUT_EXCEEDED",
                detail="analysis step timeout budget was exhausted",
            )
        except ReauthenticationRequired:
            raise
        except asyncio.CancelledError:
            raise
        except Exception:
            return self._step_result(
                step,
                status="failed",
                reason_code="STEP_EXECUTION_ERROR",
                detail="controlled semantic execution failed unexpectedly",
            )

        if result.status in {"success", "partial"} and result.data_result is not None:
            # 可用子结果必须进入既有 Result 生命周期（既有 AgentStore，
            # 不新建旁路仓储）；保存按 result_id 幂等覆盖。
            try:
                await self._result_store.save_result(
                    user_id=auth_context.principal.user_id,
                    run_id=auth_context.run_id,
                    result=result.data_result,
                )
            except Exception:
                # fail closed：未持久化的子结果不能当作可用结果。
                return self._step_result(
                    step,
                    status="failed",
                    reason_code="CHILD_RESULT_NOT_PERSISTED",
                    detail="child result could not be saved into the result lifecycle",
                )

        reason_code = (
            result.warnings[0]
            if result.warnings
            else {
                "success": "STEP_SUCCEEDED",
                "partial": "STEP_PARTIAL",
                "denied": "STEP_DENIED",
                "failed": "STEP_FAILED",
            }[result.status]
        )
        return self._step_result(
            step,
            status=result.status,
            reason_code=reason_code,
            detail=result.summary,
            tool_result=result,
        )

    def _semantic_arguments(
        self,
        plan: AnalysisPlan,
        step: AnalysisStep,
    ) -> dict[str, object]:
        try:
            spec = self._semantic_spec_factory.build(plan, step)
        except AnalysisSemanticSpecError as exc:
            raise AnalysisExecutionRejected(
                "QUERY_SPEC_NOT_DERIVABLE", "step subject or scope is not executable"
            ) from exc
        return {
            "catalog_version": plan.catalog_version,
            "catalog_fingerprint": plan.catalog_fingerprint,
            "spec": spec.model_dump(mode="json"),
        }

    @staticmethod
    def _default_group_by(
        subject: SubjectDefinition,
        scope_ref: AreaScopeRef,
    ) -> list[str]:
        if subject.min_group_by == 0:
            return []
        scope_level = len(scope_ref.scope.area_code)
        candidates = sorted(
            rule.value
            for rule in subject.group_by_rules
            if scope_level in rule.allowed_scope_levels
        )
        if len(candidates) < subject.min_group_by:
            raise AnalysisExecutionRejected(
                "QUERY_SPEC_NOT_DERIVABLE",
                f"subject {subject.subject_id} has no safe grouping for this scope",
            )
        return candidates[: subject.min_group_by]

    @staticmethod
    def _step_result(
        step: AnalysisStep,
        *,
        status: AnalysisStepExecutionStatus,
        reason_code: str,
        detail: str,
        tool_result: ToolResult | None = None,
    ) -> AnalysisStepExecution:
        return AnalysisStepExecution(
            step_id=step.step_id,
            subject=step.subject,
            status=status,
            reason_code=(reason_code or "UNSPECIFIED")[:128],
            detail=(detail or "No execution detail was provided.")[:500],
            tool_result=tool_result,
        )

    @staticmethod
    def _overall_status(
        steps: tuple[AnalysisStepExecution, ...],
        *,
        has_omissions: bool,
    ) -> tuple[AnalysisExecutionStatus, str]:
        if steps and all(step.status == "success" for step in steps) and not has_omissions:
            return "completed", "ANALYSIS_COMPLETED"
        has_usable_result = any(step.status in {"success", "partial"} for step in steps)
        if has_usable_result:
            return "partial", "ANALYSIS_PARTIAL"
        return "failed", "ANALYSIS_FAILED"
