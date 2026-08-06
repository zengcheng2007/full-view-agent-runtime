#!/usr/bin/env python
"""Simple verification that R2G1 dynamic tool execution infrastructure is in place."""

import asyncio
import inspect
import sys


def _print(msg: str) -> None:
    """Print with encoding-safe fallback for Windows GBK consoles."""
    try:
        print(msg)
    except UnicodeEncodeError:
        # Strip non-ASCII for GBK consoles
        print(msg.encode("ascii", "replace").decode("ascii"))


def verify_imports() -> bool:
    """Verify all R2G1 components can be imported."""
    _print("Verifying R2G1 imports...")

    try:
        from full_view_agent.application.capability_service import (  # noqa: F401
            CapabilityService,
            DynamicToolAdapter,
        )
        from full_view_agent.application.dynamic_tool_adapter import (  # noqa: F401
            HttpDynamicToolAdapter,
        )
        from full_view_agent.infrastructure.http_connector_executor import (  # noqa: F401
            HttpConnectorExecutor,
        )

        _print(
            "  [OK] CapabilityService, DynamicToolAdapter,"
            " HttpDynamicToolAdapter, HttpConnectorExecutor imported"
        )
        return True
    except Exception as e:
        _print(f"  [FAIL] Import failed: {e}")
        return False


def verify_capability_service_signature() -> bool:
    """Verify CapabilityService accepts dynamic_tool_adapter."""
    _print("\nVerifying CapabilityService signature...")

    try:
        from full_view_agent.application.capability_service import CapabilityService

        sig = inspect.signature(CapabilityService.__init__)
        params = list(sig.parameters.keys())

        if "dynamic_tool_adapter" in params:
            _print("  [OK] CapabilityService.__init__ has dynamic_tool_adapter parameter")
            _print(f"    Parameters: {params}")
            return True
        else:
            _print(f"  [FAIL] dynamic_tool_adapter not found in parameters: {params}")
            return False
    except Exception as e:
        _print(f"  [FAIL] Failed to verify signature: {e}")
        return False


def verify_dynamic_execution_method() -> bool:
    """Verify _execute_dynamic_tool method exists."""
    _print("\nVerifying _execute_dynamic_tool method...")

    try:
        from full_view_agent.application.capability_service import CapabilityService

        if hasattr(CapabilityService, "_execute_dynamic_tool"):
            _print("  [OK] _execute_dynamic_tool method exists")
            return True
        else:
            _print("  [FAIL] _execute_dynamic_tool method not found")
            return False
    except Exception as e:
        _print(f"  [FAIL] Failed to verify method: {e}")
        return False


async def verify_json_schema_validation() -> bool:
    """Verify JSON Schema validation works."""
    _print("\nVerifying JSON Schema validation...")

    try:
        import jsonschema

        # Test basic validation
        schema = {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "age": {"type": "integer"},
            },
            "required": ["name"],
        }

        # Valid instance
        jsonschema.validate(instance={"name": "Alice", "age": 30}, schema=schema)
        _print("  [OK] Valid instance passes validation")

        # Invalid instance (missing required field)
        try:
            jsonschema.validate(instance={"age": 30}, schema=schema)
            _print("  [FAIL] Invalid instance should have failed validation")
            return False
        except jsonschema.ValidationError:
            _print("  [OK] Invalid instance correctly fails validation")

        return True
    except Exception as e:
        _print(f"  [FAIL] JSON Schema validation failed: {e}")
        return False


def verify_wiring() -> bool:
    """Verify dynamic_tool_adapter is wired in orchestrator_factory."""
    _print("\nVerifying wiring in orchestrator_factory...")

    try:
        from full_view_agent.application.orchestrator_factory import create_orchestrator

        sig = inspect.signature(create_orchestrator)
        params = list(sig.parameters.keys())

        if "dynamic_tool_adapter" in params:
            _print("  [OK] create_orchestrator has dynamic_tool_adapter parameter")
            return True
        else:
            _print(f"  [FAIL] dynamic_tool_adapter not in create_orchestrator: {params}")
            return False
    except Exception as e:
        _print(f"  [FAIL] Failed to verify wiring: {e}")
        return False


async def main() -> int:
    """Run all verifications."""
    _print("=" * 70)
    _print("R2G1 Dynamic Tool Real Execution - Verification")
    _print("=" * 70)

    results: list[tuple[str, bool]] = []

    results.append(("Imports", verify_imports()))
    results.append(("CapabilityService Signature", verify_capability_service_signature()))
    results.append(("Dynamic Execution Method", verify_dynamic_execution_method()))
    results.append(("JSON Schema Validation", await verify_json_schema_validation()))
    results.append(("Wiring", verify_wiring()))

    _print("\n" + "=" * 70)
    _print("Summary")
    _print("=" * 70)

    for name, result in results:
        status = "PASS" if result else "FAIL"
        _print(f"  [{status}]: {name}")

    all_passed = all(result for _, result in results)

    _print("\n" + "=" * 70)
    if all_passed:
        _print("R2G1 VERIFICATION PASSED - All components in place")
        _print("=" * 70)
        return 0
    else:
        _print("R2G1 VERIFICATION FAILED - Some components missing")
        _print("=" * 70)
        return 1


if __name__ == "__main__":
    if sys.platform == "win32":
        import asyncio

        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
