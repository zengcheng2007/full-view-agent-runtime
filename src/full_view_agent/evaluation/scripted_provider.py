import json

from full_view_agent.application.answer_claims import FINISH_TOOL_ID
from full_view_agent.application.errors import (
    ModelContractError,
    ModelProviderTimeout,
    ModelProviderUnavailable,
)
from full_view_agent.application.model_provider import (
    ModelProvider,
    ModelRequest,
    ModelResponse,
    ModelToolCall,
    ModelUsage,
)
from full_view_agent.evaluation.contracts import (
    EvalErrorStep,
    EvalFinishStep,
    EvalModelStep,
    EvalToolCallStep,
)


class ScriptedModelProvider(ModelProvider):
    """Deterministic provider used by evals and offline replay."""

    def __init__(self, steps: list[EvalModelStep]) -> None:
        self._remaining = list(steps)
        self.requests: list[ModelRequest] = []
        self.consumed_steps: list[EvalModelStep] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        if not self._remaining:
            raise ModelContractError("scripted model steps exhausted")
        step = self._remaining.pop(0)
        self.consumed_steps.append(step)
        if isinstance(step, EvalErrorStep):
            error_types = {
                "model_timeout": ModelProviderTimeout,
                "model_provider_unavailable": ModelProviderUnavailable,
                "model_contract_error": ModelContractError,
            }
            raise error_types[step.error_code](step.message)
        if isinstance(step, EvalToolCallStep):
            return ModelResponse(
                content=None,
                tool_calls=(
                    ModelToolCall(
                        tool_id=step.tool_id,
                        arguments=step.arguments,
                    ),
                ),
                finish_reason="tool_calls",
                usage=ModelUsage(total_tokens=step.total_tokens),
            )
        if isinstance(step, EvalFinishStep):
            if step.structured_finish is not None:
                rebound = _rebind_finish_result_ids(step.structured_finish, request)
                step = step.model_copy(update={"structured_finish": rebound})
                self.consumed_steps[-1] = step
                return ModelResponse(
                    content=None,
                    tool_calls=(
                        ModelToolCall(
                            tool_id=FINISH_TOOL_ID,
                            arguments=rebound.model_dump(mode="json"),
                        ),
                    ),
                    finish_reason="tool_calls",
                    usage=ModelUsage(total_tokens=step.total_tokens),
                )
            return ModelResponse(
                content=step.content,
                tool_calls=(),
                finish_reason="stop",
                usage=ModelUsage(total_tokens=step.total_tokens),
            )
        raise ModelContractError("unsupported scripted model step")


def _rebind_finish_result_ids(finish, request: ModelRequest):
    by_fingerprint: dict[str, set[str]] = {}
    for message in request.messages:
        if not message.content:
            continue
        try:
            payload = json.loads(message.content)
        except (TypeError, json.JSONDecodeError):
            continue
        for item in _walk_dicts(payload):
            result_id = item.get("result_id")
            fingerprint = item.get("result_fingerprint")
            if isinstance(result_id, str) and isinstance(fingerprint, str):
                by_fingerprint.setdefault(fingerprint, set()).add(result_id)
    rebound_claims = []
    for claim in finish.claims:
        matches = by_fingerprint.get(claim.result_fingerprint, set())
        rebound_claims.append(
            claim.model_copy(update={"result_id": next(iter(matches))})
            if len(matches) == 1
            else claim
        )
    return finish.model_copy(update={"claims": rebound_claims})


def _walk_dicts(value):
    if isinstance(value, dict):
        yield value
        for nested in value.values():
            yield from _walk_dicts(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _walk_dicts(nested)
