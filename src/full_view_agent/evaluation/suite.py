from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from full_view_agent.application.session_run_service import new_id
from full_view_agent.evaluation.contracts import (
    EvalCaseReport,
    EvalSuiteReport,
)
from full_view_agent.evaluation.loader import load_eval_case, save_eval_trace
from full_view_agent.evaluation.runner import EvalRunner


async def run_eval_suite(
    *,
    cases_dir: Path,
    output_dir: Path,
    orchestrator: Literal["native", "langgraph"] = "native",
) -> EvalSuiteReport:
    started_at = datetime.now(UTC)
    case_paths = sorted(cases_dir.glob("*.yaml"))
    if not case_paths:
        raise ValueError("eval suite contains no YAML cases")
    cases = [load_eval_case(path) for path in case_paths]
    case_ids = [case.case_id for case in cases]
    if len(case_ids) != len(set(case_ids)):
        raise ValueError("eval suite contains duplicate case_id values")

    runner = EvalRunner(orchestrator=orchestrator)
    results: list[EvalCaseReport] = []
    for case in cases:
        trace = await runner.run(case)
        relative_trace_path = Path("traces") / f"{case.case_id}.json"
        save_eval_trace(output_dir / relative_trace_path, trace)
        results.append(
            EvalCaseReport(
                case_id=case.case_id,
                passed=trace.passed,
                trace_path=relative_trace_path.as_posix(),
            )
        )

    passed_cases = sum(result.passed for result in results)
    report = EvalSuiteReport(
        suite_run_id=new_id("evs"),
        started_at=started_at,
        completed_at=datetime.now(UTC),
        total_cases=len(results),
        passed_cases=passed_cases,
        failed_cases=len(results) - passed_cases,
        pass_at_1=passed_cases / len(results),
        case_results=results,
    )
    _save_report(output_dir / "report.json", report)
    return report


def load_eval_suite_report(path: Path) -> EvalSuiteReport:
    return EvalSuiteReport.model_validate_json(path.read_text(encoding="utf-8"))


def _save_report(path: Path, report: EvalSuiteReport) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(f"{path.suffix}.tmp")
    temporary_path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    temporary_path.replace(path)
