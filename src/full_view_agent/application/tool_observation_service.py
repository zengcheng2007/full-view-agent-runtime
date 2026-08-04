"""Shared persistence boundary for successful Tool observations."""

from dataclasses import dataclass
from datetime import timedelta
from typing import Protocol

from full_view_agent.application.fingerprints import canonical_fingerprint
from full_view_agent.application.harness import ToolAction
from full_view_agent.application.ports import AgentStore, EventPublisher
from full_view_agent.application.tool_registry import ToolRegistry
from full_view_agent.domain.models import (
    AgentRun,
    DataResult,
    Evidence,
    FrontendCommand,
    FrontendCommandPreconditions,
    MapRenderChoroplethPayload,
    PanelShowTablePayload,
    TableDataResult,
    ToolResult,
)


@dataclass(frozen=True)
class PersistedToolObservation:
    """Durable references produced from one successful Tool result."""

    data_result: DataResult
    evidence: Evidence


class ToolObservationPort(Protocol):
    """Application port shared by native and analysis execution paths."""

    async def persist(
        self,
        *,
        user_id: str,
        run: AgentRun,
        action: ToolAction,
        tool_result: ToolResult,
    ) -> PersistedToolObservation: ...


class ToolObservationService:
    """Persist one observation before exposing events or UI commands.

    AgentStore remains the authority for ownership, active-run checks and
    duplicate identities. No event or frontend command is emitted until both
    the Result and its Evidence have been durably accepted.
    """

    def __init__(
        self,
        *,
        store: AgentStore,
        events: EventPublisher,
        registry: ToolRegistry,
        evidence_source_system: str,
    ) -> None:
        self._store = store
        self._events = events
        self._registry = registry
        self._evidence_source_system = evidence_source_system

    async def persist(
        self,
        *,
        user_id: str,
        run: AgentRun,
        action: ToolAction,
        tool_result: ToolResult,
    ) -> PersistedToolObservation:
        data_result = tool_result.data_result
        if data_result is None:
            raise RuntimeError("successful tool produced no data result")
        # Adapter-generated result ids are intentionally not trusted as the
        # observation identity: a crash may re-execute the same tool call and
        # produce a fresh random id.  Bind the durable result to the run-scoped
        # tool call so replay converges before any row or event is written.
        result_id = canonical_fingerprint(
            domain="tool-observation-result:1.0",
            value={"run_id": run.run_id, "tool_call_id": tool_result.tool_call_id},
        )
        data_result = data_result.model_copy(update={"result_id": result_id})
        evidence_id = canonical_fingerprint(
            domain="tool-observation-evidence:1.0",
            value={
                "run_id": run.run_id,
                "tool_call_id": tool_result.tool_call_id,
                "result_id": data_result.result_id,
                "result_fingerprint": data_result.result_fingerprint,
            },
        )
        data_result = data_result.model_copy(update={"evidence_ids": [evidence_id]})
        evidence = self._build_evidence(
            run=run,
            action=action,
            tool_result=tool_result,
            data_result=data_result,
            evidence_id=evidence_id,
        )
        commands = self._build_frontend_commands(
            run=run,
            action=action,
            tool_result=tool_result,
            data_result=data_result,
        )
        data_result, evidence, commands = await self._store.save_tool_observation(
            user_id=user_id,
            run_id=run.run_id,
            result=data_result,
            evidence=evidence,
            commands=commands,
        )
        await self._publish(
            run,
            "result.available",
            {"result_id": data_result.result_id},
            idempotency_key=f"{tool_result.tool_call_id}:result.available",
        )
        await self._publish(
            run,
            "evidence.available",
            {"evidence_id": evidence_id, "result_id": data_result.result_id},
            idempotency_key=f"{tool_result.tool_call_id}:evidence.available",
        )
        for command in commands:
            await self._publish(
                run,
                "frontend.command.requested",
                {"command": command.model_dump(mode="json")},
                idempotency_key=(
                    f"{tool_result.tool_call_id}:frontend.command:{command.type}"
                ),
            )
        return PersistedToolObservation(data_result=data_result, evidence=evidence)

    def _build_evidence(
        self,
        *,
        run: AgentRun,
        action: ToolAction,
        tool_result: ToolResult,
        data_result: DataResult,
        evidence_id: str,
    ) -> Evidence:
        policy_fingerprint = (
            tool_result.policy.policy_fingerprint
            if tool_result.policy is not None
            else canonical_fingerprint(
                domain="evidence-policy:unavailable",
                value={"run_id": run.run_id, "tool_call_id": tool_result.tool_call_id},
            )
        )
        query_fingerprint = (
            tool_result.policy.request_fingerprint
            if tool_result.policy is not None
            else canonical_fingerprint(
                domain="evidence-query:unavailable",
                value={"tool_id": tool_result.tool_id, "result_id": data_result.result_id},
            )
        )
        manifest = self._registry.get_manifest(tool_result.tool_id)
        lineage = tool_result.semantic_lineage
        return Evidence.model_validate(
            {
                "evidence_id": evidence_id,
                "result_id": data_result.result_id,
                "result_fingerprint": data_result.result_fingerprint,
                "source_system": self._evidence_source_system,
                "dataset_id": manifest.dataset_id,
                "dataset_snapshot_version": None,
                "semantic_registry_version": (
                    lineage.catalog_version if lineage is not None else None
                ),
                "metric_definitions": (
                    [
                        definition.model_dump(mode="json")
                        for definition in lineage.metric_definitions
                    ]
                    if lineage is not None
                    else []
                ),
                "effective_area_codes": (
                    action_area_codes(action)
                    or ([lineage.area_code] if lineage is not None else [])
                ),
                "time_range": None,
                "as_of": None,
                "retrieved_at": data_result.created_at,
                "query_fingerprint": query_fingerprint,
                "policy_fingerprint": policy_fingerprint,
                "tool": {
                    "tool_id": tool_result.tool_id,
                    "tool_version": tool_result.tool_version,
                },
                "freshness": {
                    "status": "unknown",
                    "expected_update_cycle": None,
                },
            }
        )

    def _build_frontend_commands(
        self,
        *,
        run: AgentRun,
        action: ToolAction,
        tool_result: ToolResult,
        data_result: DataResult,
    ) -> tuple[FrontendCommand, ...]:
        client = run.client_capabilities
        if not isinstance(data_result, TableDataResult) or client is None:
            return ()
        if "1.0" not in client.frontend_command_schema_versions:
            return ()
        now = data_result.created_at
        area_codes = action_area_codes(action)
        canonical_tool_id = (
            tool_result.semantic_lineage.canonical_tool_id
            if tool_result.semantic_lineage is not None
            else action.tool_id
        )
        commands: list[FrontendCommand] = []
        if "panel.show_table" in client.supported_commands:
            commands.append(
                FrontendCommand(
                    command_id=canonical_fingerprint(
                        domain="tool-observation-command:1.0",
                        value={
                            "run_id": run.run_id,
                            "tool_call_id": tool_result.tool_call_id,
                            "type": "panel.show_table",
                        },
                    ),
                    run_id=run.run_id,
                    target_client_instance_id=client.client_instance_id,
                    type="panel.show_table",
                    issued_at=now,
                    expires_at=now + timedelta(minutes=5),
                    preconditions=FrontendCommandPreconditions(
                        session_id=run.session_id,
                        area_code=area_codes[0] if area_codes else None,
                        required_client_capability="panel.show_table@1.0",
                    ),
                    payload=PanelShowTablePayload(result_id=data_result.result_id),
                )
            )
        if (
            canonical_tool_id == "governance.query_population_metrics"
            and "map.render_choropleth" in client.supported_commands
        ):
            commands.append(
                FrontendCommand(
                    command_id=canonical_fingerprint(
                        domain="tool-observation-command:1.0",
                        value={
                            "run_id": run.run_id,
                            "tool_call_id": tool_result.tool_call_id,
                            "type": "map.render_choropleth",
                        },
                    ),
                    run_id=run.run_id,
                    target_client_instance_id=client.client_instance_id,
                    type="map.render_choropleth",
                    target="map_panel",
                    issued_at=now,
                    expires_at=now + timedelta(minutes=5),
                    preconditions=FrontendCommandPreconditions(
                        session_id=run.session_id,
                        area_code=area_codes[0] if area_codes else None,
                        required_client_capability="map.render_choropleth@1.0",
                    ),
                    payload=MapRenderChoroplethPayload(result_id=data_result.result_id),
                )
            )
        return tuple(commands)

    async def _publish(
        self,
        run: AgentRun,
        event_type: str,
        data: dict[str, object],
        idempotency_key: str | None = None,
    ) -> None:
        await self._events.publish(
            event_type=event_type,
            session_id=run.session_id,
            run_id=run.run_id,
            data=data,
            idempotency_key=idempotency_key,
        )


def action_area_codes(action: ToolAction) -> list[str]:
    """Extract the declared area scope from canonical or semantic actions."""

    for key in ("query", "spec"):
        container = action.arguments.get(key)
        if not isinstance(container, dict):
            continue
        scope = container.get("scope")
        if not isinstance(scope, dict):
            continue
        area_code = scope.get("area_code")
        if isinstance(area_code, str) and area_code:
            return [area_code]
    return []
