from pathlib import Path

import yaml

from full_view_agent.evaluation.contracts import EvalCase, EvalTrace


def load_eval_case(path: Path) -> EvalCase:
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    return EvalCase.model_validate(payload)


def load_eval_trace(path: Path) -> EvalTrace:
    return EvalTrace.model_validate_json(path.read_text(encoding="utf-8"))


def save_eval_trace(path: Path, trace: EvalTrace) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(f"{path.suffix}.tmp")
    temporary_path.write_text(
        trace.model_dump_json(indent=2),
        encoding="utf-8",
    )
    temporary_path.replace(path)
