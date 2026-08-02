from full_view_agent.application.answer_claims import FINISH_TOOL_ID, StructuredFinish
from full_view_agent.application.errors import (
    ModelContractError,
    ModelProviderTimeout,
    ModelProviderUnavailable,
)
from full_view_agent.application.model_provider import (
    ModelProvider,
    ModelRequest,
    ModelResponse,
)
from full_view_agent.evaluation.contracts import (
    EvalErrorStep,
    EvalFinishStep,
    EvalModelStep,
    EvalToolCallStep,
)


class RecordingModelProvider:
    """Records replayable model actions without storing credentials."""

    def __init__(self, delegate: ModelProvider) -> None:
        self._delegate = delegate
        self.requests: list[ModelRequest] = []
        self.consumed_steps: list[EvalModelStep] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        try:
            response = await self._delegate.complete(request)
        except ModelProviderTimeout as exc:
            self._record_error("model_timeout", str(exc))
            raise
        except ModelProviderUnavailable as exc:
            self._record_error("model_provider_unavailable", str(exc))
            raise
        except ModelContractError as exc:
            self._record_error("model_contract_error", str(exc))
            raise

        if len(response.tool_calls) == 1:
            call = response.tool_calls[0]
            if call.tool_id == FINISH_TOOL_ID:
                try:
                    finish = StructuredFinish.model_validate(call.arguments)
                except ValueError:
                    self._record_error(
                        "model_contract_error",
                        "model returned invalid structured finish",
                    )
                else:
                    self.consumed_steps.append(
                        EvalFinishStep(
                            type="finish",
                            content=finish.summary,
                            structured_finish=finish,
                            total_tokens=response.usage.total_tokens,
                        )
                    )
            else:
                self.consumed_steps.append(
                    EvalToolCallStep(
                        type="tool_call",
                        tool_id=call.tool_id,
                        arguments=call.arguments,
                        total_tokens=response.usage.total_tokens,
                    )
                )
        elif len(response.tool_calls) > 1:
            self._record_error(
                "model_contract_error",
                "model returned more than one tool call",
            )
        elif response.content and response.content.strip():
            self.consumed_steps.append(
                EvalFinishStep(
                    type="finish",
                    content=response.content,
                    total_tokens=response.usage.total_tokens,
                )
            )
        else:
            self._record_error(
                "model_contract_error",
                "model returned no actionable content",
            )
        return response

    def _record_error(self, error_code: str, message: str) -> None:
        self.consumed_steps.append(
            EvalErrorStep.model_validate(
                {
                    "type": "error",
                    "error_code": error_code,
                    "message": message,
                }
            )
        )
