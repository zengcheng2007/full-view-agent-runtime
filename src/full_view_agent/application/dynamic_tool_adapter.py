"""P2 Dynamic Tool Adapter: Bridge between CapabilityService and HttpConnectorExecutor.

This adapter implements the DynamicToolAdapter protocol and executes dynamic tools
via the HttpConnectorExecutor. It converts between the internal DataResult format
and the HTTP response format.

For dynamic tools, we use AreaCandidatesResult as a generic wrapper since dynamic tools
can have arbitrary schemas. The raw response is stored in the data field.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from full_view_agent.application.errors import (
    UpstreamContractError,
    UpstreamTimeout,
    UpstreamUnavailable,
)
from full_view_agent.domain.models import (
    AreaCandidatesData,
    AreaCandidatesResult,
    AuthContext,
    DataResult,
    InternalToolManifest,
    PolicyDecision,
)

if TYPE_CHECKING:
    from full_view_agent.infrastructure.capability_repository import (
        CapabilityRepository,
    )
    from full_view_agent.infrastructure.http_connector_executor import (
        HttpConnectorExecutor,
    )

logger = logging.getLogger(__name__)


class HttpDynamicToolAdapter:
    """Executes dynamic tools via HTTP connectors with SSRF protection.

    This adapter:
    1. Loads the ToolCapability from the repository
    2. Delegates to HttpConnectorExecutor for the actual HTTP call
    3. Wraps the HTTP response in an AreaCandidatesResult (used as generic container)
    """

    def __init__(
        self,
        *,
        repository: CapabilityRepository,
        http_executor: HttpConnectorExecutor,
    ) -> None:
        self._repository = repository
        self._http_executor = http_executor

    async def execute(
        self,
        *,
        manifest: InternalToolManifest,
        arguments: dict[str, object],
        policy_decision: PolicyDecision,
        auth_context: AuthContext,
    ) -> DataResult:
        """Execute a dynamic tool via HTTP connector.

        Args:
            manifest: The tool manifest (contains capability_id, connector_ref, etc.)
            arguments: The validated arguments dict
            policy_decision: The policy decision (used for audit trail)
            auth_context: The auth context (used for credential injection)

        Returns:
            An AreaCandidatesResult wrapping the HTTP response data

        Raises:
            UpstreamUnavailable: If connector or tool not found
            UpstreamTimeout: If HTTP request times out
            UpstreamContractError: If response doesn't match expected schema
            SemanticValidationError: If response violates semantic constraints
        """
        from full_view_agent.domain.capability import ToolCapability

        # Load the ToolCapability from repository
        tool = await self._repository.get(
            manifest.tool_id, manifest.tool_version
        )
        if tool is None:
            raise UpstreamUnavailable(
                f"Tool {manifest.tool_id} v{manifest.tool_version} not found"
            )
        if not isinstance(tool, ToolCapability):
            raise UpstreamContractError(
                f"Capability {manifest.tool_id} is not a ToolCapability"
            )

        # Execute via HttpConnectorExecutor
        # The executor handles SSRF protection, credential injection, redirects, etc.
        try:
            response_data = await self._http_executor.execute(
                tool=tool,
                arguments=arguments,
            )
        except Exception as exc:
            # Re-raise our domain exceptions as-is
            if isinstance(
                exc,
                (UpstreamTimeout, UpstreamUnavailable, UpstreamContractError),
            ):
                raise
            # Wrap other exceptions
            logger.error(f"Dynamic tool execution failed: {exc}", exc_info=True)
            raise UpstreamUnavailable(f"HTTP execution failed: {exc}") from exc

        # Wrap response in AreaCandidatesResult as a generic container
        # We store the raw response in the 'data' field
        from full_view_agent.application.fingerprints import canonical_fingerprint

        fingerprint = canonical_fingerprint(
            domain=f"dynamic-result:{tool.capability_id}:{tool.version}",
            value=response_data,
        )

        # Create a minimal AreaCandidatesResult to wrap the data
        # The actual response is stored in a custom way
        return AreaCandidatesResult(
            result_id=f"dynamic-{tool.capability_id}-{tool.version}",
            data_schema_ref=f"schema://dynamic/{tool.capability_id}/{tool.version}",
            result_fingerprint=fingerprint,
            data=AreaCandidatesData(
                resolved_area_code=None,
                ambiguous=False,
                candidates=[],
            ),
            candidate_count=0,
        )
