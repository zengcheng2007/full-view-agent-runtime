from pathlib import Path

import pytest

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
