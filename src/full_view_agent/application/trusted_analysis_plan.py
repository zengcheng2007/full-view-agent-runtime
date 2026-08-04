"""服务端 AnalysisPlan 的统一加载、完整性与当前授权重规划校验。"""

from typing import NoReturn

from pydantic import ValidationError

from full_view_agent.application.analysis_plan_integrity import (
    recompute_analysis_plan_id,
)
from full_view_agent.application.analysis_plan_repository import (
    AnalysisPlanRepository,
    AnalysisPlanStoreRejected,
)
from full_view_agent.application.analysis_planner import AnalysisPlanner
from full_view_agent.domain.analysis_plan import (
    AnalysisPlan,
    AnalysisRequest,
    AreaScopeRef,
)
from full_view_agent.domain.models import AuthContext
from full_view_agent.semantic.authorization import SubjectAuthorization
from full_view_agent.semantic.catalog import SemanticCatalog


class AnalysisExecutionRejected(Exception):
    """计划在执行或报告组装前未通过可信边界。"""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"analysis execution rejected [{code}]: {message}")


class TrustedAnalysisPlanLoader:
    """执行器与报告组装器共享的可信计划加载门。"""

    def __init__(
        self,
        *,
        catalog: SemanticCatalog,
        planner: AnalysisPlanner,
        repository: AnalysisPlanRepository,
    ) -> None:
        if planner.catalog is not catalog:
            raise ValueError("trusted plan loader and planner must share the catalog")
        self._catalog = catalog
        self._planner = planner
        self._repository = repository

    async def load(
        self,
        *,
        plan_id: str,
        request_id: str,
        auth_context: AuthContext,
    ) -> AnalysisPlan:
        try:
            loaded = await self._repository.get(
                tenant_id=auth_context.principal.tenant_id,
                user_id=auth_context.principal.user_id,
                run_id=auth_context.run_id,
                plan_id=plan_id,
            )
        except AnalysisPlanStoreRejected as exc:
            raise AnalysisExecutionRejected(
                "PLAN_STORE_REJECTED",
                "server-side analysis plan failed store integrity validation",
            ) from exc
        if loaded is None:
            self._reject("PLAN_NOT_FOUND", "server-side analysis plan was not found")
        plan = self._revalidate_plan(loaded)
        if plan.plan_id != plan_id:
            self._reject(
                "PLAN_SOURCE_ID_MISMATCH",
                "loaded plan id does not match the requested plan id",
            )
        if plan.request_id != request_id:
            self._reject(
                "PLAN_REQUEST_MISMATCH",
                "loaded plan is not bound to this execution request",
            )
        self._validate_plan_snapshot(plan)
        self._validate_replanning(plan, auth_context=auth_context)
        return plan

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
            self._reject(
                "PLAN_REPLANNING_MISMATCH",
                "loaded plan differs from the current authorized deterministic plan",
            )

    @classmethod
    def _revalidate_plan(cls, plan: AnalysisPlan) -> AnalysisPlan:
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
            self._reject(
                "CATALOG_VERSION_MISMATCH",
                "plan catalog version does not match current catalog",
            )
        if plan.catalog_fingerprint != self._catalog.execution_fingerprint:
            self._reject(
                "CATALOG_FINGERPRINT_MISMATCH",
                "plan catalog fingerprint does not match current catalog",
            )
        for step in plan.steps:
            binding = self._catalog.binding(step.subject)
            if binding is None or (
                binding.capability_id != step.capability_id
                or binding.capability_version != step.capability_version
            ):
                self._reject(
                    "CAPABILITY_BINDING_MISMATCH",
                    f"step {step.step_id} no longer matches its catalog binding",
                )
            if not isinstance(step.scope_ref, AreaScopeRef):
                self._reject(
                    "SCOPE_KIND_UNSUPPORTED",
                    f"step {step.step_id} does not use an executable area scope",
                )
        if plan.plan_id != recompute_analysis_plan_id(plan):
            self._reject(
                "PLAN_ID_MISMATCH",
                "plan id does not match the canonical plan content",
            )
        for step in plan.steps:
            if step.scope_ref != plan.scope_ref:
                self._reject(
                    "PLAN_SCOPE_MISMATCH",
                    f"step {step.step_id} scope differs from the plan scope",
                )
            if any(goal != "overview" and goal != step.subject for goal in step.goals):
                self._reject(
                    "PLAN_GOAL_MISMATCH",
                    f"step {step.step_id} carries a goal for another subject",
                )
            if any(goal not in plan.goals for goal in step.goals):
                self._reject(
                    "PLAN_GOAL_MISMATCH",
                    f"step {step.step_id} carries a goal absent from the plan",
                )
            if step.step_id != f"step-{step.subject}":
                self._reject(
                    "PLAN_STEP_ID_MISMATCH",
                    f"step id {step.step_id} does not match its subject",
                )

    @staticmethod
    def _reject(code: str, message: str) -> NoReturn:
        raise AnalysisExecutionRejected(code, message)
