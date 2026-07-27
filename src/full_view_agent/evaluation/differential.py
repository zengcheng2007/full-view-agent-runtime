import json
from pathlib import Path

from full_view_agent.evaluation.loader import load_eval_trace
from full_view_agent.evaluation.suite import run_eval_suite


async def run_differential_suite(
    *,
    cases_dir: Path,
    output_dir: Path,
) -> dict[str, object]:
    """Run the same baseline through both orchestrators and report drift."""
    native_dir = output_dir / "native"
    langgraph_dir = output_dir / "langgraph"
    native_report = await run_eval_suite(
        cases_dir=cases_dir,
        output_dir=native_dir,
        orchestrator="native",
    )
    langgraph_report = await run_eval_suite(
        cases_dir=cases_dir,
        output_dir=langgraph_dir,
        orchestrator="langgraph",
    )

    native_cases = {
        result.case_id: load_eval_trace(native_dir / result.trace_path)
        for result in native_report.case_results
    }
    langgraph_cases = {
        result.case_id: load_eval_trace(langgraph_dir / result.trace_path)
        for result in langgraph_report.case_results
    }
    all_case_ids = sorted(native_cases.keys() | langgraph_cases.keys())
    comparisons: list[dict[str, object]] = []
    for case_id in all_case_ids:
        native = native_cases.get(case_id)
        langgraph = langgraph_cases.get(case_id)
        if native is None or langgraph is None:
            differences = ["case_missing"]
        else:
            differences = _semantic_differences(native, langgraph)
        comparisons.append(
            {
                "case_id": case_id,
                "matched": not differences,
                "differences": differences,
            }
        )

    different_cases = sum(not bool(item["matched"]) for item in comparisons)
    gate_passed = (
        native_report.failed_cases == 0
        and langgraph_report.failed_cases == 0
        and different_cases == 0
    )
    report: dict[str, object] = {
        "report_version": "1.0",
        "gate_passed": gate_passed,
        "total_cases": len(all_case_ids),
        "different_cases": different_cases,
        "native_passed": native_report.passed_cases,
        "langgraph_passed": langgraph_report.passed_cases,
        "comparisons": comparisons,
    }
    output_path = output_dir / "differential-report.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(output_path)
    return report


def _semantic_differences(native, langgraph) -> list[str]:
    comparisons = {
        "passed": (native.passed, langgraph.passed),
        "terminal_status": (
            native.terminal_status,
            langgraph.terminal_status,
        ),
        "outcome": (native.outcome, langgraph.outcome),
        "completion_reason_code": (
            native.completion_reason_code,
            langgraph.completion_reason_code,
        ),
        "tool_ids": (native.tool_ids, langgraph.tool_ids),
        "event_types": (native.event_types, langgraph.event_types),
        "evidence_count": (
            len(native.evidence_ids),
            len(langgraph.evidence_ids),
        ),
        "grades": (
            [grade.model_dump() for grade in native.grades],
            [grade.model_dump() for grade in langgraph.grades],
        ),
        "total_tokens": (native.total_tokens, langgraph.total_tokens),
    }
    return [
        name
        for name, (native_value, langgraph_value) in comparisons.items()
        if native_value != langgraph_value
    ]
