from pathlib import Path

import pytest

from full_view_agent.evaluation.contracts import EvalErrorStep
from full_view_agent.evaluation.loader import load_eval_trace, save_eval_trace
from full_view_agent.evaluation.runner import EvalRunner

from .test_eval_runner import population_case


@pytest.mark.asyncio
async def test_eval_trace_round_trip_and_offline_replay(tmp_path: Path) -> None:
    runner = EvalRunner()
    original = await runner.run(population_case())
    trace_path = tmp_path / "population-success.json"

    save_eval_trace(trace_path, original)
    loaded = load_eval_trace(trace_path)
    case_with_broken_live_script = population_case().model_copy(
        update={
            "model_steps": [
                EvalErrorStep(
                    type="error",
                    error_code="model_timeout",
                    message="this step must not run during replay",
                )
            ]
        }
    )
    replayed = await runner.replay(case_with_broken_live_script, loaded)

    assert loaded == original
    assert replayed.passed is True
    assert replayed.replayed_from_eval_run_id == original.eval_run_id
    assert replayed.eval_run_id != original.eval_run_id
    assert replayed.tool_ids == original.tool_ids
    assert replayed.completion_reason_code == original.completion_reason_code


@pytest.mark.asyncio
async def test_eval_replay_rejects_trace_from_another_case() -> None:
    runner = EvalRunner()
    trace = await runner.run(population_case())
    other_case = population_case().model_copy(update={"case_id": "other-case"})

    with pytest.raises(ValueError, match="case_id"):
        await runner.replay(other_case, trace)
