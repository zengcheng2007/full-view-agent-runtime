"""P1-2 ??????????????????????????

? AnalysisRequest ????????SubjectAuthorization?? Catalog
??????????? AnalysisPlan?

- ?????????????????????????
- overview ?????? Catalog ?????????????????
  ??? Catalog ???????????????/????? overview
  ????????????????????????????
  SUBJECT_NOT_BINDABLE omission?Catalog ??????????
  ?? omission?????? goal ?????? subject?Catalog
  ?????????????? SUBJECT_NOT_BINDABLE omission?
- ???????????polygon ?????????????/??
  ?????????? omission/reason?????????????
- ????????????????step_id ???step-{subject}??
  plan_id ??????????????????????????
  ?? Native/LangGraph ??????
- ???? ``catalog.execution_fingerprint``?????
  ``catalog_fingerprint`` ? plan_id ??????????????
  ?????????/adapter??????? plan_id?????/
  ??????????
- ??? default_budget ????????????????
  ?tightened_to???????????plan_id ????????
  ?????effective budget???????????????
- fail closed?authorization ???????????????? None
  ??????????????????????? + omission ???
- ? LLM??????????????URL?SQL?adapter ?????
  ?????/??????????
"""

from collections.abc import Sequence

from full_view_agent.application.analysis_plan_integrity import (
    compute_analysis_plan_id,
)
from full_view_agent.domain.analysis_plan import (
    ANALYSIS_GOAL_ORDER,
    ANALYSIS_PLAN_SCHEMA_VERSION,
    AnalysisGoal,
    AnalysisOmission,
    AnalysisPlan,
    AnalysisRequest,
    AnalysisStep,
    PlanBudget,
    SavedPolygonScopeRef,
)
from full_view_agent.semantic.authorization import SubjectAuthorization
from full_view_agent.semantic.catalog import SemanticCatalog, area_is_authorized

DEFAULT_STEP_TIMEOUT_MS = 10_000


class AnalysisPlanningError(Exception):
    """?????????????"""


class AnalysisPlanRejected(AnalysisPlanningError):
    """????????fail closed??????? code?"""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"analysis plan rejected [{code}]: {message}")


class AnalysisPlanner:
    """??????????? ? ?? ? Catalog ????? ? ???"""

    def __init__(
        self,
        catalog: SemanticCatalog,
        *,
        default_budget: PlanBudget | None = None,
    ) -> None:
        self._catalog = catalog
        self._default_budget = default_budget or PlanBudget()

    def plan(
        self,
        request: AnalysisRequest,
        *,
        authorization: SubjectAuthorization | None,
    ) -> AnalysisPlan:
        # Fail closed????????????????????????
        if authorization is None:
            raise AnalysisPlanRejected(
                "AUTHORIZATION_REQUIRED",
                "analysis plan must derive from explicit authorization",
            )
        goals = _canonical_goals(request.goals)
        # ??? default_budget ?????????????????
        # ??????????????????????????
        constraints = (request.budget or self._default_budget).tightened_to(
            self._default_budget
        )
        # overview ??? Catalog ?????????????????
        subject_goals = _subject_goals(goals, self._catalog.subject_ids())

        steps: list[AnalysisStep] = []
        omissions: list[AnalysisOmission] = []
        # ????? = ?????????????
        for subject_id in sorted(subject_goals):
            covered_goals = tuple(subject_goals[subject_id])
            if len(steps) >= constraints.max_tool_calls:
                omissions.append(
                    AnalysisOmission(
                        goals=covered_goals,
                        subject=subject_id,
                        reason_code="BUDGET_TOOL_CALLS_EXCEEDED",
                        detail=(
                            f"tool call budget {constraints.max_tool_calls} "
                            "exhausted by earlier subjects in canonical order"
                        ),
                    )
                )
                continue
            outcome = self._evaluate(
                request, authorization, constraints, subject_id, covered_goals
            )
            if isinstance(outcome, AnalysisStep):
                steps.append(outcome)
            else:
                omissions.append(outcome)

        plan_id = compute_analysis_plan_id(
            schema_version=ANALYSIS_PLAN_SCHEMA_VERSION,
            catalog_version=self._catalog.catalog_version,
            catalog_fingerprint=self._catalog.execution_fingerprint,
            request_id=request.request_id,
            goals=goals,
            scope_ref=request.scope_ref,
            steps=steps,
            omissions=omissions,
            constraints=constraints,
        )
        return AnalysisPlan(
            plan_id=plan_id,
            catalog_version=self._catalog.catalog_version,
            catalog_fingerprint=self._catalog.execution_fingerprint,
            request_id=request.request_id,
            goals=goals,
            scope_ref=request.scope_ref,
            steps=tuple(steps),
            omissions=tuple(omissions),
            constraints=constraints,
        )

    def _evaluate(
        self,
        request: AnalysisRequest,
        authorization: SubjectAuthorization,
        constraints: PlanBudget,
        subject_id: str,
        covered_goals: tuple[AnalysisGoal, ...],
    ) -> AnalysisStep | AnalysisOmission:
        """????????? ? ????? ? ??? omission?

        ????????????? ? scope ?? ? entitlement ?
        dataset ? ???? ? ?? ? ?????????????????
        """
        subject = self._catalog.subject(subject_id)
        if subject is None:
            return AnalysisOmission(
                goals=covered_goals,
                subject=subject_id,
                reason_code="SUBJECT_NOT_BINDABLE",
                detail="catalog does not declare this subject",
            )
        if subject_id not in self._catalog.bindable_subject_ids():
            return AnalysisOmission(
                goals=covered_goals,
                subject=subject_id,
                reason_code="SUBJECT_NOT_BINDABLE",
                detail="subject is declared but has no verified capability binding",
            )

        scope_ref = request.scope_ref
        if isinstance(scope_ref, SavedPolygonScopeRef):
            return AnalysisOmission(
                goals=covered_goals,
                subject=subject_id,
                reason_code="SCOPE_KIND_UNSUPPORTED",
                detail=(
                    "first-slice analysis executes administrative scopes only; "
                    f"saved polygon reference {scope_ref.polygon_ref} is not executable"
                ),
            )

        if subject.required_entitlement not in authorization.entitlements:
            return AnalysisOmission(
                goals=covered_goals,
                subject=subject_id,
                reason_code="NOT_ENTITLED",
                detail=(
                    f"authorization lacks required entitlement "
                    f"{subject.required_entitlement}"
                ),
            )
        if subject.logical_dataset_id not in authorization.datasets:
            return AnalysisOmission(
                goals=covered_goals,
                subject=subject_id,
                reason_code="DATASET_NOT_AUTHORIZED",
                detail=(
                    f"authorization does not include dataset "
                    f"{subject.logical_dataset_id}"
                ),
            )
        if authorization.field_policy_set not in subject.field_policy_sets:
            return AnalysisOmission(
                goals=covered_goals,
                subject=subject_id,
                reason_code="FIELD_POLICY_MISMATCH",
                detail=(
                    f"field policy set {authorization.field_policy_set or '<empty>'} "
                    f"is not accepted by this subject"
                ),
            )
        if not area_is_authorized(scope_ref.scope, authorization):
            return AnalysisOmission(
                goals=covered_goals,
                subject=subject_id,
                reason_code="AREA_NOT_AUTHORIZED",
                detail=(
                    f"area {scope_ref.scope.area_code} is not within authorized "
                    "area scopes"
                ),
            )
        level = len(scope_ref.scope.area_code)
        if level not in subject.scope_levels:
            return AnalysisOmission(
                goals=covered_goals,
                subject=subject_id,
                reason_code="SCOPE_LEVEL_UNSUPPORTED",
                detail=(
                    f"area level {level} is not in subject supported levels "
                    f"{subject.scope_levels}"
                ),
            )

        # bindable_subject_ids ?????????????????????
        binding = self._catalog.binding(subject_id)
        if binding is None:
            raise AnalysisPlanningError(
                f"catalog binding for subject {subject_id} disappeared after "
                "bindability check"
            )
        return AnalysisStep(
            step_id=f"step-{subject_id}",
            goals=covered_goals,
            subject=subject_id,
            scope_ref=scope_ref,
            capability_id=binding.capability_id,
            capability_version=binding.capability_version,
            # ????????????????????????
            # ??????????????????????????????
            timeout_ms=min(DEFAULT_STEP_TIMEOUT_MS, constraints.total_timeout_ms),
        )


def _canonical_goals(goals: tuple[AnalysisGoal, ...]) -> tuple[AnalysisGoal, ...]:
    """?????????????????????????"""
    requested = set(goals)
    return tuple(goal for goal in ANALYSIS_GOAL_ORDER if goal in requested)


def _subject_goals(
    goals: tuple[AnalysisGoal, ...],
    declared_subject_ids: Sequence[str],
) -> dict[str, list[AnalysisGoal]]:
    """?? ? ????????????

    overview ?????? Catalog ????``declared_subject_ids``?
    ??????????????????????????????
    SUBJECT_NOT_BINDABLE omission?????? goal ??????
    subject?Catalog ???????????? omission????
    ????????
    """
    mapping: dict[str, list[AnalysisGoal]] = {}
    for goal in goals:
        candidates = declared_subject_ids if goal == "overview" else (goal,)
        for subject_id in candidates:
            mapping.setdefault(subject_id, []).append(goal)
    return mapping
