"""Trusted production bridge for resolving analysis area names.

The bridge deliberately reuses the ordinary CapabilityService boundary so
Registry admission, Policy, denial ledger, credentials, and upstream error
normalization remain the single source of truth.
"""

from pydantic import ValidationError

from full_view_agent.application.analysis_intent_service import (
    AnalysisIntentRejected,
    ResolvedArea,
)
from full_view_agent.application.errors import (
    ReauthenticationRequired,
    UpstreamContractError,
    UpstreamTimeout,
    UpstreamUnavailable,
)
from full_view_agent.application.fingerprints import canonical_fingerprint
from full_view_agent.application.harness import HarnessToolExecutor
from full_view_agent.application.session_run_service import new_id
from full_view_agent.domain.models import AreaCandidatesResult, AuthContext

_RESOLVE_AREA_TOOL_ID = "governance.resolve_area"
_RESOLVE_AREA_TOOL_VERSION = "1.0.0"
_AREA_SCHEMA_REF = "schema://data/area-candidates/1.0.0"
_AREA_FINGERPRINT_DOMAIN = "data-result:area-candidates:1.0.0"
_MAX_CANDIDATES = 20


class CapabilityAnalysisAreaResolver:
    """Resolve names through the authorized production Capability boundary."""

    def __init__(self, *, capability: HarnessToolExecutor) -> None:
        self._capability = capability

    async def resolve_area_query(
        self,
        *,
        query: str,
        auth_context: AuthContext,
    ) -> tuple[ResolvedArea, ...]:
        tool_call_id = new_id("tcl")
        result = await self._capability.execute(
            tool_call_id=tool_call_id,
            tool_id=_RESOLVE_AREA_TOOL_ID,
            raw_arguments={"query": query, "max_candidates": _MAX_CANDIDATES},
            auth_context=auth_context,
        )
        if (
            result.tool_call_id != tool_call_id
            or result.tool_id != _RESOLVE_AREA_TOOL_ID
            or result.tool_version != _RESOLVE_AREA_TOOL_VERSION
        ):
            raise UpstreamContractError("area resolver returned the wrong tool result")
        if result.status == "denied":
            if "AUTH_CONTEXT_EXPIRED" in result.warnings:
                raise ReauthenticationRequired(
                    "area resolution requires refreshed authentication"
                )
            raise AnalysisIntentRejected(
                "AREA_RESOLUTION_NOT_AUTHORIZED",
                "area resolution is not authorized for the current run",
            )
        if result.status == "failed":
            self._raise_failed_result(result.warnings)
        if result.status != "success":
            raise UpstreamContractError(
                "area resolution did not return a complete successful result"
            )
        data_result = result.data_result
        if not isinstance(data_result, AreaCandidatesResult):
            raise UpstreamContractError(
                "area resolver returned an unexpected result kind"
            )
        try:
            data_result = AreaCandidatesResult.model_validate(
                data_result.model_dump(mode="json")
            )
        except ValidationError as exc:
            raise UpstreamContractError(
                "area resolver returned an invalid result contract"
            ) from exc
        self._validate_result(data_result)
        return tuple(
            ResolvedArea(
                area_code=candidate.area_code,
                area_name=candidate.area_name,
            )
            for candidate in data_result.data.candidates
        )

    @staticmethod
    def _raise_failed_result(warnings: list[str]) -> None:
        warning_set = set(warnings)
        if "upstream_timeout" in warning_set:
            raise UpstreamTimeout("area resolution timed out")
        if "upstream_unavailable" in warning_set:
            raise UpstreamUnavailable("area resolution service is unavailable")
        if "AUTH_CONTEXT_EXPIRED" in warning_set:
            raise ReauthenticationRequired(
                "area resolution requires refreshed authentication"
            )
        raise UpstreamContractError("area resolution failed its capability contract")

    @staticmethod
    def _validate_result(result: AreaCandidatesResult) -> None:
        if result.payload_status != "available":
            raise UpstreamContractError("area resolution payload is unavailable")
        if result.data_schema_ref != _AREA_SCHEMA_REF:
            raise UpstreamContractError("area resolution schema is unsupported")
        if result.candidate_count != len(result.data.candidates):
            raise UpstreamContractError("area candidate count is inconsistent")
        expected_fingerprint = canonical_fingerprint(
            domain=_AREA_FINGERPRINT_DOMAIN,
            value=result.data,
        )
        if result.result_fingerprint != expected_fingerprint:
            raise UpstreamContractError("area resolution fingerprint is inconsistent")

        count = result.candidate_count
        resolved = result.data.resolved_area_code
        ambiguous = result.data.ambiguous
        if count == 0 and (resolved is not None or ambiguous):
            raise UpstreamContractError("empty area result has invalid resolution state")
        if count == 1 and (
            resolved != result.data.candidates[0].area_code or ambiguous
        ):
            raise UpstreamContractError("single area result has invalid resolution state")
        if count > 1 and (resolved is not None or not ambiguous):
            raise UpstreamContractError("ambiguous area result has invalid resolution state")
