"""P1-3/P1-4 研判报告组装器：可信加载、服务端重推导与 Result 生命周期闭环。

公开 API：``AnalysisReportAssembler(catalog, planner, plan_repository,
resolver, result_store)``、``await assembler.assemble(plan_id, request_id,
auth_context, execution)`` 与 ``await assembler.assemble_and_save(...)``。
计划只能经服务端可信仓储加载；子 Result 由执行器经既有 AgentStore
落库，报告引用必须可读取且内容一致；报告本身是既有 DataResult union
成员，具备服务端确定性 result_id/fingerprint，并可保存重读一致。
"""

from __future__ import annotations

import json
from collections.abc import Callable

import pytest
from pydantic import TypeAdapter

from full_view_agent.application.analysis_executor import AnalysisExecutionRejected
from full_view_agent.application.analysis_planner import AnalysisPlanner
from full_view_agent.application.analysis_report import (
    ANALYSIS_REPORT_DATA_SCHEMA_REF,
    AnalysisReportAssembler,
    AnalysisReportAssemblyError,
)
from full_view_agent.application.errors import ResourceNotFound, RunStateConflict
from full_view_agent.domain.analysis_execution import AnalysisExecutionResult
from full_view_agent.domain.analysis_plan import AnalysisOmission
from full_view_agent.domain.analysis_report import (
    AnalysisReportDataResult,
    AnalysisReportSection,
)
from full_view_agent.domain.models import (
    AuthContext,
    DataResult,
    EventFinishRateRow,
    EventFinishRateTable,
)
from full_view_agent.infrastructure.memory_store import InMemoryAgentStore
from full_view_agent.semantic.authorization import SubjectAuthorization

from .test_analysis_executor import (
    _execute_loaded,
    _executor,
    _full_auth_context,
    _overview_plan,
    _replace_steps,
    _seed_result_store,
)
from .test_analysis_planner import area_request


def _assembler(port, catalog, store: InMemoryAgentStore) -> AnalysisReportAssembler:
    return AnalysisReportAssembler(
        catalog=catalog,
        planner=port.planner,
        plan_repository=port.plan_repository,
        resolver=port.resolver,
        result_store=store,
    )


async def _run_analysis(
    executor,
    port,
    plan,
    store: InMemoryAgentStore,
    *,
    auth_context: AuthContext,
    trusted_replanned: bool = False,
) -> AnalysisExecutionResult:
    """真实执行；可用子结果由执行器经既有 Result 生命周期落库。"""
    await _seed_result_store(store, auth_context=auth_context)
    return await _execute_loaded(
        executor,
        port,
        plan,
        auth_context=auth_context,
        trusted_replanned=trusted_replanned,
    )


@pytest.mark.asyncio
async def test_real_three_subject_execution_chain_assembles_successfully() -> None:
    executor, port, catalog = _executor()
    plan = _overview_plan(catalog)
    auth = _full_auth_context()
    store = port.result_store
    assert store is not None
    execution = await _run_analysis(executor, port, plan, store, auth_context=auth)

    report = await _assembler(port, catalog, store).assemble(
        plan_id=plan.plan_id,
        request_id=plan.request_id,
        auth_context=auth,
        execution=execution,
    )

    assert report.status == "completed"
    assert [section.subject for section in report.sections] == [
        "event",
        "housing",
        "population",
    ]
    assert all(section.result_ref is not None for section in report.sections)


@pytest.mark.asyncio
async def test_three_subject_success_builds_ordered_reference_only_report() -> None:
    executor, port, catalog = _executor()
    plan = _overview_plan(catalog)
    auth = _full_auth_context()
    store = port.result_store
    assert store is not None
    execution = await _run_analysis(executor, port, plan, store, auth_context=auth)

    report = await _assembler(port, catalog, store).assemble(
        plan_id=plan.plan_id,
        request_id=plan.request_id,
        auth_context=auth,
        execution=execution,
    )

    assert [section.subject for section in report.sections] == [
        step.subject for step in plan.steps
    ]
    result_refs = [section.result_ref for section in report.sections]
    assert all(result_ref is not None for result_ref in result_refs)
    concrete_refs = [result_ref for result_ref in result_refs if result_ref is not None]
    assert all(result_ref.kind == "table" for result_ref in concrete_refs)
    assert report.limitations == ()
    # 每个子 Result 引用都必须能在可信 Result Store 中读取且指纹一致。
    for result_ref in concrete_refs:
        stored = await store.get_result(
            user_id=auth.principal.user_id, result_id=result_ref.result_id
        )
        assert stored.result_fingerprint == result_ref.result_fingerprint
    dumped = report.model_dump(mode="json")
    assert "rows" not in json.dumps(dumped)
    assert all("data" not in section for section in dumped["sections"])


@pytest.mark.asyncio
async def test_single_partial_subject_makes_partial_report_and_limitation() -> None:
    executor, port, catalog = _executor(statuses={"housing": "partial"})
    plan = _overview_plan(catalog)
    auth = _full_auth_context()
    store = port.result_store
    assert store is not None
    execution = await _run_analysis(executor, port, plan, store, auth_context=auth)

    report = await _assembler(port, catalog, store).assemble(
        plan_id=plan.plan_id,
        request_id=plan.request_id,
        auth_context=auth,
        execution=execution,
    )

    assert report.status == "partial"
    housing = next(item for item in report.sections if item.subject == "housing")
    assert housing.status == "partial"
    assert housing.result_ref is not None
    assert [(item.subject, item.status) for item in report.limitations] == [
        ("housing", "partial")
    ]


@pytest.mark.asyncio
async def test_all_failed_report_has_no_result_references_or_zero_values() -> None:
    executor, port, catalog = _executor(
        statuses={"event": "failed", "housing": "denied"},
        delays={"population": 0.3},
    )
    plan = _overview_plan(catalog)
    steps = tuple(
        step.model_copy(update={"timeout_ms": 100})
        if step.subject == "population"
        else step
        for step in plan.steps
    )
    plan = _replace_steps(plan, steps)
    auth = _full_auth_context()
    store = port.result_store
    assert store is not None
    execution = await _run_analysis(
        executor, port, plan, store, auth_context=auth, trusted_replanned=True
    )

    # 超时预算变更计划非 Planner 原生产物；组装器共享的受控 Planner
    # 需要同一重规划基准，其余加载门禁（快照/绑定/ID）保持生效。
    port.planner.expected_plan = plan
    try:
        report = await _assembler(port, catalog, store).assemble(
            plan_id=plan.plan_id,
            request_id=plan.request_id,
            auth_context=auth,
            execution=execution,
        )
    finally:
        port.planner.expected_plan = None

    assert report.status == "failed"
    assert all(section.result_ref is None for section in report.sections)
    assert [item.status for item in report.limitations] == [
        "failed",
        "denied",
        "timeout",
    ]
    dumped = report.model_dump(mode="json")
    assert all(
        "value" not in section and "row_count" not in section
        for section in dumped["sections"]
    )


@pytest.mark.asyncio
async def test_plan_omission_is_preserved_and_prevents_completed_status() -> None:
    executor, port, catalog = _executor()
    full_auth = _full_auth_context()
    auth = full_auth.model_copy(
        update={
            "entitlements": [
                entitlement
                for entitlement in full_auth.entitlements
                if entitlement != "governance.housing.aggregate.read"
            ]
        }
    )
    plan = AnalysisPlanner(catalog).plan(
        area_request("overview"),
        authorization=SubjectAuthorization.from_auth_context(auth),
    )
    assert [omission.subject for omission in plan.omissions] == ["housing"]
    store = port.result_store
    assert store is not None
    execution = await _run_analysis(executor, port, plan, store, auth_context=auth)

    report = await _assembler(port, catalog, store).assemble(
        plan_id=plan.plan_id,
        request_id=plan.request_id,
        auth_context=auth,
        execution=execution,
    )

    assert report.status == "partial"
    assert report.omissions == plan.omissions


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (
            lambda result: result.model_copy(update={"plan_id": "wrong"}),
            "EXECUTION_PLAN_MISMATCH",
        ),
        (
            lambda result: result.model_copy(update={"request_id": "wrong"}),
            "EXECUTION_REQUEST_MISMATCH",
        ),
        (
            lambda result: result.model_copy(
                update={"steps": tuple(reversed(result.steps))}
            ),
            "STEP_ORDER_MISMATCH",
        ),
        (
            lambda result: result.model_copy(
                update={"steps": (*result.steps, result.steps[0])}
            ),
            "STEP_COUNT_MISMATCH",
        ),
    ],
)
async def test_identity_duplicate_and_order_pollution_fail_closed(
    mutate: Callable[[AnalysisExecutionResult], AnalysisExecutionResult],
    code: str,
) -> None:
    executor, port, catalog = _executor()
    plan = _overview_plan(catalog)
    auth = _full_auth_context()
    store = port.result_store
    assert store is not None
    execution = await _run_analysis(executor, port, plan, store, auth_context=auth)

    with pytest.raises(AnalysisReportAssemblyError) as exc_info:
        await _assembler(port, catalog, store).assemble(
            plan_id=plan.plan_id,
            request_id=plan.request_id,
            auth_context=auth,
            execution=mutate(execution),
        )

    assert exc_info.value.code == code


@pytest.mark.asyncio
async def test_duplicate_subject_plan_is_rejected_at_the_trusted_loader() -> None:
    executor, port, catalog = _executor()
    plan = _overview_plan(catalog)
    auth = _full_auth_context()
    store = port.result_store
    assert store is not None
    execution = await _run_analysis(executor, port, plan, store, auth_context=auth)
    duplicate = plan.steps[1].model_copy(update={"subject": plan.steps[0].subject})
    polluted_plan = plan.model_copy(
        update={"steps": (plan.steps[0], duplicate, plan.steps[2])}
    )
    # 仓储按 plan_id 命中污染计划；组装器的可信加载边界必须先 fail closed。
    port.plan_repository.plans[
        (
            auth.principal.tenant_id,
            auth.principal.user_id,
            auth.run_id,
            plan.plan_id,
        )
    ] = polluted_plan

    with pytest.raises(AnalysisExecutionRejected) as exc_info:
        await _assembler(port, catalog, store).assemble(
            plan_id=plan.plan_id,
            request_id=plan.request_id,
            auth_context=auth,
            execution=execution,
        )

    assert exc_info.value.code == "CAPABILITY_BINDING_MISMATCH"


@pytest.mark.asyncio
async def test_plan_id_must_bind_the_canonical_plan_content() -> None:
    executor, port, catalog = _executor()
    plan = _overview_plan(catalog)
    auth = _full_auth_context()
    store = port.result_store
    assert store is not None
    execution = await _run_analysis(executor, port, plan, store, auth_context=auth)
    polluted_plan = plan.model_copy(update={"goals": ("population",)})
    port.plan_repository.plans[
        (
            auth.principal.tenant_id,
            auth.principal.user_id,
            auth.run_id,
            plan.plan_id,
        )
    ] = polluted_plan

    with pytest.raises(AnalysisExecutionRejected) as exc_info:
        await _assembler(port, catalog, store).assemble(
            plan_id=plan.plan_id,
            request_id=plan.request_id,
            auth_context=auth,
            execution=execution,
        )

    assert exc_info.value.code == "PLAN_ID_MISMATCH"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["success", "partial"])
async def test_usable_step_without_child_result_fails_closed(status: str) -> None:
    statuses = {"housing": "partial"} if status == "partial" else None
    executor, port, catalog = _executor(statuses=statuses)
    plan = _overview_plan(catalog)
    auth = _full_auth_context()
    store = port.result_store
    assert store is not None
    execution = await _run_analysis(executor, port, plan, store, auth_context=auth)
    housing = execution.steps[1].model_copy(update={"tool_result": None})
    polluted = execution.model_copy(
        update={"steps": (execution.steps[0], housing, execution.steps[2])}
    )

    with pytest.raises(AnalysisReportAssemblyError) as exc_info:
        await _assembler(port, catalog, store).assemble(
            plan_id=plan.plan_id,
            request_id=plan.request_id,
            auth_context=auth,
            execution=polluted,
        )

    assert exc_info.value.code == "CHILD_RESULT_REQUIRED"


@pytest.mark.asyncio
async def test_failed_step_cannot_carry_a_success_result_reference() -> None:
    executor, port, catalog = _executor(statuses={"housing": "failed"})
    plan = _overview_plan(catalog)
    auth = _full_auth_context()
    store = port.result_store
    assert store is not None
    execution = await _run_analysis(executor, port, plan, store, auth_context=auth)
    polluted = execution.steps[1].model_copy(
        update={"tool_result": execution.steps[0].tool_result}
    )
    execution = execution.model_copy(
        update={"steps": (execution.steps[0], polluted, execution.steps[2])}
    )

    with pytest.raises(AnalysisReportAssemblyError) as exc_info:
        await _assembler(port, catalog, store).assemble(
            plan_id=plan.plan_id,
            request_id=plan.request_id,
            auth_context=auth,
            execution=execution,
        )

    assert exc_info.value.code == "NON_USABLE_STEP_RESULT_FORBIDDEN"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["failed", "denied"])
async def test_failed_or_denied_tool_result_is_retained_only_as_a_limitation(
    status: str,
) -> None:
    executor, port, catalog = _executor(statuses={"housing": status})
    plan = _overview_plan(catalog)
    auth = _full_auth_context()
    store = port.result_store
    assert store is not None
    execution = await _run_analysis(executor, port, plan, store, auth_context=auth)

    report = await _assembler(port, catalog, store).assemble(
        plan_id=plan.plan_id,
        request_id=plan.request_id,
        auth_context=auth,
        execution=execution,
    )

    assert report.status == "partial"
    assert report.sections[1].result_ref is None
    assert report.limitations[0].status == status


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("update", "code"),
    [
        ({"tool_id": "governance.query_population_metrics"}, "CHILD_TOOL_MISMATCH"),
        ({"status": "partial"}, "CHILD_STATUS_MISMATCH"),
    ],
)
async def test_child_tool_identity_and_status_mismatch_fail_closed(
    update: dict[str, object], code: str
) -> None:
    executor, port, catalog = _executor()
    plan = _overview_plan(catalog)
    auth = _full_auth_context()
    store = port.result_store
    assert store is not None
    execution = await _run_analysis(executor, port, plan, store, auth_context=auth)
    first = execution.steps[0]
    assert first.tool_result is not None
    polluted_tool = first.tool_result.model_copy(update=update)
    polluted_step = first.model_copy(update={"tool_result": polluted_tool})
    execution = execution.model_copy(
        update={"steps": (polluted_step, *execution.steps[1:])}
    )

    with pytest.raises(AnalysisReportAssemblyError) as exc_info:
        await _assembler(port, catalog, store).assemble(
            plan_id=plan.plan_id,
            request_id=plan.request_id,
            auth_context=auth,
            execution=execution,
        )

    assert exc_info.value.code == code


@pytest.mark.asyncio
async def test_child_tool_call_id_must_bind_the_plan_step() -> None:
    executor, port, catalog = _executor()
    plan = _overview_plan(catalog)
    auth = _full_auth_context()
    store = port.result_store
    assert store is not None
    execution = await _run_analysis(executor, port, plan, store, auth_context=auth)
    first = execution.steps[0]
    assert first.tool_result is not None
    tool = first.tool_result.model_copy(update={"tool_call_id": "forged-call"})
    step = first.model_copy(update={"tool_result": tool})
    execution = execution.model_copy(
        update={"steps": (step, *execution.steps[1:])}
    )

    with pytest.raises(AnalysisReportAssemblyError) as exc_info:
        await _assembler(port, catalog, store).assemble(
            plan_id=plan.plan_id,
            request_id=plan.request_id,
            auth_context=auth,
            execution=execution,
        )

    assert exc_info.value.code == "CHILD_TOOL_CALL_MISMATCH"


@pytest.mark.asyncio
async def test_child_lineage_subject_mismatch_fails_closed() -> None:
    executor, port, catalog = _executor()
    plan = _overview_plan(catalog)
    auth = _full_auth_context()
    store = port.result_store
    assert store is not None
    execution = await _run_analysis(executor, port, plan, store, auth_context=auth)
    first = execution.steps[0]
    assert first.tool_result is not None
    assert first.tool_result.semantic_lineage is not None
    lineage = first.tool_result.semantic_lineage.model_copy(
        update={"subject": "housing"}
    )
    tool = first.tool_result.model_copy(update={"semantic_lineage": lineage})
    step = first.model_copy(update={"tool_result": tool})
    execution = execution.model_copy(
        update={"steps": (step, *execution.steps[1:])}
    )

    with pytest.raises(AnalysisReportAssemblyError) as exc_info:
        await _assembler(port, catalog, store).assemble(
            plan_id=plan.plan_id,
            request_id=plan.request_id,
            auth_context=auth,
            execution=execution,
        )

    assert exc_info.value.code == "CHILD_LINEAGE_MISMATCH"


@pytest.mark.asyncio
async def test_child_lineage_capability_binding_mismatch_fails_closed() -> None:
    executor, port, catalog = _executor()
    plan = _overview_plan(catalog)
    auth = _full_auth_context()
    store = port.result_store
    assert store is not None
    execution = await _run_analysis(executor, port, plan, store, auth_context=auth)
    first = execution.steps[0]
    assert first.tool_result is not None
    assert first.tool_result.semantic_lineage is not None
    lineage = first.tool_result.semantic_lineage.model_copy(
        update={"canonical_tool_version": "9.9.9"}
    )
    tool = first.tool_result.model_copy(update={"semantic_lineage": lineage})
    step = first.model_copy(update={"tool_result": tool})
    execution = execution.model_copy(
        update={"steps": (step, *execution.steps[1:])}
    )

    with pytest.raises(AnalysisReportAssemblyError) as exc_info:
        await _assembler(port, catalog, store).assemble(
            plan_id=plan.plan_id,
            request_id=plan.request_id,
            auth_context=auth,
            execution=execution,
        )

    assert exc_info.value.code == "CHILD_LINEAGE_MISMATCH"


@pytest.mark.asyncio
async def test_malformed_or_forged_child_fingerprint_fails_closed() -> None:
    executor, port, catalog = _executor()
    plan = _overview_plan(catalog)
    auth = _full_auth_context()
    store = port.result_store
    assert store is not None
    execution = await _run_analysis(executor, port, plan, store, auth_context=auth)
    first = execution.steps[0]
    assert first.tool_result is not None and first.tool_result.data_result is not None
    data = first.tool_result.data_result.model_copy(
        update={"result_fingerprint": "forged"}
    )
    tool = first.tool_result.model_copy(update={"data_result": data})
    step = first.model_copy(update={"tool_result": tool})
    execution = execution.model_copy(
        update={"steps": (step, *execution.steps[1:])}
    )

    with pytest.raises(AnalysisReportAssemblyError) as exc_info:
        await _assembler(port, catalog, store).assemble(
            plan_id=plan.plan_id,
            request_id=plan.request_id,
            auth_context=auth,
            execution=execution,
        )

    assert exc_info.value.code == "CHILD_RESULT_FINGERPRINT_MISMATCH"


@pytest.mark.asyncio
async def test_unknown_child_result_kind_fails_closed() -> None:
    executor, port, catalog = _executor()
    plan = _overview_plan(catalog)
    auth = _full_auth_context()
    store = port.result_store
    assert store is not None
    execution = await _run_analysis(executor, port, plan, store, auth_context=auth)
    first = execution.steps[0]
    assert first.tool_result is not None and first.tool_result.data_result is not None
    unknown = first.tool_result.data_result.model_copy(update={"kind": "metric"})
    tool = first.tool_result.model_copy(update={"data_result": unknown})
    step = first.model_copy(update={"tool_result": tool})
    execution = execution.model_copy(
        update={"steps": (step, *execution.steps[1:])}
    )

    with pytest.raises(AnalysisReportAssemblyError) as exc_info:
        await _assembler(port, catalog, store).assemble(
            plan_id=plan.plan_id,
            request_id=plan.request_id,
            auth_context=auth,
            execution=execution,
        )

    assert exc_info.value.code == "EXECUTION_CONTRACT_INVALID"


@pytest.mark.asyncio
async def test_child_result_must_be_readable_from_the_trusted_result_store() -> None:
    executor, port, catalog = _executor()
    plan = _overview_plan(catalog)
    auth = _full_auth_context()
    store = port.result_store
    assert store is not None
    execution = await _run_analysis(executor, port, plan, store, auth_context=auth)

    # 未持久化的子结果不能进入报告引用。
    empty_store = InMemoryAgentStore()
    await _seed_result_store(empty_store, auth_context=auth)
    with pytest.raises(AnalysisReportAssemblyError) as missing_error:
        await _assembler(port, catalog, empty_store).assemble(
            plan_id=plan.plan_id,
            request_id=plan.request_id,
            auth_context=auth,
            execution=execution,
        )
    assert missing_error.value.code == "CHILD_RESULT_NOT_STORED"

    # 存储内容与执行证据不一致时同样 fail closed。
    first_result = execution.steps[0].tool_result
    assert first_result is not None and first_result.data_result is not None
    forged_data = EventFinishRateTable(
        rows=[EventFinishRateRow(level="community", finish_rate=1)]
    )
    await store.save_result(
        user_id=auth.principal.user_id,
        run_id=auth.run_id,
        result=first_result.data_result.model_copy(update={"data": forged_data}),
    )

    with pytest.raises(AnalysisReportAssemblyError) as mismatch_error:
        await _assembler(port, catalog, store).assemble(
            plan_id=plan.plan_id,
            request_id=plan.request_id,
            auth_context=auth,
            execution=execution,
        )
    assert mismatch_error.value.code == "CHILD_RESULT_STORE_MISMATCH"


@pytest.mark.asyncio
async def test_overall_status_and_omission_drift_fail_closed() -> None:
    executor, port, catalog = _executor()
    plan = _overview_plan(catalog)
    auth = _full_auth_context()
    store = port.result_store
    assert store is not None
    execution = await _run_analysis(executor, port, plan, store, auth_context=auth)
    assembler = _assembler(port, catalog, store)

    bad_status = execution.model_copy(update={"status": "partial"})
    with pytest.raises(AnalysisReportAssemblyError) as status_error:
        await assembler.assemble(
            plan_id=plan.plan_id,
            request_id=plan.request_id,
            auth_context=auth,
            execution=bad_status,
        )
    assert status_error.value.code == "EXECUTION_STATUS_MISMATCH"

    omission = AnalysisOmission(
        goals=("housing",),
        subject="housing",
        reason_code="NOT_ENTITLED",
        detail="polluted",
    )
    bad_omission = execution.model_copy(update={"omissions": (omission,)})
    with pytest.raises(AnalysisReportAssemblyError) as omission_error:
        await assembler.assemble(
            plan_id=plan.plan_id,
            request_id=plan.request_id,
            auth_context=auth,
            execution=bad_omission,
        )
    assert omission_error.value.code == "EXECUTION_OMISSIONS_MISMATCH"


@pytest.mark.asyncio
async def test_report_round_trips_and_future_extensions_are_empty() -> None:
    executor, port, catalog = _executor()
    plan = _overview_plan(catalog)
    auth = _full_auth_context()
    store = port.result_store
    assert store is not None
    execution = await _run_analysis(executor, port, plan, store, auth_context=auth)

    report = await _assembler(port, catalog, store).assemble(
        plan_id=plan.plan_id,
        request_id=plan.request_id,
        auth_context=auth,
        execution=execution,
    )
    restored = AnalysisReportDataResult.model_validate_json(
        report.model_dump_json()
    )

    assert restored == report
    assert restored.extensions.derived_metric_refs == ()
    assert restored.extensions.evidence_graph_refs == ()

    with pytest.raises(ValueError, match="result reference"):
        AnalysisReportSection(
            step_id="step-event",
            subject="event",
            status="failed",
            reason_code="STEP_FAILED",
            result_ref=report.sections[0].result_ref,
        )


@pytest.mark.asyncio
async def test_report_contract_itself_rejects_status_and_limitation_pollution() -> None:
    executor, port, catalog = _executor(statuses={"housing": "partial"})
    plan = _overview_plan(catalog)
    auth = _full_auth_context()
    store = port.result_store
    assert store is not None
    execution = await _run_analysis(executor, port, plan, store, auth_context=auth)

    report = await _assembler(port, catalog, store).assemble(
        plan_id=plan.plan_id,
        request_id=plan.request_id,
        auth_context=auth,
        execution=execution,
    )

    with pytest.raises(ValueError, match="overall status"):
        AnalysisReportDataResult.model_validate(
            {**report.model_dump(mode="python"), "status": "completed"}
        )
    with pytest.raises(ValueError, match="limitations"):
        AnalysisReportDataResult.model_validate(
            {**report.model_dump(mode="python"), "limitations": ()}
        )


@pytest.mark.asyncio
async def test_executor_and_assembler_form_a_closed_result_lifecycle() -> None:
    executor, port, catalog = _executor()
    plan = _overview_plan(catalog)
    auth = _full_auth_context()
    store = port.result_store
    assert store is not None
    execution = await _run_analysis(executor, port, plan, store, auth_context=auth)
    assembler = _assembler(port, catalog, store)

    report = await assembler.assemble_and_save(
        plan_id=plan.plan_id,
        request_id=plan.request_id,
        auth_context=auth,
        execution=execution,
    )

    # 报告本身是既有 Result 生命周期成员：确定性身份 + 可读回。
    assert report.kind == "analysis_report"
    assert report.data_schema_ref == ANALYSIS_REPORT_DATA_SCHEMA_REF
    assert report.result_id.startswith("sha256:")
    assert report.result_fingerprint.startswith("sha256:")
    stored = await store.get_result(
        user_id=auth.principal.user_id, result_id=report.result_id
    )
    assert stored == report
    # 既有 DataResult union（PG/memory 共用同一解码语义）可解析。
    adapter = TypeAdapter(DataResult)
    restored = adapter.validate_json(report.model_dump_json())
    assert isinstance(restored, AnalysisReportDataResult)
    assert restored == report
    # 报告不复制子结果 payload。
    assert "rows" not in report.model_dump_json()

    # 保存-重读幂等：相同执行证据产生相同身份，覆盖写不产生重复条目。
    entries_before = len(store.results)
    again = await assembler.assemble_and_save(
        plan_id=plan.plan_id,
        request_id=plan.request_id,
        auth_context=auth,
        execution=execution,
    )
    assert again.result_id == report.result_id
    assert again.result_fingerprint == report.result_fingerprint
    assert len(store.results) == entries_before


@pytest.mark.asyncio
async def test_report_identity_is_deterministic_and_pure_assemble_has_no_side_effects() -> None:
    executor, port, catalog = _executor()
    plan = _overview_plan(catalog)
    auth = _full_auth_context()
    store = port.result_store
    assert store is not None
    execution = await _run_analysis(executor, port, plan, store, auth_context=auth)
    assembler = _assembler(port, catalog, store)

    first = await assembler.assemble(
        plan_id=plan.plan_id,
        request_id=plan.request_id,
        auth_context=auth,
        execution=execution,
    )
    second = await assembler.assemble(
        plan_id=plan.plan_id,
        request_id=plan.request_id,
        auth_context=auth,
        execution=execution,
    )

    assert first.result_id == second.result_id
    assert first.result_fingerprint == second.result_fingerprint
    # assemble 不落库；只有 assemble_and_save 进入 Result 生命周期。
    with pytest.raises(ResourceNotFound):
        await store.get_result(
            user_id=auth.principal.user_id, result_id=first.result_id
        )


@pytest.mark.asyncio
async def test_report_save_failure_fails_closed_without_a_stored_report() -> None:
    class _ReportRejectingStore(InMemoryAgentStore):
        async def save_result(
            self, *, user_id: str, run_id: str, result: DataResult
        ) -> DataResult:
            if getattr(result, "kind", None) == "analysis_report":
                raise RunStateConflict(
                    "results can only be saved for an active run"
                )
            return await super().save_result(
                user_id=user_id, run_id=run_id, result=result
            )

    store = _ReportRejectingStore()
    executor, port, catalog = _executor(result_store=store)
    plan = _overview_plan(catalog)
    auth = _full_auth_context()
    execution = await _run_analysis(executor, port, plan, store, auth_context=auth)

    with pytest.raises(AnalysisReportAssemblyError) as exc_info:
        await _assembler(port, catalog, store).assemble_and_save(
            plan_id=plan.plan_id,
            request_id=plan.request_id,
            auth_context=auth,
            execution=execution,
        )

    assert exc_info.value.code == "REPORT_NOT_PERSISTED"


@pytest.mark.asyncio
async def test_report_reread_mismatch_fails_closed() -> None:
    class _CorruptingReportStore(InMemoryAgentStore):
        async def save_result(
            self, *, user_id: str, run_id: str, result: DataResult
        ) -> DataResult:
            if getattr(result, "kind", None) == "analysis_report":
                result = result.model_copy(update={"inline": False})
            return await super().save_result(
                user_id=user_id, run_id=run_id, result=result
            )

    store = _CorruptingReportStore()
    executor, port, catalog = _executor(result_store=store)
    plan = _overview_plan(catalog)
    auth = _full_auth_context()
    execution = await _run_analysis(executor, port, plan, store, auth_context=auth)

    with pytest.raises(AnalysisReportAssemblyError) as exc_info:
        await _assembler(port, catalog, store).assemble_and_save(
            plan_id=plan.plan_id,
            request_id=plan.request_id,
            auth_context=auth,
            execution=execution,
        )

    assert exc_info.value.code == "REPORT_STORE_MISMATCH"
