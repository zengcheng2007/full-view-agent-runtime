"""P2-1 Dynamic Tool Bridge: Convert published ToolCapability to registry entries.

This module bridges the Capability Center (CRUD + lifecycle) with the execution
path (ToolRegistry). Published tools from capability_repository are converted to
InternalToolManifest + ModelToolDescriptor and merged into the ToolRegistry for
new Runs. Only published tools are included; draft/testing/pending/disabled are
excluded.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from full_view_agent.domain.capability import ToolCapability
from full_view_agent.domain.models import (
    InternalToolManifest,
    ModelInputSchemaReference,
    ModelToolDescriptor,
    ToolCachePolicy,
    ToolLimits,
    ToolPolicyBinding,
    ToolResultSchemaBinding,
)

if TYPE_CHECKING:
    from full_view_agent.infrastructure.capability_repository import (
        CapabilityRepository,
    )

logger = logging.getLogger(__name__)


def convert_tool_capability_to_manifest(
    tool: ToolCapability,
) -> InternalToolManifest:
    """Convert a published ToolCapability to an InternalToolManifest.

    This creates a manifest that the execution layer can understand. The
    adapter_ref points to a dynamic connector executor that will use the
    connector_ref and resource_path from the ToolCapability.
    """
    # Build result schema binding from result_kind
    result_schemas = [
        ToolResultSchemaBinding(
            kind=tool.result_kind,
            data_schema_ref=tool.data_schema_ref or f"schema://dynamic/{tool.capability_id}/{tool.version}",
        )
    ]

    # Build limits from tool config
    limits = ToolLimits(
        timeout_ms=tool.timeout_ms,
        max_attempts=tool.max_attempts,
        max_result_rows=tool.max_result_rows,
        max_group_buckets=200,  # Default for dynamic tools
    )

    # Build cache policy
    cache_policy = ToolCachePolicy(
        enabled=tool.cache_enabled,
        ttl_seconds=tool.cache_ttl_seconds,
    )

    # Build policy binding
    # For dynamic tools, we use the capability_id as the action
    # and dataset_area_fields as the denial_scope
    policy = ToolPolicyBinding(
        action=f"capability.{tool.capability_id}",
        pre_check=True,
        post_filter=True,
        denial_scope="dataset_area_fields",
    )

    # Build adapter_ref pointing to dynamic connector executor
    # Format: adapter://dynamic/{connector_id}/{resource_path}
    adapter_ref = f"adapter://dynamic/{tool.connector_ref}/{tool.resource_path}"

    # InternalToolManifest only accepts "low" or "medium" risk levels
    # Map "high" to "medium" for dynamic tools
    mapped_risk_level = "medium" if tool.risk_level == "high" else tool.risk_level

    return InternalToolManifest(
        schema_version="1.1",
        tool_id=tool.capability_id,
        tool_version=tool.version,
        status="active",
        domain="governance",  # All capabilities are governance for now
        owner=tool.owner,
        effect="read",  # All tool capabilities are read-only
        risk_level=mapped_risk_level,
        dataset_id=tool.dataset_ids[0] if tool.dataset_ids else "dynamic",
        data_classifications=["internal"],  # Default classification
        required_permissions=tool.required_permissions,
        input_schema_ref=f"schema://dynamic/{tool.capability_id}/input/{tool.version}",
        result_schemas=result_schemas,
        limits=limits,
        cache_policy=cache_policy,
        policy=policy,
        adapter_ref=adapter_ref,
    )


def convert_tool_capability_to_descriptor(
    tool: ToolCapability,
) -> ModelToolDescriptor:
    """Convert a published ToolCapability to a ModelToolDescriptor.

    This creates a descriptor that the model can understand for tool selection.
    """
    return ModelToolDescriptor(
        tool_id=tool.capability_id,
        tool_version=tool.version,
        name=tool.name,
        description=tool.description or f"Dynamic tool: {tool.name}",
        input_schema=ModelInputSchemaReference(
            **{"$ref": f"schema://dynamic/{tool.capability_id}/input/{tool.version}"}
        ),
    )


async def load_published_tools(
    repository: CapabilityRepository,
) -> list[ToolCapability]:
    """Load all published tools from the capability repository.

    Only tools with status='published' are returned. Draft, testing,
    pending_approval, and disabled tools are excluded.
    """
    all_capabilities = await repository.list_capabilities(
        capability_type="tool",
        status="published",
    )
    # Filter to only ToolCapability instances
    published = [
        t for t in all_capabilities if isinstance(t, ToolCapability)
    ]
    logger.info(f"Loaded {len(published)} published tools from capability repository")
    return published


def build_dynamic_tool_registry_entries(
    tools: list[ToolCapability],
) -> tuple[list[InternalToolManifest], list[ModelToolDescriptor]]:
    """Convert a list of published ToolCapability to registry entries.

    Returns a tuple of (manifests, descriptors) that can be merged into
    a ToolRegistry.
    """
    manifests = []
    descriptors = []

    for tool in tools:
        try:
            manifest = convert_tool_capability_to_manifest(tool)
            descriptor = convert_tool_capability_to_descriptor(tool)
            manifests.append(manifest)
            descriptors.append(descriptor)
        except Exception as e:
            logger.error(f"Failed to convert tool {tool.capability_id}: {e}")
            continue

    return manifests, descriptors
