"""P1-2 ??????????????

???? AnalysisRequest ???????? Catalog ?????????
- ????????????????????
- ??????????? omission/reason?????????????
- ?????step_id?plan_id ?????????????
- ??? fail-closed ????????????polygon ???next_area?
  ???????????/?????????????????????
- ?????????????
"""

import inspect

import pytest
from pydantic import ValidationError

from full_view_agent.application.analysis_planner import (
    AnalysisPlanner,
    AnalysisPlanRejected,
)
from full_view_agent.domain.analysis_plan import (
    AnalysisPlan,
    AnalysisRequest,
    AreaScopeRef,
    PlanBudget,
    SavedPolygonScopeRef,
)
from full_view_agent.domain.models import AuthorizedAreaScope, MetricQueryScope
from full_view_agent.semantic.authorization import SubjectAuthorization
from full_view_agent.semantic.catalog import CapabilityBinding, SemanticCatalog

from .test_policy import population_auth_context

FULL_AUTH = SubjectAuthorization(
    entitlements=(
        "governance.area.read",
        "governance.event.aggregate.read",
        "governance.housing.aggregate.read",
        "governance.population.aggregate.read",
    ),
    datasets=("administrative_area", "event", "housing", "population"),
    area_scopes=(AuthorizedAreaScope(area_code="3301", include_descendants=True),),
    field_policy_set="governance_analyst_v1",
)

# ???? token + ???????? token????????????
FORBIDDEN_TOKENS = (
    "adapter://",
    "geo-qxst",
    "http://",
    "https://",
    "getNextSiteData",
    "getRoomLeaseType",
    "tableName",
    "areaCode",
    "base_room_lease",
    "dm_empty_nest_old",
    "coordinates",
    "longitude",
    "latitude",
    "POLYGON",
    "next_area",
    "gender",
    "age_band",
    "time_range",
    "trend",
    "causal",
    "profile",
)


@pytest.fixture
def catalog() -> SemanticCatalog:
    # ???????housing next_area ???
    return SemanticCatalog.default()


@pytest.fixture
def planner(catalog: SemanticCatalog) -> AnalysisPlanner:
    return AnalysisPlanner(catalog)


def area_request(
    *goals: str,
    area_code: str = "330106",
    request_id: str = "req-01",
    budget: PlanBudget | None = None,
) -> AnalysisRequest:
    return AnalysisRequest(
        request_id=request_id,
        goals=goals,  # type: ignore[arg-type]
        scope_ref=AreaScopeRef(scope=MetricQueryScope(area_code=area_code)),
        budget=budget,
    )


def omission_map(plan: AnalysisPlan) -> dict[str, str]:
    return {omission.subject: omission.reason_code for omission in plan.omissions}


# ---------------------------------------------------------------------------
# ??????????????????
# ---------------------------------------------------------------------------


def test_single_goal_plans_only_that_subject(planner: AnalysisPlanner) -> None:
    plan = planner.plan(area_request("population"), authorization=FULL_AUTH)

    assert [step.subject for step in plan.steps] == ["population"]
    assert plan.steps[0].goals == ("population",)
    assert plan.goals == ("population",)
    assert plan.omissions == ()


def test_step_carries_catalog_binding_and_request_scope(
    planner: AnalysisPlanner,
    catalog: SemanticCatalog,
) -> None:
    plan = planner.plan(area_request("population"), authorization=FULL_AUTH)
    step = plan.steps[0]
    binding = catalog.binding("population")
    assert binding is not None

    assert step.step_id == "step-population"
    assert step.capability_id == binding.capability_id
    assert step.capability_version == binding.capability_version
    assert step.subject == "population"
    assert step.scope_ref == AreaScopeRef(
        scope=MetricQueryScope(area_code="330106")
    )
    assert step.effect == "read"
    assert step.depends_on == ()
    assert 100 <= step.timeout_ms <= 120_000


# ---------------------------------------------------------------------------
# ??????????????????
# ---------------------------------------------------------------------------


def test_all_four_goals_dedupe_to_three_subject_steps(
    planner: AnalysisPlanner,
) -> None:
    plan = planner.plan(
        area_request("overview", "population", "housing", "event"),
        authorization=FULL_AUTH,
    )

    assert plan.goals == ("overview", "population", "housing", "event")
    # ??????????????????????????
    assert [step.subject for step in plan.steps] == ["event", "housing", "population"]
    by_subject = {step.subject: step for step in plan.steps}
    assert by_subject["population"].goals == ("overview", "population")
    assert by_subject["housing"].goals == ("overview", "housing")
    assert by_subject["event"].goals == ("overview", "event")
    assert plan.omissions == ()


def test_goal_order_does_not_affect_plan_output(planner: AnalysisPlanner) -> None:
    forward = planner.plan(
        area_request("overview", "population", "housing", "event"),
        authorization=FULL_AUTH,
    )
    backward = planner.plan(
        area_request("event", "housing", "population", "overview"),
        authorization=FULL_AUTH,
    )
    assert forward.model_dump_json() == backward.model_dump_json()


def test_duplicate_goals_are_canonicalized(planner: AnalysisPlanner) -> None:
    plan = planner.plan(
        area_request("population", "population", "population"),
        authorization=FULL_AUTH,
    )
    assert plan.goals == ("population",)
    assert [step.subject for step in plan.steps] == ["population"]


# ---------------------------------------------------------------------------
# ???????? omission ????/??
# ---------------------------------------------------------------------------


def test_missing_entitlement_forms_omission(planner: AnalysisPlanner) -> None:
    population_only = SubjectAuthorization(
        entitlements=("governance.population.aggregate.read",),
        datasets=("population",),
        area_scopes=(AuthorizedAreaScope(area_code="3301", include_descendants=True),),
        field_policy_set="governance_analyst_v1",
    )
    plan = planner.plan(area_request("overview"), authorization=population_only)

    assert [step.subject for step in plan.steps] == ["population"]
    assert omission_map(plan) == {
        "event": "NOT_ENTITLED",
        "housing": "NOT_ENTITLED",
    }


def test_missing_dataset_forms_distinct_omission(planner: AnalysisPlanner) -> None:
    entitlement_without_dataset = SubjectAuthorization(
        entitlements=(
            "governance.population.aggregate.read",
            "governance.housing.aggregate.read",
        ),
        datasets=("population",),
        area_scopes=(AuthorizedAreaScope(area_code="3301", include_descendants=True),),
        field_policy_set="governance_analyst_v1",
    )
    plan = planner.plan(
        area_request("population", "housing"),
        authorization=entitlement_without_dataset,
    )

    assert [step.subject for step in plan.steps] == ["population"]
    assert omission_map(plan) == {"housing": "DATASET_NOT_AUTHORIZED"}


def test_field_policy_mismatch_forms_omission(planner: AnalysisPlanner) -> None:
    wrong_policy = SubjectAuthorization(
        entitlements=FULL_AUTH.entitlements,
        datasets=FULL_AUTH.datasets,
        area_scopes=FULL_AUTH.area_scopes,
        field_policy_set="other_policy_v9",
    )
    plan = planner.plan(area_request("overview"), authorization=wrong_policy)

    assert plan.steps == ()
    assert set(omission_map(plan)) == {"event", "housing", "population"}
    assert {code for code in omission_map(plan).values()} == {"FIELD_POLICY_MISMATCH"}


def test_area_outside_authorization_forms_omission(planner: AnalysisPlanner) -> None:
    plan = planner.plan(
        area_request("overview", area_code="330206"),
        authorization=FULL_AUTH,
    )

    assert plan.steps == ()
    assert {code for code in omission_map(plan).values()} == {"AREA_NOT_AUTHORIZED"}


def test_city_scope_includes_controlled_population_ranking(
    planner: AnalysisPlanner,
) -> None:
    # ???4 ??? housing/event ????????? population ?
    # scope_levels?6/9/12??population ?? omission????????
    plan = planner.plan(
        area_request("overview", area_code="3301"),
        authorization=FULL_AUTH,
    )

    assert [step.subject for step in plan.steps] == ["event", "housing", "population"]
    assert omission_map(plan) == {}


def test_unbindable_subject_forms_omission(catalog: SemanticCatalog) -> None:
    partial = SemanticCatalog(
        catalog_version=catalog.catalog_version,
        supported_spec_versions=catalog.supported_spec_versions,
        subjects=catalog.subjects,
        bindings={
            subject_id: binding
            for subject_id, binding in catalog.bindings.items()
            if subject_id != "event"
        },
    )
    planner = AnalysisPlanner(partial)
    plan = planner.plan(area_request("overview"), authorization=FULL_AUTH)

    assert [step.subject for step in plan.steps] == ["housing", "population"]
    assert omission_map(plan) == {"event": "SUBJECT_NOT_BINDABLE"}


# ---------------------------------------------------------------------------
# ???????overview ??? Catalog ????????? omission
# ---------------------------------------------------------------------------


def population_only_catalog(catalog: SemanticCatalog) -> SemanticCatalog:
    """??? population???????? Catalog?"""
    return SemanticCatalog(
        catalog_version=catalog.catalog_version,
        supported_spec_versions=catalog.supported_spec_versions,
        subjects={"population": catalog.subjects["population"]},
        bindings={"population": catalog.bindings["population"]},
    )


def test_overview_derives_candidates_from_catalog_declarations(
    catalog: SemanticCatalog,
) -> None:
    # Reviewer ???? population Catalog + overview ??? population?
    # ??? Catalog ???? event/housing ???? omission?
    planner = AnalysisPlanner(population_only_catalog(catalog))
    plan = planner.plan(area_request("overview"), authorization=FULL_AUTH)

    assert [step.subject for step in plan.steps] == ["population"]
    assert plan.omissions == ()
    planned_or_omitted = {step.subject for step in plan.steps} | set(
        omission_map(plan)
    )
    assert planned_or_omitted == {"population"}


def test_overview_with_empty_catalog_plans_nothing(
    catalog: SemanticCatalog,
) -> None:
    empty = SemanticCatalog(
        catalog_version=catalog.catalog_version,
        supported_spec_versions=catalog.supported_spec_versions,
        subjects={},
        bindings={},
    )
    plan = AnalysisPlanner(empty).plan(area_request("overview"), authorization=FULL_AUTH)

    # ????? ? overview ??????? + ? omission???????
    assert plan.steps == ()
    assert plan.omissions == ()


def test_explicit_goal_for_absent_subject_forms_real_omission(
    catalog: SemanticCatalog,
) -> None:
    # ????? goal ?????? subject?Catalog ???????
    # ???????????? SUBJECT_NOT_BINDABLE omission?
    planner = AnalysisPlanner(population_only_catalog(catalog))
    plan = planner.plan(area_request("housing"), authorization=FULL_AUTH)

    assert plan.steps == ()
    assert omission_map(plan) == {"housing": "SUBJECT_NOT_BINDABLE"}


def test_overview_plus_absent_explicit_goal_mixes_real_outcomes(
    catalog: SemanticCatalog,
) -> None:
    planner = AnalysisPlanner(population_only_catalog(catalog))
    plan = planner.plan(
        area_request("overview", "housing"), authorization=FULL_AUTH
    )

    # overview ?? population ????? housing ???? omission?
    # event ????? ? ????
    assert [step.subject for step in plan.steps] == ["population"]
    assert omission_map(plan) == {"housing": "SUBJECT_NOT_BINDABLE"}
    assert "event" not in plan.model_dump_json()


def test_single_goal_on_derived_catalog_does_not_expand(
    catalog: SemanticCatalog,
) -> None:
    planner = AnalysisPlanner(population_only_catalog(catalog))
    plan = planner.plan(area_request("population"), authorization=FULL_AUTH)

    assert [step.subject for step in plan.steps] == ["population"]
    assert plan.omissions == ()


def test_plan_id_is_stable_for_catalog_derived_overview(
    catalog: SemanticCatalog,
) -> None:
    derived = population_only_catalog(catalog)
    request = area_request("overview")
    first = AnalysisPlanner(derived).plan(request, authorization=FULL_AUTH)
    second = AnalysisPlanner(derived).plan(request, authorization=FULL_AUTH)

    assert first.plan_id == second.plan_id
    assert first.model_dump_json() == second.model_dump_json()


def test_unsupported_capabilities_never_planned_or_substituted(
    planner: AnalysisPlanner,
) -> None:
    plan = planner.plan(
        area_request("overview", "population", "housing", "event"),
        authorization=FULL_AUTH,
    )
    serialized = plan.model_dump_json()

    # ???????????????????????????
    for token in ("next_area", "gender", "age_band", "time_range", "trend"):
        assert token not in serialized
    # ???????/??????????????
    assert "arguments" not in serialized
    assert "group_by" not in serialized


# ---------------------------------------------------------------------------
# polygon ??????????????? ? ??? omission
# ---------------------------------------------------------------------------


def test_polygon_scope_reference_is_accepted_but_not_executable(
    planner: AnalysisPlanner,
) -> None:
    request = AnalysisRequest(
        request_id="req-polygon",
        goals=("overview",),
        scope_ref=SavedPolygonScopeRef(polygon_ref="saved-polygon-01"),
    )
    plan = planner.plan(request, authorization=FULL_AUTH)

    assert plan.steps == ()
    assert set(omission_map(plan)) == {"event", "housing", "population"}
    assert {code for code in omission_map(plan).values()} == {"SCOPE_KIND_UNSUPPORTED"}
    # ??????????scope ??????????????
    assert "saved-polygon-01" in plan.model_dump_json()


def test_inline_coordinates_rejected_at_request_boundary() -> None:
    with pytest.raises(ValidationError):
        AnalysisRequest.model_validate(
            {
                "request_id": "req-01",
                "goals": ["overview"],
                "scope_ref": {
                    "kind": "saved_polygon",
                    "polygon_ref": "POLYGON((120.1 30.2, 120.2 30.3))",
                },
            }
        )
    with pytest.raises(ValidationError):
        AnalysisRequest.model_validate(
            {
                "request_id": "req-01",
                "goals": ["overview"],
                "scope_ref": {
                    "kind": "area",
                    "scope": {"area_code": "330106"},
                    "center": {"lat": 30.2, "lng": 120.1},
                },
            }
        )


def test_unknown_goal_rejected_at_request_boundary() -> None:
    with pytest.raises(ValidationError):
        AnalysisRequest.model_validate(
            {
                "request_id": "req-01",
                "goals": ["economy"],
                "scope_ref": {"kind": "area", "scope": {"area_code": "330106"}},
            }
        )


# ---------------------------------------------------------------------------
# fail closed??????????? ? ? omission
# ---------------------------------------------------------------------------


def test_authorization_parameter_has_no_default() -> None:
    # ? model_capability_view ???????????????
    signature = inspect.signature(AnalysisPlanner.plan)
    assert signature.parameters["authorization"].default is inspect.Parameter.empty


def test_missing_authorization_is_rejected(planner: AnalysisPlanner) -> None:
    with pytest.raises(AnalysisPlanRejected) as exc_info:
        planner.plan(area_request("overview"), authorization=None)
    assert exc_info.value.code == "AUTHORIZATION_REQUIRED"


def test_empty_authorization_produces_only_omissions(planner: AnalysisPlanner) -> None:
    plan = planner.plan(area_request("overview"), authorization=SubjectAuthorization())

    assert plan.steps == ()
    assert set(omission_map(plan)) == {"event", "housing", "population"}
    assert {code for code in omission_map(plan).values()} == {"NOT_ENTITLED"}


def test_authorization_derived_from_auth_context_is_sufficient(
    planner: AnalysisPlanner,
) -> None:
    authorization = SubjectAuthorization.from_auth_context(population_auth_context())
    # population_auth_context??? 330106?? population ???
    plan = planner.plan(area_request("overview"), authorization=authorization)

    assert [step.subject for step in plan.steps] == ["population"]
    assert omission_map(plan) == {
        "event": "NOT_ENTITLED",
        "housing": "NOT_ENTITLED",
    }


# ---------------------------------------------------------------------------
# ????????????? ? ????? + ??? omission
# ---------------------------------------------------------------------------


def test_tool_call_budget_overflow_forms_omissions(planner: AnalysisPlanner) -> None:
    plan = planner.plan(
        area_request("overview", budget=PlanBudget(max_tool_calls=1)),
        authorization=FULL_AUTH,
    )

    # ??????event, housing, population?? event ?????
    assert [step.subject for step in plan.steps] == ["event"]
    assert omission_map(plan) == {
        "housing": "BUDGET_TOOL_CALLS_EXCEEDED",
        "population": "BUDGET_TOOL_CALLS_EXCEEDED",
    }
    assert plan.constraints.max_tool_calls == 1
    assert len(plan.steps) <= plan.constraints.max_tool_calls


# ---------------------------------------------------------------------------
# ????????????????????
# ---------------------------------------------------------------------------

SERVER_CEILING = PlanBudget(max_parallel=2, max_tool_calls=4, total_timeout_ms=30_000)
WIDENED_BUDGET = PlanBudget(max_parallel=8, max_tool_calls=64, total_timeout_ms=600_000)


@pytest.fixture
def capped_planner(catalog: SemanticCatalog) -> AnalysisPlanner:
    return AnalysisPlanner(catalog, default_budget=SERVER_CEILING)


def test_request_budget_cannot_widen_server_ceiling(
    capped_planner: AnalysisPlanner,
) -> None:
    plan = capped_planner.plan(
        area_request("overview", budget=WIDENED_BUDGET), authorization=FULL_AUTH
    )
    assert plan.constraints == SERVER_CEILING


def test_request_budget_can_tighten_server_ceiling(
    capped_planner: AnalysisPlanner,
) -> None:
    tighter = PlanBudget(max_parallel=1, max_tool_calls=2, total_timeout_ms=10_000)
    plan = capped_planner.plan(
        area_request("overview", budget=tighter), authorization=FULL_AUTH
    )
    assert plan.constraints == tighter


def test_request_budget_tightens_per_field(capped_planner: AnalysisPlanner) -> None:
    mixed = PlanBudget(max_parallel=1, max_tool_calls=64, total_timeout_ms=600_000)
    plan = capped_planner.plan(
        area_request("overview", budget=mixed), authorization=FULL_AUTH
    )
    assert plan.constraints == PlanBudget(
        max_parallel=1, max_tool_calls=4, total_timeout_ms=30_000
    )


def test_absent_request_budget_uses_server_budget(
    capped_planner: AnalysisPlanner,
) -> None:
    plan = capped_planner.plan(area_request("overview"), authorization=FULL_AUTH)
    assert plan.constraints == SERVER_CEILING


def test_plan_id_derives_from_effective_budget(capped_planner: AnalysisPlanner) -> None:
    # ??????????????????????????effective
    # constraints ?? ? ???????plan_id ?????????
    widened = capped_planner.plan(
        area_request("overview", budget=WIDENED_BUDGET), authorization=FULL_AUTH
    )
    plain = capped_planner.plan(area_request("overview"), authorization=FULL_AUTH)
    assert widened.model_dump_json() == plain.model_dump_json()
    assert widened.plan_id == plain.plan_id


def test_step_timeout_is_clamped_to_server_total_budget(
    catalog: SemanticCatalog,
) -> None:
    # ???????????????????????????
    # ??????????????????
    tight_total = AnalysisPlanner(
        catalog, default_budget=PlanBudget(total_timeout_ms=1_000)
    )
    plan = tight_total.plan(area_request("population"), authorization=FULL_AUTH)

    assert plan.constraints.total_timeout_ms == 1_000
    assert plan.steps[0].timeout_ms == 1_000


def test_plan_constraints_are_carried(planner: AnalysisPlanner) -> None:
    plan = planner.plan(area_request("overview"), authorization=FULL_AUTH)

    assert plan.constraints.max_parallel >= 1
    assert plan.constraints.max_tool_calls >= len(plan.steps)
    assert plan.constraints.total_timeout_ms >= max(
        step.timeout_ms for step in plan.steps
    )


# ---------------------------------------------------------------------------
# ?????????????????/????
# ---------------------------------------------------------------------------


def test_plan_is_deterministic_across_calls_and_instances(
    catalog: SemanticCatalog,
) -> None:
    request = area_request("overview", "population")
    first = AnalysisPlanner(catalog).plan(request, authorization=FULL_AUTH)
    second = AnalysisPlanner(catalog).plan(request, authorization=FULL_AUTH)

    assert first.model_dump_json() == second.model_dump_json()
    assert first.plan_id == second.plan_id
    assert [step.step_id for step in first.steps] == [
        step.step_id for step in second.steps
    ]


# ---------------------------------------------------------------------------
# ?????????????????? ? ??????
# ---------------------------------------------------------------------------


def catalog_with_adapter_ref(
    catalog: SemanticCatalog, *, adapter_ref: str
) -> SemanticCatalog:
    """?? version/capability_id/version ?????? adapter_ref?"""
    return SemanticCatalog(
        catalog_version=catalog.catalog_version,
        supported_spec_versions=catalog.supported_spec_versions,
        subjects=catalog.subjects,
        bindings={
            subject_id: CapabilityBinding(
                capability_id=binding.capability_id,
                capability_version=binding.capability_version,
                adapter_ref=f"{adapter_ref}/{subject_id}",
            )
            for subject_id, binding in catalog.bindings.items()
        },
    )


def test_plan_pins_catalog_execution_fingerprint(
    planner: AnalysisPlanner, catalog: SemanticCatalog
) -> None:
    plan = planner.plan(area_request("overview"), authorization=FULL_AUTH)
    assert plan.catalog_fingerprint == catalog.execution_fingerprint


def test_adapter_ref_change_moves_fingerprint_and_plan_id(
    catalog: SemanticCatalog,
) -> None:
    altered = catalog_with_adapter_ref(catalog, adapter_ref="adapter://geo-other")
    request = area_request("overview")

    plan_base = AnalysisPlanner(catalog).plan(request, authorization=FULL_AUTH)
    plan_altered = AnalysisPlanner(altered).plan(request, authorization=FULL_AUTH)

    # ??????????? ID/???????? adapter_ref ???
    assert plan_base.catalog_version == plan_altered.catalog_version
    assert [step.capability_id for step in plan_base.steps] == [
        step.capability_id for step in plan_altered.steps
    ]
    assert [step.capability_version for step in plan_base.steps] == [
        step.capability_version for step in plan_altered.steps
    ]
    # ??????????????catalog ????????plan_id ????
    assert altered.execution_fingerprint != catalog.execution_fingerprint
    assert plan_altered.catalog_fingerprint != plan_base.catalog_fingerprint
    assert plan_altered.plan_id != plan_base.plan_id
    assert plan_altered.model_dump_json() != plan_base.model_dump_json()


def test_identical_catalog_rebuild_keeps_fingerprint_stable(
    catalog: SemanticCatalog,
) -> None:
    clone = SemanticCatalog(
        catalog_version=catalog.catalog_version,
        supported_spec_versions=catalog.supported_spec_versions,
        subjects=catalog.subjects,
        bindings=catalog.bindings,
    )
    request = area_request("overview")

    plan_first = AnalysisPlanner(catalog).plan(request, authorization=FULL_AUTH)
    plan_clone = AnalysisPlanner(clone).plan(request, authorization=FULL_AUTH)

    assert clone.execution_fingerprint == catalog.execution_fingerprint
    assert plan_clone.catalog_fingerprint == plan_first.catalog_fingerprint
    assert plan_clone.plan_id == plan_first.plan_id


def test_plan_is_not_hardcoded_to_one_scenario(planner: AnalysisPlanner) -> None:
    district = planner.plan(
        area_request("overview", area_code="330106"), authorization=FULL_AUTH
    )
    street = planner.plan(
        area_request("overview", area_code="330106001"), authorization=FULL_AUTH
    )

    # ???????????scope ????????????/?????
    assert district.steps[0].scope_ref.scope.area_code == "330106"
    assert street.steps[0].scope_ref.scope.area_code == "330106001"
    assert district.plan_id != street.plan_id
    serialized = district.model_dump_json()
    assert "??" not in serialized
    assert "hangzhou" not in serialized.lower()


def test_plan_serialization_excludes_forbidden_tokens(
    planner: AnalysisPlanner,
) -> None:
    plan = planner.plan(
        area_request("overview", "population", "housing", "event"),
        authorization=FULL_AUTH,
    )
    serialized = plan.model_dump_json()
    for token in FORBIDDEN_TOKENS:
        assert token not in serialized


def test_plan_references_request_and_catalog(
    planner: AnalysisPlanner, catalog: SemanticCatalog
) -> None:
    plan = planner.plan(
        area_request("event", request_id="req-77"), authorization=FULL_AUTH
    )
    assert plan.request_id == "req-77"
    assert plan.catalog_version == catalog.catalog_version
    assert plan.schema_version == "1.0"
    assert plan.plan_id.startswith("sha256:")
