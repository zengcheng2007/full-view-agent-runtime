from __future__ import annotations

import asyncio
from collections.abc import Sequence

import pytest

from full_view_agent.application.analysis_area_resolver import (
    CapabilityAnalysisAreaResolver,
)
from full_view_agent.application.analysis_intent_service import AnalysisIntentRejected
from full_view_agent.application.capability_service import CapabilityService
from full_view_agent.application.errors import (
    ReauthenticationRequired,
    UpstreamContractError,
    UpstreamTimeout,
    UpstreamUnavailable,
)
from full_view_agent.application.fingerprints import canonical_fingerprint
from full_view_agent.application.policy import MinimalPolicyAdapter
from full_view_agent.application.tool_registry import ToolRegistry
from full_view_agent.domain.models import (
    AreaCandidate,
    AreaCandidatesData,
    AreaCandidatesResult,
    ToolResult,
)
from full_view_agent.infrastructure.governance_adapter import (
    InMemoryGovernanceAdapter,
)

from .test_analysis_intent_service import (
    ALL_DATASETS,
    ALL_ENTITLEMENTS,
    build_service,
    make_auth_context,
    named_intent,
)


def area_result(candidates: Sequence[AreaCandidate]) -> AreaCandidatesResult:
    items = list(candidates)
    data = AreaCandidatesData(
        resolved_area_code=items[0].area_code if len(items) == 1 else None,
        ambiguous=len(items) > 1,
        candidates=items,
    )
    return AreaCandidatesResult(
        result_id="res-area",
        data_schema_ref="schema://data/area-candidates/1.0.0",
        result_fingerprint=canonical_fingerprint(
            domain="data-result:area-candidates:1.0.0",
            value=data,
        ),
        data=data,
        candidate_count=len(items),
    )


def tool_result(
    *,
    status: str = "success",
    data_result: AreaCandidatesResult | None = None,
    tool_id: str = "governance.resolve_area",
    warnings: list[str] | None = None,
) -> ToolResult:
    return ToolResult.model_validate(
        {
            "tool_call_id": "tcl-returned",
            "tool_id": tool_id,
            "tool_version": "1.0.0",
            "status": status,
            "summary": "area resolution",
            "data_result": data_result,
            "warnings": warnings or [],
        }
    )


class FakeCapability:
    def __init__(self, results: Sequence[ToolResult]) -> None:
        self._results = list(results)
        self.calls: list[dict[str, object]] = []

    async def execute(self, **kwargs: object) -> ToolResult:
        self.calls.append(kwargs)
        return self._results.pop(0).model_copy(
            update={"tool_call_id": kwargs["tool_call_id"]}
        )


async def test_bridge_forwards_auth_and_only_server_owned_arguments() -> None:
    auth = make_auth_context()
    candidate = AreaCandidate(
        area_code="330106",
        area_name="西湖区",
        level="district",
    )
    capability = FakeCapability([tool_result(data_result=area_result([candidate]))])
    resolver = CapabilityAnalysisAreaResolver(capability=capability)

    resolved = await resolver.resolve_area_query(query="西湖区", auth_context=auth)

    assert resolved[0].model_dump() == {"area_code": "330106", "area_name": "西湖区"}
    assert len(capability.calls) == 1
    call = capability.calls[0]
    assert call["tool_id"] == "governance.resolve_area"
    assert call["raw_arguments"] == {"query": "西湖区", "max_candidates": 20}
    assert call["auth_context"] is auth
    assert str(call["tool_call_id"]).startswith("tcl_")


@pytest.mark.parametrize(
    "candidates",
    [
        [],
        [AreaCandidate(area_code="330106", area_name="西湖区", level="district")],
        [
            AreaCandidate(area_code="330106", area_name="西湖区", level="district"),
            AreaCandidate(area_code="330108", area_name="滨江区", level="district"),
        ],
    ],
)
async def test_bridge_preserves_zero_one_and_many_candidates(
    candidates: list[AreaCandidate],
) -> None:
    capability = FakeCapability([tool_result(data_result=area_result(candidates))])
    resolver = CapabilityAnalysisAreaResolver(capability=capability)

    resolved = await resolver.resolve_area_query(
        query="区域",
        auth_context=make_auth_context(),
    )

    assert [item.area_code for item in resolved] == [
        item.area_code for item in candidates
    ]


@pytest.mark.parametrize(
    ("status", "warnings", "error_type"),
    [
        ("denied", [], AnalysisIntentRejected),
        ("denied", ["AUTH_CONTEXT_EXPIRED"], ReauthenticationRequired),
        ("failed", ["upstream_timeout"], UpstreamTimeout),
        ("failed", ["upstream_unavailable"], UpstreamUnavailable),
        ("failed", ["upstream_contract_error"], UpstreamContractError),
    ],
)
async def test_bridge_maps_controlled_capability_failures(
    status: str,
    warnings: list[str],
    error_type: type[Exception],
) -> None:
    capability = FakeCapability(
        [tool_result(status=status, data_result=None, warnings=warnings)]
    )
    resolver = CapabilityAnalysisAreaResolver(capability=capability)

    with pytest.raises(error_type):
        await resolver.resolve_area_query(
            query="西湖区",
            auth_context=make_auth_context(),
        )


def malformed_results() -> list[ToolResult]:
    valid = area_result(
        [AreaCandidate(area_code="330106", area_name="西湖区", level="district")]
    )
    return [
        tool_result(data_result=valid, tool_id="governance.query_population_metrics"),
        tool_result(data_result=valid).model_copy(update={"tool_version": "9.9.9"}),
        tool_result(status="partial", data_result=valid),
        tool_result(data_result=valid.model_copy(update={"payload_status": "expired"})),
        tool_result(data_result=valid.model_copy(update={"data_schema_ref": "bad"})),
        tool_result(data_result=valid.model_copy(update={"candidate_count": 0})),
        tool_result(data_result=valid.model_copy(update={"result_fingerprint": "bad"})),
        tool_result(
            data_result=valid.model_copy(
                update={"data": valid.data.model_copy(update={"ambiguous": True})}
            )
        ),
    ]


@pytest.mark.parametrize("result", malformed_results())
async def test_bridge_rejects_malformed_or_incomplete_results(
    result: ToolResult,
) -> None:
    resolver = CapabilityAnalysisAreaResolver(capability=FakeCapability([result]))

    with pytest.raises(UpstreamContractError):
        await resolver.resolve_area_query(
            query="西湖区",
            auth_context=make_auth_context(),
        )


async def test_concurrent_calls_do_not_share_auth_context() -> None:
    first_auth = make_auth_context(user_id="user-01", run_id="run-01")
    second_auth = make_auth_context(user_id="user-02", run_id="run-02")
    empty = tool_result(data_result=area_result([]))
    capability = FakeCapability([empty, empty])
    resolver = CapabilityAnalysisAreaResolver(capability=capability)

    await asyncio.gather(
        resolver.resolve_area_query(query="未知一", auth_context=first_auth),
        resolver.resolve_area_query(query="未知二", auth_context=second_auth),
    )

    assert {id(call["auth_context"]) for call in capability.calls} == {
        id(first_auth),
        id(second_auth),
    }


async def test_real_capability_chain_compiles_named_area_to_trusted_plan() -> None:
    capability = CapabilityService(
        registry=ToolRegistry.default(),
        policy=MinimalPolicyAdapter(),
        adapter=InMemoryGovernanceAdapter(),
    )
    resolver = CapabilityAnalysisAreaResolver(capability=capability)
    service, repository = build_service(resolver=resolver)
    auth = make_auth_context(
        entitlements=(*ALL_ENTITLEMENTS, "governance.area.read"),
        datasets=(*ALL_DATASETS, "administrative_area"),
    )

    plan = await service.compile_intent(
        named_intent("housing", area_query="西湖区"),
        auth_context=auth,
    )

    assert plan.scope_ref.scope.area_code == "330106"
    assert [step.subject for step in plan.steps] == ["housing"]
    loaded = await repository.get(
        tenant_id=auth.principal.tenant_id,
        user_id=auth.principal.user_id,
        run_id=auth.run_id,
        plan_id=plan.plan_id,
    )
    assert loaded == plan


async def test_real_capability_chain_denies_before_adapter_without_area_access() -> None:
    capability = CapabilityService(
        registry=ToolRegistry.default(),
        policy=MinimalPolicyAdapter(),
        adapter=InMemoryGovernanceAdapter(),
    )
    resolver = CapabilityAnalysisAreaResolver(capability=capability)

    with pytest.raises(AnalysisIntentRejected) as exc_info:
        await resolver.resolve_area_query(
            query="西湖区",
            auth_context=make_auth_context(),
        )

    assert exc_info.value.code == "AREA_RESOLUTION_NOT_AUTHORIZED"
