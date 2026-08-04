"""Application service for server-authored, run-scoped AnalysisPlan resources."""

from full_view_agent.application.analysis_plan_repository import (
    AnalysisPlanRepository,
    AnalysisPlanStoreRejected,
)
from full_view_agent.application.analysis_planner import (
    AnalysisPlanner,
    AnalysisPlanningError,
)
from full_view_agent.application.errors import (
    AnalysisPlanningUnavailable,
    AnalysisRequestRejected,
)
from full_view_agent.domain.analysis_plan import AnalysisPlan, AnalysisRequest
from full_view_agent.domain.models import AuthContext
from full_view_agent.semantic.authorization import SubjectAuthorization


class AnalysisPlanningService:
    """Create and persist plans only from the current trusted authorization."""

    def __init__(
        self,
        *,
        planner: AnalysisPlanner,
        repository: AnalysisPlanRepository,
    ) -> None:
        self._planner = planner
        self._repository = repository

    @property
    def planner(self) -> AnalysisPlanner:
        """只读暴露规划所用 Planner，供编译/加载服务防止错接。"""
        return self._planner

    async def create_plan(
        self,
        *,
        request: AnalysisRequest,
        auth_context: AuthContext,
    ) -> AnalysisPlan:
        try:
            plan = self._planner.plan(
                request,
                authorization=SubjectAuthorization.from_auth_context(auth_context),
            )
        except AnalysisPlanningError as exc:
            raise AnalysisRequestRejected(
                "analysis request cannot be planned under the current authorization"
            ) from exc
        try:
            return await self._repository.save(
                tenant_id=auth_context.principal.tenant_id,
                user_id=auth_context.principal.user_id,
                run_id=auth_context.run_id,
                plan=plan,
            )
        except AnalysisPlanStoreRejected as exc:
            raise AnalysisPlanningUnavailable(
                "server-side analysis plan authority rejected the plan"
            ) from exc
        except Exception as exc:
            raise AnalysisPlanningUnavailable(
                "server-side analysis plan authority is unavailable"
            ) from exc
