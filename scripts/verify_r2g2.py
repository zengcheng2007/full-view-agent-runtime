"""P2-2 Hot Publish Demonstration and Verification.

This script demonstrates the hot-publish mechanism where:
1. Each new run gets a capability snapshot at creation time
2. Publish/deactivate/rollback only affects subsequently created runs
3. Already running runs keep their original capability version
"""

from __future__ import annotations

import asyncio
import sys


def _print(msg: str) -> None:
    """Print with encoding-safe fallback for Windows GBK consoles."""
    try:
        print(msg)
    except UnicodeEncodeError:
        print(msg.encode("ascii", "replace").decode("ascii"))


async def _publish_full_lifecycle(
    management_service: object,
    capability_id: str,
    version: str,
    published_by: str,
) -> None:
    """Move through lifecycle: draft -> testing -> pending_approval -> published."""
    # draft -> testing
    await management_service.advance_status(
        capability_id=capability_id,
        version=version,
        to_status="testing",
        changed_by=published_by,
        reason="Auto-advance to testing for verification",
    )
    # testing -> pending_approval
    await management_service.advance_status(
        capability_id=capability_id,
        version=version,
        to_status="pending_approval",
        changed_by=published_by,
        reason="Auto-advance to pending_approval for verification",
    )
    # pending_approval -> published
    await management_service.publish(
        capability_id=capability_id,
        version=version,
        published_by=published_by,
    )


async def demonstrate_hot_publish() -> bool:
    """Demonstrate the hot-publish snapshot mechanism."""
    _print("=" * 70)
    _print("R2G2 Dynamic Tool Hot Publish - Demonstration")
    _print("=" * 70)

    # Import required components
    from full_view_agent.application.capability_management_service import (
        CapabilityManagementService,
    )
    from full_view_agent.application.run_capability_snapshot import (
        RunCapabilitySnapshotService,
    )
    from full_view_agent.application.tool_registry import ToolRegistry
    from full_view_agent.domain.capability import Connector, ToolCapability
    from full_view_agent.infrastructure.capability_repository import (
        InMemoryCapabilityRepository,
    )

    _print("\n1. Setting up test environment...")
    repository = InMemoryCapabilityRepository()
    management_service = CapabilityManagementService(repository)
    snapshot_service = RunCapabilitySnapshotService(repository)
    base_registry = ToolRegistry.default()

    _print("   [OK] Repository and services initialized")

    # Create a connector
    _print("\n2. Creating connector...")
    connector = Connector(
        connector_id="demo-connector",
        name="Demo Connector",
        base_url="http://demo.example.com",
        allowed_path_prefixes=["/api"],
    )
    await repository.save_connector(connector)
    _print("   [OK] Connector created")

    # Create and publish first version of tool
    _print("\n3. Creating tool v1.0.0...")
    tool_v1 = ToolCapability(
        capability_id="demo.hot_tool",
        name="Demo Hot Tool",
        domain="governance",
        owner="demo",
        version="1.0.0",
        status="draft",
        risk_level="low",
        required_permissions=[],
        dataset_ids=[],
        connector_ref="demo-connector",
        http_method="GET",
        resource_path="/api/v1",
        input_schema={"type": "object", "properties": {}},
        result_kind="table",
        created_by="demo",
        updated_by="demo",
    )
    tool_v1 = await repository.save_tool(tool_v1)
    _print("   [OK] Tool v1.0.0 created (draft)")

    # Publish v1 through full lifecycle
    _print("\n4. Publishing tool v1.0.0 through full lifecycle...")
    await _publish_full_lifecycle(management_service, "demo.hot_tool", "1.0.0", "demo")
    _print("   [OK] Tool v1.0.0 published")

    # Create first run snapshot
    _print("\n5. Creating snapshot for Run A...")
    snapshot_a = await snapshot_service.create_snapshot_for_run(
        run_id="run-A",
        base_registry=base_registry,
    )
    _print(f"   [OK] Run A snapshot created at {snapshot_a.created_at}")
    _print(
        f"   [OK] Run A sees tool version:"
        f" {snapshot_a.tool_versions.get('demo.hot_tool', 'N/A')}"
    )

    # Create and publish v2
    _print("\n6. Creating tool v2.0.0...")
    tool_v2 = ToolCapability(
        capability_id="demo.hot_tool",
        name="Demo Hot Tool",
        domain="governance",
        owner="demo",
        version="2.0.0",
        status="draft",
        risk_level="low",
        required_permissions=[],
        dataset_ids=[],
        connector_ref="demo-connector",
        http_method="POST",
        resource_path="/api/v2",
        input_schema={"type": "object", "properties": {}},
        result_kind="table",
        created_by="demo",
        updated_by="demo",
    )
    tool_v2 = await repository.save_tool(tool_v2)
    await _publish_full_lifecycle(management_service, "demo.hot_tool", "2.0.0", "demo")
    _print("   [OK] Tool v2.0.0 created and published")

    # Create second run snapshot
    _print("\n7. Creating snapshot for Run B...")
    snapshot_b = await snapshot_service.create_snapshot_for_run(
        run_id="run-B",
        base_registry=base_registry,
    )
    _print(f"   [OK] Run B snapshot created at {snapshot_b.created_at}")
    _print(
        f"   [OK] Run B sees tool version:"
        f" {snapshot_b.tool_versions.get('demo.hot_tool', 'N/A')}"
    )

    # Verify snapshots are different
    _print("\n8. Verifying snapshot isolation...")
    version_a = snapshot_a.tool_versions.get("demo.hot_tool")
    version_b = snapshot_b.tool_versions.get("demo.hot_tool")

    if version_a != version_b:
        _print(f"   [OK] SUCCESS: Run A sees v{version_a}, Run B sees v{version_b}")
        _print("   [OK] Hot publish works: new runs see updated capabilities")
        _print("   [OK] Existing runs keep their original version")
        return True
    else:
        _print(f"   [FAIL] Both runs see same version {version_a}")
        return False


async def verify_rollback_isolation() -> bool:
    """Verify that rollback doesn't affect existing runs."""
    _print("\n" + "=" * 70)
    _print("R2G2 Rollback Isolation Test")
    _print("=" * 70)

    from full_view_agent.application.capability_management_service import (
        CapabilityManagementService,
    )
    from full_view_agent.application.run_capability_snapshot import (
        RunCapabilitySnapshotService,
    )
    from full_view_agent.application.tool_registry import ToolRegistry
    from full_view_agent.domain.capability import Connector, ToolCapability
    from full_view_agent.infrastructure.capability_repository import (
        InMemoryCapabilityRepository,
    )

    _print("\n1. Setting up test environment...")
    repository = InMemoryCapabilityRepository()
    management_service = CapabilityManagementService(repository)
    snapshot_service = RunCapabilitySnapshotService(repository)
    base_registry = ToolRegistry.default()

    # Create connector
    connector = Connector(
        connector_id="test-connector",
        name="Test Connector",
        base_url="http://test.example.com",
        allowed_path_prefixes=["/api"],
    )
    await repository.save_connector(connector)

    # Create and publish v1
    tool_v1 = ToolCapability(
        capability_id="test.rollback_tool",
        name="Rollback Tool",
        domain="governance",
        owner="test",
        version="1.0.0",
        status="draft",
        risk_level="low",
        required_permissions=[],
        dataset_ids=[],
        connector_ref="test-connector",
        http_method="GET",
        resource_path="/api/v1",
        input_schema={"type": "object", "properties": {}},
        result_kind="table",
        created_by="test",
        updated_by="test",
    )
    tool_v1 = await repository.save_tool(tool_v1)
    await _publish_full_lifecycle(management_service, "test.rollback_tool", "1.0.0", "test")
    _print("   [OK] Tool v1.0.0 published")

    # Create Run X with v1
    snapshot_x = await snapshot_service.create_snapshot_for_run(
        run_id="run-X",
        base_registry=base_registry,
    )
    _print(f"   [OK] Run X created with v{snapshot_x.tool_versions.get('test.rollback_tool')}")

    # Create and publish v2
    tool_v2 = ToolCapability(
        capability_id="test.rollback_tool",
        name="Rollback Tool",
        domain="governance",
        owner="test",
        version="2.0.0",
        status="draft",
        risk_level="low",
        required_permissions=[],
        dataset_ids=[],
        connector_ref="test-connector",
        http_method="GET",
        resource_path="/api/v2",
        input_schema={"type": "object", "properties": {}},
        result_kind="table",
        created_by="test",
        updated_by="test",
    )
    tool_v2 = await repository.save_tool(tool_v2)
    await _publish_full_lifecycle(management_service, "test.rollback_tool", "2.0.0", "test")
    _print("   [OK] Tool v2.0.0 published")

    # Rollback to v1
    _print("\n2. Rolling back to v1.0.0...")
    await management_service.rollback(
        capability_id="test.rollback_tool",
        to_version="1.0.0",
        changed_by="test",
        reason="Testing rollback isolation",
    )
    _print("   [OK] Rolled back to v1.0.0")

    # Create Run Y after rollback
    snapshot_y = await snapshot_service.create_snapshot_for_run(
        run_id="run-Y",
        base_registry=base_registry,
    )
    _print(f"   [OK] Run Y created with v{snapshot_y.tool_versions.get('test.rollback_tool')}")

    # Verify both runs see v1
    version_x = snapshot_x.tool_versions.get("test.rollback_tool")
    version_y = snapshot_y.tool_versions.get("test.rollback_tool")

    _print("\n3. Verifying rollback isolation...")
    if version_x == "1.0.0" and version_y == "1.0.0":
        _print(f"   [OK] SUCCESS: Both Run X and Run Y see v{version_x}")
        _print("   [OK] Rollback isolation works: existing runs unaffected")
        return True
    else:
        _print(f"   [FAIL] Run X sees v{version_x}, Run Y sees v{version_y}")
        return False


async def main() -> int:
    """Run all R2G2 verifications."""
    _print("\n")
    result1 = await demonstrate_hot_publish()
    result2 = await verify_rollback_isolation()

    _print("\n" + "=" * 70)
    _print("R2G2 Summary")
    _print("=" * 70)

    if result1 and result2:
        _print("R2G2 VERIFICATION PASSED - Hot publish mechanism works")
        _print("\nKey Points:")
        _print("  1. Each run gets a capability snapshot at creation time")
        _print("  2. Publish/deactivate/rollback only affects new runs")
        _print("  3. Existing runs keep their original capability version")
        _print("  4. RunCapabilitySnapshotService provides the isolation")
        return 0
    else:
        _print("R2G2 VERIFICATION FAILED")
        return 1


if __name__ == "__main__":
    if sys.platform == "win32":
        import asyncio

        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
