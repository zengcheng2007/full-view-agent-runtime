from pathlib import Path

import pytest

from full_view_agent.evaluation.differential import run_differential_suite
from full_view_agent.evaluation.loader import load_eval_trace
from full_view_agent.evaluation.suite import load_eval_suite_report, run_eval_suite


@pytest.mark.asyncio
async def test_versioned_baseline_suite_passes_and_writes_replayable_artifacts(
    tmp_path: Path,
) -> None:
    cases_dir = Path(__file__).parents[1] / "evals" / "cases"

    report = await run_eval_suite(cases_dir=cases_dir, output_dir=tmp_path)

    assert report.total_cases >= 12
    assert report.passed_cases == report.total_cases
    assert report.failed_cases == 0
    assert report.pass_at_1 == 1.0
    case_ids = [r.case_id for r in report.case_results]
    assert "result-traceability" in case_ids
    assert "s2-housing-metrics-success" in case_ids
    assert "s3-event-metrics-success" in case_ids
    assert "s5-comprehensive-overview" in case_ids
    assert load_eval_suite_report(tmp_path / "report.json") == report
    for result in report.case_results:
        assert (tmp_path / result.trace_path).is_file()
    traceability_result = next(
        result
        for result in report.case_results
        if result.case_id == "result-traceability"
    )
    traceability = load_eval_trace(tmp_path / traceability_result.trace_path)
    assert traceability.event_types.count("tool.started") == 2
    assert traceability.event_types.count("tool.completed") == 2
    lifecycle_grade = next(
        grade
        for grade in traceability.grades
        if grade.name == "tool_lifecycle_terminal_count"
    )
    assert lifecycle_grade.passed is True


@pytest.mark.asyncio
async def test_versioned_baseline_suite_runs_through_langgraph(
    tmp_path: Path,
) -> None:
    cases_dir = Path(__file__).parents[1] / "evals" / "cases"

    report = await run_eval_suite(
        cases_dir=cases_dir,
        output_dir=tmp_path,
        orchestrator="langgraph",
    )

    assert report.failed_cases == 0
    assert report.pass_at_1 == 1.0


@pytest.mark.asyncio
async def test_differential_suite_reports_no_native_langgraph_drift(
    tmp_path: Path,
) -> None:
    cases_dir = Path(__file__).parents[1] / "evals" / "cases"

    report = await run_differential_suite(
        cases_dir=cases_dir,
        output_dir=tmp_path,
    )

    assert report["gate_passed"] is True
    assert report["different_cases"] == 0
    assert (tmp_path / "differential-report.json").is_file()
    # S1-A Native freeze：语义入口脚本用例必须进入双编排器差分且零差异。
    comparisons = {
        comparison["case_id"]: comparison
        for comparison in report["comparisons"]
    }
    semantic_comparison = comparisons["planning-population-semantic-success"]
    assert semantic_comparison["matched"] is True
    assert semantic_comparison["differences"] == []
