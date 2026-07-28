import json
from pathlib import Path

import full_view_agent.evaluation.cli as eval_cli
from full_view_agent.evaluation.cli import main
from full_view_agent.evaluation.loader import load_eval_trace
from full_view_agent.evaluation.runner import EvalRunner

from .test_eval_runner import TwoStepLiveModelProvider


def test_eval_cli_runs_suite_and_replays_saved_trace(
    tmp_path: Path,
    capsys,
) -> None:
    project_root = Path(__file__).parents[1]
    cases_dir = project_root / "evals" / "cases"
    suite_dir = tmp_path / "suite"

    run_exit_code = main(
        [
            "run",
            "--cases",
            str(cases_dir),
            "--output",
            str(suite_dir),
        ]
    )
    replay_output = tmp_path / "replayed.json"
    replay_exit_code = main(
        [
            "replay",
            "--case",
            str(cases_dir / "planning-population-success.yaml"),
            "--trace",
            str(suite_dir / "traces" / "planning-population-success.json"),
            "--output",
            str(replay_output),
        ]
    )

    assert run_exit_code == 0
    assert replay_exit_code == 0
    assert load_eval_trace(replay_output).passed is True
    output = capsys.readouterr().out
    assert "pass@1=100" in output
    assert "REPLAY PASS" in output


def test_eval_cli_runs_native_langgraph_differential_gate(
    tmp_path: Path,
    capsys,
) -> None:
    project_root = Path(__file__).parents[1]
    cases_dir = project_root / "evals" / "cases"
    output_dir = tmp_path / "differential"

    exit_code = main(
        [
            "run-differential",
            "--cases",
            str(cases_dir),
            "--output",
            str(output_dir),
        ]
    )

    assert exit_code == 0
    report_path = output_dir / "differential-report.json"
    assert report_path.is_file()
    assert "DIFFERENTIAL PASS" in capsys.readouterr().out
    # S1-A Native freeze：CLI 差分报告覆盖语义入口用例且零差异。
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["gate_passed"] is True
    matched = {
        comparison["case_id"]: comparison["matched"]
        for comparison in report["comparisons"]
    }
    assert matched["planning-population-semantic-success"] is True


def test_eval_cli_runs_one_live_case_and_records_model_metadata(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    project_root = Path(__file__).parents[1]
    case_path = project_root / "evals" / "cases" / "planning-population-success.yaml"
    output_path = tmp_path / "live-trace.json"
    runner = EvalRunner(
        provider=TwoStepLiveModelProvider(),
        model_provider="openai_compatible",
        model_name="qwen-live-test",
    )
    monkeypatch.setattr(
        eval_cli,
        "build_live_eval_runner",
        lambda env_file, **kwargs: runner,
        raising=False,
    )

    exit_code = main(
        [
            "run-live",
            "--case",
            str(case_path),
            "--output",
            str(output_path),
            "--env-file",
            str(tmp_path / ".env"),
        ]
    )

    trace = load_eval_trace(output_path)
    assert exit_code == 0
    assert trace.passed is True
    assert trace.model_name == "qwen-live-test"
    assert "LIVE EVAL PASS" in capsys.readouterr().out


def test_eval_cli_runs_one_live_http_case(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    project_root = Path(__file__).parents[1]
    case_path = project_root / "evals" / "cases" / "planning-population-success.yaml"
    output_path = tmp_path / "live-http-trace.json"
    runner = EvalRunner(
        provider=TwoStepLiveModelProvider(),
        model_provider="openai_compatible",
        model_name="qwen-live-http-test",
    )
    monkeypatch.setattr(
        eval_cli,
        "build_live_http_eval_runner",
        lambda env_file, **kwargs: runner,
        raising=False,
    )

    exit_code = main(
        [
            "run-live-http",
            "--case",
            str(case_path),
            "--output",
            str(output_path),
            "--env-file",
            str(tmp_path / ".env"),
        ]
    )

    trace = load_eval_trace(output_path)
    assert exit_code == 0
    assert trace.passed is True
    assert trace.model_name == "qwen-live-http-test"
    assert "LIVE HTTP EVAL PASS" in capsys.readouterr().out
