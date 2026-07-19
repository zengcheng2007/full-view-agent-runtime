import argparse
import asyncio
from pathlib import Path

from full_view_agent.evaluation.live import build_live_eval_runner
from full_view_agent.evaluation.live_http import build_live_http_eval_runner
from full_view_agent.evaluation.loader import (
    load_eval_case,
    load_eval_trace,
    save_eval_trace,
)
from full_view_agent.evaluation.runner import EvalRunner
from full_view_agent.evaluation.suite import run_eval_suite


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    arguments = parser.parse_args(argv)
    if arguments.command == "run":
        return asyncio.run(
            _run_suite(
                cases_dir=arguments.cases,
                output_dir=arguments.output,
            )
        )
    if arguments.command == "run-live":
        return asyncio.run(
            _run_live(
                case_path=arguments.case,
                output_path=arguments.output,
                env_file=arguments.env_file,
            )
        )
    if arguments.command == "run-live-http":
        return asyncio.run(
            _run_live_http(
                case_path=arguments.case,
                output_path=arguments.output,
                env_file=arguments.env_file,
            )
        )
    return asyncio.run(
        _replay(
            case_path=arguments.case,
            trace_path=arguments.trace,
            output_path=arguments.output,
        )
    )


async def _run_suite(*, cases_dir: Path, output_dir: Path) -> int:
    report = await run_eval_suite(cases_dir=cases_dir, output_dir=output_dir)
    status = "PASS" if report.failed_cases == 0 else "FAIL"
    print(
        f"EVAL {status}: {report.passed_cases}/{report.total_cases} "
        f"pass@1={report.pass_at_1:.2%} report={output_dir / 'report.json'}"
    )
    return 0 if report.failed_cases == 0 else 1


async def _replay(*, case_path: Path, trace_path: Path, output_path: Path) -> int:
    case = load_eval_case(case_path)
    source_trace = load_eval_trace(trace_path)
    replayed = await EvalRunner().replay(case, source_trace)
    save_eval_trace(output_path, replayed)
    status = "PASS" if replayed.passed else "FAIL"
    print(f"REPLAY {status}: case={case.case_id} trace={output_path}")
    return 0 if replayed.passed else 1


async def _run_live(*, case_path: Path, output_path: Path, env_file: Path) -> int:
    case = load_eval_case(case_path)
    trace = await build_live_eval_runner(env_file).run(case)
    save_eval_trace(output_path, trace)
    status = "PASS" if trace.passed else "FAIL"
    print(
        f"LIVE EVAL {status}: case={case.case_id} model={trace.model_name} "
        f"tokens={trace.total_tokens} trace={output_path}"
    )
    return 0 if trace.passed else 1


async def _run_live_http(
    *,
    case_path: Path,
    output_path: Path,
    env_file: Path,
) -> int:
    case = load_eval_case(case_path)
    trace = await build_live_http_eval_runner(env_file).run(case)
    save_eval_trace(output_path, trace)
    status = "PASS" if trace.passed else "FAIL"
    print(
        f"LIVE HTTP EVAL {status}: case={case.case_id} model={trace.model_name} "
        f"tokens={trace.total_tokens} trace={output_path}"
    )
    return 0 if trace.passed else 1


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run or replay agent eval cases")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="run all YAML cases")
    run_parser.add_argument("--cases", type=Path, required=True)
    run_parser.add_argument("--output", type=Path, required=True)

    live_parser = subparsers.add_parser(
        "run-live",
        help="run one YAML case against the configured live model",
    )
    live_parser.add_argument("--case", type=Path, required=True)
    live_parser.add_argument("--output", type=Path, required=True)
    live_parser.add_argument("--env-file", type=Path, default=Path(".env"))

    live_http_parser = subparsers.add_parser(
        "run-live-http",
        help="run one YAML case against the live model and existing HTTP services",
    )
    live_http_parser.add_argument("--case", type=Path, required=True)
    live_http_parser.add_argument("--output", type=Path, required=True)
    live_http_parser.add_argument("--env-file", type=Path, default=Path(".env"))

    replay_parser = subparsers.add_parser("replay", help="replay one saved trace")
    replay_parser.add_argument("--case", type=Path, required=True)
    replay_parser.add_argument("--trace", type=Path, required=True)
    replay_parser.add_argument("--output", type=Path, required=True)
    return parser
