from pydantic import BaseModel

from full_view_agent.application.capability_service import ToolAdapter
from full_view_agent.application.errors import (
    UpstreamContractError,
    UpstreamTimeout,
    UpstreamUnavailable,
)
from full_view_agent.domain.models import (
    AuthContext,
    DataResult,
    InternalToolManifest,
    PolicyDecision,
)
from full_view_agent.evaluation.contracts import EvalFaultSpec


class FaultInjectingEvalAdapter:
    """Evaluation-only decorator for deterministic downstream failures."""

    def __init__(
        self,
        *,
        delegate: ToolAdapter,
        fault: EvalFaultSpec,
    ) -> None:
        self._delegate = delegate
        self._fault = fault

    async def execute(
        self,
        *,
        manifest: InternalToolManifest,
        arguments: BaseModel,
        policy_decision: PolicyDecision,
        auth_context: AuthContext,
    ) -> DataResult:
        if manifest.tool_id == self._fault.tool_id:
            if self._fault.type == "upstream_timeout":
                raise UpstreamTimeout("evaluation injected upstream timeout")
            if self._fault.type == "upstream_unavailable":
                raise UpstreamUnavailable(
                    "evaluation injected upstream unavailable"
                )
            raise UpstreamContractError(
                "evaluation injected upstream contract error"
            )
        return await self._delegate.execute(
            manifest=manifest,
            arguments=arguments,
            policy_decision=policy_decision,
            auth_context=auth_context,
        )
