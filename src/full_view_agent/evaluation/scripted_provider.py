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
            return ModelResponse(
                content=step.content,
                tool_calls=(),
                finish_reason="stop",
                usage=ModelUsage(total_tokens=step.total_tokens),
            )
        raise ModelContractError("unsupported scripted model step")
