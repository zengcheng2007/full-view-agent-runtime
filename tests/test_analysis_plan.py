"""P1-2 ??????????/?????????

???????????????? + scope ?? + ???? + ?????
scope ?????????????? polygon ??????????
??????WKT????????? Schema ?????????
??????URL??? token ??????????
"""

import pytest
from pydantic import TypeAdapter, ValidationError

from full_view_agent.domain.analysis_plan import (
    AnalysisOmission,
    AnalysisPlan,
    AnalysisRequest,
    AnalysisScopeRef,
    AnalysisStep,
    AreaScopeRef,
    PlanBudget,
    SavedPolygonScopeRef,
)
from full_view_agent.domain.models import MetricQueryScope

AREA_REF = AreaScopeRef(scope=MetricQueryScope(area_code="330106"))
SCOPE_REF_ADAPTER = TypeAdapter(AnalysisScopeRef)
TEST_CATALOG_FINGERPRINT = "sha256:" + "0" * 64


def make_step(
    *,
    subject: str = "population",
    step_id: str | None = None,
    goals: tuple[str, ...] = ("overview",),
    depends_on: tuple[str, ...] = (),
    timeout_ms: int = 10_000,
) -> AnalysisStep:
    return AnalysisStep(
        step_id=step_id or f"step-{subject}",
        goals=goals,
        subject=subject,
        scope_ref=AREA_REF,
        capability_id=f"governance.query_{subject}_metrics",
        capability_version="1.0.0",
        depends_on=depends_on,
        timeout_ms=timeout_ms,
    )


def make_plan(
    *,
    steps: tuple[AnalysisStep, ...] = (),
    omissions: tuple[AnalysisOmission, ...] = (),
    constraints: PlanBudget | None = None,
    catalog_fingerprint: str = TEST_CATALOG_FINGERPRINT,
) -> AnalysisPlan:
    return AnalysisPlan(
        plan_id="sha256:" + "1" * 64,
        catalog_version="0.1.0-s0-candidate",
        catalog_fingerprint=catalog_fingerprint,
        request_id="req-01",
        goals=("overview",),
        scope_ref=AREA_REF,
        steps=steps,
        omissions=omissions,
        constraints=constraints or PlanBudget(),
    )


# ---------------------------------------------------------------------------
# scope ????????????
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "area_code",
    ["3301", "330106", "330106001", "330106001001", "330106001001001"],
)
def test_area_scope_ref_accepts_supported_levels(area_code: str) -> None:
    ref = AreaScopeRef(scope=MetricQueryScope(area_code=area_code))
    assert ref.kind == "area"
    assert ref.scope.area_code == area_code


@pytest.mark.parametrize(
    "area_code",
    [
        "33.01",  # ?????????
        "area-3301",  # ?????
        "33010",  # ???????5 ??
        "3301060010010011",  # ???16 ??
        "",  # ???
    ],
)
def test_area_scope_ref_rejects_invalid_codes(area_code: str) -> None:
    with pytest.raises(ValidationError):
        AreaScopeRef(scope=MetricQueryScope(area_code=area_code))


# ---------------------------------------------------------------------------
# scope ?????? polygon ???????????
# ---------------------------------------------------------------------------


def test_saved_polygon_scope_ref_accepts_opaque_reference() -> None:
    ref = SavedPolygonScopeRef(polygon_ref="saved-polygon-01")
    assert ref.kind == "saved_polygon"
    assert ref.polygon_ref == "saved-polygon-01"


@pytest.mark.parametrize(
    "polygon_ref",
    [
        "POLYGON((120.1 30.2, 120.2 30.3))",  # WKT
        "120.1,30.2",  # ????
        "120.1 30.2",  # ??????
        "30.25N,120.15E",  # ?????
        "?? A",  # ??/?????
    ],
)
def test_saved_polygon_scope_ref_rejects_coordinate_like_values(
    polygon_ref: str,
) -> None:
    with pytest.raises(ValidationError):
        SavedPolygonScopeRef(polygon_ref=polygon_ref)


def test_scope_ref_rejects_inline_coordinate_fields() -> None:
    # extra=forbid???????/????? Schema ??????
    with pytest.raises(ValidationError):
        SavedPolygonScopeRef.model_validate(
            {
                "kind": "saved_polygon",
                "polygon_ref": "p-1",
                "coordinates": [[120.1, 30.2], [120.2, 30.3]],
            }
        )
    with pytest.raises(ValidationError):
        AreaScopeRef.model_validate(
            {
                "kind": "area",
                "scope": {"area_code": "330106"},
                "geometry": {"type": "Polygon"},
            }
        )


def test_scope_ref_union_rejects_unknown_kind() -> None:
    with pytest.raises(ValidationError):
        SCOPE_REF_ADAPTER.validate_python({"kind": "bbox", "bounds": [1, 2, 3, 4]})


def test_scope_ref_union_parses_both_kinds() -> None:
    area = SCOPE_REF_ADAPTER.validate_python(
        {"kind": "area", "scope": {"area_code": "330106"}}
    )
    assert isinstance(area, AreaScopeRef)
    polygon = SCOPE_REF_ADAPTER.validate_python(
        {"kind": "saved_polygon", "polygon_ref": "p-1"}
    )
    assert isinstance(polygon, SavedPolygonScopeRef)


# ---------------------------------------------------------------------------
# ????????? + ???
# ---------------------------------------------------------------------------


def test_request_rejects_unknown_goal() -> None:
    with pytest.raises(ValidationError):
        AnalysisRequest.model_validate(
            {
                "request_id": "req-01",
                "goals": ["traffic"],
                "scope_ref": {"kind": "area", "scope": {"area_code": "330106"}},
            }
        )


def test_request_requires_at_least_one_goal() -> None:
    with pytest.raises(ValidationError):
        AnalysisRequest(request_id="req-01", goals=(), scope_ref=AREA_REF)


def test_request_is_immutable() -> None:
    request = AnalysisRequest(
        request_id="req-01", goals=("population",), scope_ref=AREA_REF
    )
    with pytest.raises(ValidationError):
        request.request_id = "req-02"
    with pytest.raises(ValidationError):
        request.goals = ("event",)


# ---------------------------------------------------------------------------
# ??????? + ???????
# ---------------------------------------------------------------------------


def test_step_effect_is_read_only() -> None:
    with pytest.raises(ValidationError):
        AnalysisStep(
            step_id="step-x",
            goals=("overview",),
            subject="population",
            scope_ref=AREA_REF,
            capability_id="governance.query_population_metrics",
            capability_version="1.0.0",
            effect="write",
        )


def test_step_timeout_is_bounded() -> None:
    with pytest.raises(ValidationError):
        make_step(timeout_ms=50)
    with pytest.raises(ValidationError):
        make_step(timeout_ms=121_000)


# ---------------------------------------------------------------------------
# ??????????????????????????
# ---------------------------------------------------------------------------

SERVER_CEILING = PlanBudget(max_parallel=2, max_tool_calls=4, total_timeout_ms=30_000)


def test_budget_tightened_to_ceiling_never_expands() -> None:
    widened = PlanBudget(max_parallel=8, max_tool_calls=64, total_timeout_ms=600_000)
    assert widened.tightened_to(SERVER_CEILING) == SERVER_CEILING


def test_budget_tightened_to_keeps_smaller_request_values() -> None:
    tighter = PlanBudget(max_parallel=1, max_tool_calls=2, total_timeout_ms=10_000)
    assert tighter.tightened_to(SERVER_CEILING) == tighter


def test_budget_tightened_to_clamps_per_field() -> None:
    mixed = PlanBudget(max_parallel=1, max_tool_calls=64, total_timeout_ms=600_000)
    assert mixed.tightened_to(SERVER_CEILING) == PlanBudget(
        max_parallel=1, max_tool_calls=4, total_timeout_ms=30_000
    )


def test_budget_tightened_to_is_total_when_equal() -> None:
    assert SERVER_CEILING.tightened_to(SERVER_CEILING) == SERVER_CEILING


# ---------------------------------------------------------------------------
# ???????????????????
# ---------------------------------------------------------------------------


def test_plan_accepts_known_dependencies() -> None:
    plan = make_plan(
        steps=(
            make_step(subject="event", step_id="step-event"),
            make_step(
                subject="population",
                step_id="step-population",
                depends_on=("step-event",),
            ),
        )
    )
    assert plan.steps[1].depends_on == ("step-event",)


def test_plan_rejects_unknown_dependency() -> None:
    with pytest.raises(ValidationError, match="unknown steps"):
        make_plan(steps=(make_step(depends_on=("step-missing",)),))


def test_plan_rejects_self_dependency() -> None:
    with pytest.raises(ValidationError, match="itself"):
        make_plan(steps=(make_step(step_id="step-a", depends_on=("step-a",)),))


def test_plan_rejects_two_node_cycle() -> None:
    with pytest.raises(ValidationError, match="cycle"):
        make_plan(
            steps=(
                make_step(step_id="step-a", depends_on=("step-b",)),
                make_step(step_id="step-b", depends_on=("step-a",)),
            )
        )


def test_plan_rejects_three_node_cycle() -> None:
    with pytest.raises(ValidationError, match="cycle"):
        make_plan(
            steps=(
                make_step(step_id="step-a", depends_on=("step-c",)),
                make_step(step_id="step-b", depends_on=("step-a",)),
                make_step(step_id="step-c", depends_on=("step-b",)),
            )
        )


def test_plan_rejects_cycle_reachable_from_valid_prefix() -> None:
    # step-c ???????????????????
    with pytest.raises(ValidationError, match="cycle"):
        make_plan(
            steps=(
                make_step(step_id="step-a", depends_on=("step-b",)),
                make_step(step_id="step-b", depends_on=("step-a",)),
                make_step(step_id="step-c", depends_on=("step-a",)),
            )
        )


def test_plan_accepts_linear_chain_and_diamond() -> None:
    plan = make_plan(
        steps=(
            make_step(step_id="step-a"),
            make_step(step_id="step-b", depends_on=("step-a",)),
            make_step(step_id="step-c", depends_on=("step-b",)),
            make_step(step_id="step-d", depends_on=("step-b", "step-c")),
        )
    )
    assert len(plan.steps) == 4


def test_plan_rejects_duplicate_step_ids() -> None:
    with pytest.raises(ValidationError, match="duplicate"):
        make_plan(
            steps=(
                make_step(subject="event", step_id="step-a"),
                make_step(subject="housing", step_id="step-a"),
            )
        )


def test_plan_rejects_steps_over_tool_call_budget() -> None:
    with pytest.raises(ValidationError, match="max_tool_calls"):
        make_plan(
            steps=(
                make_step(subject="event", step_id="step-event"),
                make_step(subject="housing", step_id="step-housing"),
            ),
            constraints=PlanBudget(max_tool_calls=1),
        )


def test_plan_rejects_step_timeout_over_total_budget() -> None:
    with pytest.raises(ValidationError, match="total"):
        make_plan(
            steps=(make_step(timeout_ms=10_000),),
            constraints=PlanBudget(total_timeout_ms=1_000),
        )


def test_plan_allows_empty_steps_with_omissions() -> None:
    plan = make_plan(
        omissions=(
            AnalysisOmission(
                goals=("population",),
                subject="population",
                reason_code="NOT_ENTITLED",
                detail="?????? entitlement",
            ),
        )
    )
    assert plan.steps == ()
    assert plan.omissions[0].reason_code == "NOT_ENTITLED"


def test_plan_is_immutable() -> None:
    plan = make_plan(steps=(make_step(),))
    with pytest.raises(ValidationError):
        plan.plan_id = "sha256:tampered"


# ---------------------------------------------------------------------------
# ????????? + canonical ?????????
# ---------------------------------------------------------------------------


def test_plan_requires_catalog_fingerprint() -> None:
    with pytest.raises(ValidationError, match="catalog_fingerprint"):
        AnalysisPlan(
            plan_id="sha256:" + "1" * 64,
            catalog_version="0.1.0-s0-candidate",
            request_id="req-01",
            goals=("overview",),
            scope_ref=AREA_REF,
            constraints=PlanBudget(),
        )


@pytest.mark.parametrize(
    "fingerprint",
    [
        "",
        "0" * 64,  # ? sha256: ??
        "sha256:xyz",  # ?????/????
        "sha256:" + "A" * 64,  # ???????canonical ??????
        "sha256:" + "0" * 63,  # ????
        "sha256:" + "0" * 65,  # ????
        "md5:" + "0" * 64,  # ??????
    ],
)
def test_plan_rejects_malformed_catalog_fingerprint(fingerprint: str) -> None:
    with pytest.raises(ValidationError):
        make_plan(catalog_fingerprint=fingerprint)


def test_plan_accepts_canonical_catalog_fingerprint() -> None:
    plan = make_plan(catalog_fingerprint="sha256:" + "ab" * 32)
    assert plan.catalog_fingerprint == "sha256:" + "ab" * 32


# ---------------------------------------------------------------------------
# ??????????????????? token
# ---------------------------------------------------------------------------


def test_plan_serialization_excludes_physical_and_coordinate_tokens() -> None:
    plan = make_plan(
        steps=(make_step(),),
        omissions=(
            AnalysisOmission(
                goals=("housing",),
                subject="housing",
                reason_code="SUBJECT_NOT_BINDABLE",
                detail="Catalog ??????????????",
            ),
        ),
    )
    serialized = plan.model_dump_json()
    forbidden = (
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
        "POINT(",
    )
    for token in forbidden:
        assert token not in serialized
