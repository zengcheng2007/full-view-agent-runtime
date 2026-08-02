"""Native orchestrator migrated from MockRunExecutor.

This is the current production loop. It will coexist with
LangGraphOrchestrator behind OrchestrationPort until the latter
passes acceptance and becomes the default.
"""

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Protocol

from full_view_agent.application.capability_service import (
    AuthContextRefresher,
    CapabilityService,
    DenialLedger,
)
from full_view_agent.application.errors import (
    BudgetExceeded,
    LoopDetected,
    ModelContractError,
    ModelProviderTimeout,
    ModelProviderUnavailable,
    ReauthenticationRequired,
    ResourceNotFound,
)
from full_view_agent.application.fingerprints import canonical_fingerprint
from full_view_agent.application.harness import (
    AgentHarness,
    DeterministicCompletionValidator,
    FinishAction,
    HarnessState,
    Planner,
    ToolAction,
)
from full_view_agent.application.policy import MinimalPolicyAdapter
from full_view_agent.application.ports import AgentStore, EventPublisher, OrchestrationPort
from full_view_agent.application.session_run_service import SessionRunService, new_id
from full_view_agent.application.tool_registry import ToolRegistry
from full_view_agent.domain.models import (
    AgentMessage,
    AuthContext,
    DataResult,
    Evidence,
    FrontendCommand,
    FrontendCommandPreconditions,
    MapRenderChoroplethPayload,
    PanelShowTablePayload,
    ResultReferenceContent,
    Steer,
    TableDataResult,
    TextContent,
    ToolResult,
)

logger = logging.getLogger(__name__)


class CapabilityExecutor(Protocol):
    async def execute(
        self,
        *,
        tool_call_id: str,
        tool_id: str,
        raw_arguments: dict[str, object],
        auth_context: AuthContext,
    ) -> ToolResult: ...


class AuthContextProvider(Protocol):
    async def get(self, *, user_id: str, run_id: str) -> AuthContext: ...


class RunPlannerFactory(Protocol):
    def create(self, *, user_id: str, auth_context: AuthContext) -> Planner: ...


class PopulationQueryPlanner:
    async def decide(self, state: HarnessState) -> ToolAction | FinishAction:
        if state.tool_results:
            # Compatibility planner emits a fixed server-owned summary.
            return FinishAction(summary="人口指标查询已完成", legacy=True)
        return ToolAction(
            tool_id="governance.query_population_metrics",
            arguments={
                "query": {
                    "metrics": ["person_count"],
                    "scope": {"area_code": "330106"},
                    "filters": [
                        {
                            "field": "person_category",
                            "operator": "eq",
                            "value": "solitary_elderly",
                        }
                    ],
                    "group_by": ["street"],
                }
            },
        )


class NativeOrchestrator(OrchestrationPort):
    def __init__(
        self,
        *,
        service: SessionRunService,
        store: AgentStore,
        events: EventPublisher,
        auth_context_provider: AuthContextProvider,
        capability: CapabilityExecutor | None = None,
        auth_context_refresher: AuthContextRefresher | None = None,
        denial_ledger: DenialLedger | None = None,
        harness: AgentHarness | None = None,
        registry: ToolRegistry | None = None,
        planner_factory: RunPlannerFactory | None = None,
        evidence_source_system: str = "in_memory_fixture",
    ) -> None:
        self._service = service
        self._store = store
        self._events = events
        self._auth_context_provider = auth_context_provider
        if capability is None:
            from full_view_agent.infrastructure.governance_adapter import (
                InMemoryGovernanceAdapter,
            )

            capability = CapabilityService(
                registry=ToolRegistry.default(),
                policy=MinimalPolicyAdapter(),
                adapter=InMemoryGovernanceAdapter(),
                auth_context_refresher=auth_context_refresher,
                denial_ledger=denial_ledger,
            )
        self._capability = capability
        self._harness = harness or AgentHarness(
            tool_executor=capability,
            validator=DeterministicCompletionValidator(),
        )
        self._registry = registry or ToolRegistry.default()
        self._planner_factory = planner_factory
        self._evidence_source_system = evidence_source_system
        self._run_tasks: dict[str, asyncio.Task[None]] = {}
        self._task_failures: list[str] = []

    async def execute(self, *, user_id: str, run_id: str) -> None:
        try:
            await self._execute(user_id=user_id, run_id=run_id)
        except Exception:
            logger.exception("unexpected run execution failure", extra={"run_id": run_id})
            current = await self._store.get_run(user_id=user_id, run_id=run_id)
            if current.status != "running":
                return
            failed = await self._service.fail_run(
                user_id=user_id,
                run_id=run_id,
                completion_reason_code="internal_error",
            )
            await self._publish(
                failed,
                "run.failed",
                {
                    "status": failed.status,
                    "outcome": failed.outcome,
                    "error_code": "internal_error",
                    "message": "运行时发生未预期错误。",
                },
            )

    async def _execute(self, *, user_id: str, run_id: str) -> None:
        current = await self._store.get_run(user_id=user_id, run_id=run_id)
        if current.status in (
            "completed", "failed", "cancelled", "expired",
        ):
            return  # terminal – must not resume or start tools
        was_queued = current.status == "queued"
        running = (
            await self._service.start_run(user_id=user_id, run_id=run_id)
            if was_queued
            else current
        )
        await self._publish(
            running,
            "run.started" if was_queued else "run.resumed",
            {"status": "running"},
        )
        for steer in await self._store.apply_pending_steers(user_id=user_id, run_id=run_id):
            await self._publish(
                running,
                "steer.applied",
                {"steer_id": steer.steer_id, "delivery": steer.delivery},
            )
        auth_context = await self._auth_context_provider.get(
            user_id=user_id,
            run_id=run_id,
        )
        inherited_result_ids, inherited_evidence_ids = (
            await self._load_inherited_grounding(
                user_id=user_id,
                session_id=running.session_id,
                current_run_id=run_id,
            )
        )
        tool_actions: dict[str, ToolAction] = {}

        async def before_tool_call(action: ToolAction, tool_call_id: str) -> None:
            tool_actions[tool_call_id] = action
            await self._publish(
                running,
                "tool.started",
                {"tool_call_id": tool_call_id, "tool_id": action.tool_id},
            )

        async def after_tool_call(result: ToolResult) -> None:
            latest = await self._store.get_run(user_id=user_id, run_id=run_id)
            if latest.status != "running":
                return
            await self._publish(
                latest,
                "tool.failed" if result.status == "failed" else "tool.completed",
                {"tool_result": result.model_dump(mode="json")},
            )

        try:
            planner = (
                self._planner_factory.create(
                    user_id=user_id,
                    auth_context=auth_context,
                )
                if self._planner_factory is not None
                else PopulationQueryPlanner()
            )
            harness_result = await self._run_harness(
                user_id=user_id,
                run_id=run_id,
                session_id=running.session_id,
                planner=planner,
                auth_context=auth_context,
                before_tool_call=before_tool_call,
                after_tool_call=after_tool_call,
                inherited_result_ids=inherited_result_ids,
                inherited_evidence_ids=inherited_evidence_ids,
            )
        except ReauthenticationRequired:
            waiting, input_request = await self._service.wait_for_reauthentication(
                user_id=user_id,
                run_id=run_id,
            )
            event_data = {
                "status": waiting.status,
                "waiting_for": waiting.waiting_for,
                "input_request_id": input_request.input_request_id,
                "kind": input_request.kind,
                "prompt": input_request.prompt,
                "options": [
                    option.model_dump(mode="json") for option in input_request.options
                ],
                "allow_free_text": input_request.allow_free_text,
                "run_state_version": input_request.run_state_version,
                "expires_at": input_request.expires_at.isoformat(),
            }
            await self._publish(waiting, "run.waiting", event_data)
            await self._publish(waiting, "input.required", event_data)
            await self._publish(waiting, "reauth_required", event_data)
            return
        except (
            BudgetExceeded,
            LoopDetected,
            ModelContractError,
            ModelProviderTimeout,
            ModelProviderUnavailable,
        ) as exc:
            error_code = (
                exc.root_cause_code
                if isinstance(exc, BudgetExceeded) and exc.root_cause_code
                else exc.code
            )
            error_message = (
                exc.root_cause_message
                if isinstance(exc, BudgetExceeded) and exc.root_cause_message
                else str(exc)
            )
            failed = await self._service.fail_run(
                user_id=user_id,
                run_id=run_id,
                completion_reason_code=error_code,
            )
            await self._publish(
                failed,
                "run.failed",
                {
                    "status": failed.status,
                    "outcome": failed.outcome,
                    "error_code": error_code,
                    "message": error_message,
                },
            )
            return
        current = await self._store.get_run(user_id=user_id, run_id=run_id)
        if current.status != "running":
            return
        # LangGraph recovery may resume after the tool node in a fresh process.
        # Rebuild this transient lookup from checkpointed Harness state so that
        # durable observations never require repeating a completed Tool call.
        tool_actions.update(
            zip(
                harness_result.state.tool_call_ids,
                harness_result.state.tool_actions,
                strict=True,
            )
        )
        tool_results = list(harness_result.state.tool_results)
        if not tool_results:
            await self._complete_success(
                user_id=user_id,
                run=running,
                summary=harness_result.summary,
                result_references=[
                    ResultReferenceContent(
                        type="result_reference",
                        result_id=result_id,
                        label="沿用会话中已验证的结果",
                    )
                    for result_id in harness_result.state.inherited_result_ids
                ],
                evidence_ids=list(harness_result.state.inherited_evidence_ids),
                warning_count=0,
            )
            return
        tool_result = tool_results[-1]
        if tool_result.status == "failed":
            error_code = tool_result.warnings[0] if tool_result.warnings else "tool_failed"
            failed = await self._service.fail_run(
                user_id=user_id,
                run_id=run_id,
                completion_reason_code=error_code,
            )
            await self._publish(
                failed,
                "run.failed",
                {
                    "status": failed.status,
                    "outcome": failed.outcome,
                    "error_code": error_code,
                    "message": tool_result.summary,
                },
            )
            return
        if tool_result.status == "denied":
            completed = await self._service.complete_run(
                user_id=user_id,
                run_id=run_id,
                outcome="denied",
                completion_reason_code=(
                    tool_result.warnings[0]
                    if tool_result.warnings
                    else "policy_denied"
                ),
            )
            await self._publish(
                completed,
                "run.completed",
                {
                    "status": completed.status,
                    "outcome": completed.outcome,
                    "completion_reason_code": completed.completion_reason_code,
                },
            )
            return
        result_references: list[ResultReferenceContent] = []
        evidence_ids: list[str] = []
        for observed_result in tool_results:
            if observed_result.status not in {"success", "partial"}:
                continue
            data_result, evidence = await self._persist_tool_observation(
                user_id=user_id,
                run=running,
                action=tool_actions[observed_result.tool_call_id],
                tool_result=observed_result,
            )
            result_references.append(
                ResultReferenceContent(
                    type="result_reference",
                    result_id=data_result.result_id,
                    label=observed_result.summary,
                )
            )
            evidence_ids.append(evidence.evidence_id)
            await self._request_frontend_commands(
                user_id=user_id,
                run=running,
                action=tool_actions[observed_result.tool_call_id],
                tool_result=observed_result,
                data_result=data_result,
            )
        await self._complete_success(
            user_id=user_id,
            run=running,
            summary=harness_result.summary,
            result_references=result_references,
            evidence_ids=evidence_ids,
            warning_count=sum(len(result.warnings) for result in tool_results),
        )

    async def _run_harness(
        self,
        *,
        user_id: str,
        run_id: str,
        session_id: str,
        planner: Planner,
        auth_context: AuthContext,
        before_tool_call,
        after_tool_call,
        inherited_result_ids: tuple[str, ...] = (),
        inherited_evidence_ids: tuple[str, ...] = (),
    ):
        """Native fallback: the Harness owns the loop directly."""
        del user_id, run_id, session_id
        return await self._harness.run(
            planner=planner,
            auth_context=auth_context,
            before_tool_call=before_tool_call,
            after_tool_call=after_tool_call,
            inherited_result_ids=inherited_result_ids,
            inherited_evidence_ids=inherited_evidence_ids,
        )

    async def _load_inherited_grounding(
        self,
        *,
        user_id: str,
        session_id: str,
        current_run_id: str,
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """Reload prior result/evidence references through the owned store.

        Historical assistant prose alone is never treated as trusted data.
        A follow-up inherits grounding only when both the referenced result
        payload and its evidence still exist for the same session owner.
        """
        result_ids: list[str] = []
        evidence_ids: list[str] = []
        messages = await self._store.list_messages(
            user_id=user_id,
            session_id=session_id,
        )
        for message in messages:
            if (
                message.role != "assistant"
                or message.run_id == current_run_id
                or not message.evidence_ids
            ):
                continue
            referenced_ids = [
                item.result_id
                for item in message.content
                if isinstance(item, ResultReferenceContent)
            ]
            if not referenced_ids:
                continue
            valid_results: list[str] = []
            valid_evidence: list[str] = []
            try:
                for result_id in referenced_ids:
                    result = await self._store.get_result(
                        user_id=user_id,
                        result_id=result_id,
                    )
                    if getattr(result, "payload_status", "available") == "available":
                        valid_results.append(result_id)
                for evidence_id in message.evidence_ids:
                    await self._store.get_evidence(
                        user_id=user_id,
                        evidence_id=evidence_id,
                    )
                    valid_evidence.append(evidence_id)
            except ResourceNotFound:
                continue
            if valid_results and valid_evidence:
                result_ids.extend(valid_results)
                evidence_ids.extend(valid_evidence)
        return tuple(dict.fromkeys(result_ids)), tuple(dict.fromkeys(evidence_ids))

    async def _persist_tool_observation(
        self,
        *,
        user_id: str,
        run,
        action: ToolAction,
        tool_result: ToolResult,
    ) -> tuple[DataResult, Evidence]:
        data_result = tool_result.data_result
        if data_result is None:
            raise RuntimeError("successful tool produced no data result")
        evidence_id = new_id("evd")
        data_result = data_result.model_copy(update={"evidence_ids": [evidence_id]})
        await self._store.save_result(
            user_id=user_id,
            run_id=run.run_id,
            result=data_result,
        )
        now = datetime.now(UTC)
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
        # S1-A：语义入口执行时，Evidence 的语义登记版本、指标口径与
        # 有效区域来自结果血缘（解析时固化的 spec/plan 版本指纹所对应的
        # 目录与主题），规范 Tool 字段仍由生产 manifest 提供。
        lineage = tool_result.semantic_lineage
        evidence = Evidence.model_validate(
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
                    _action_area_codes(action)
                    or ([lineage.area_code] if lineage is not None else [])
                ),
                "time_range": None,
                "as_of": None,
                "retrieved_at": now,
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
        await self._store.save_evidence(
            user_id=user_id,
            run_id=run.run_id,
            evidence=evidence,
        )
        await self._publish(run, "result.available", {"result_id": data_result.result_id})
        await self._publish(
            run,
            "evidence.available",
            {"evidence_id": evidence_id, "result_id": data_result.result_id},
        )
        return data_result, evidence

    async def _request_frontend_commands(
        self,
        *,
        user_id: str,
        run,
        action: ToolAction,
        tool_result: ToolResult,
        data_result: DataResult,
    ) -> None:
        client = run.client_capabilities
        if not isinstance(data_result, TableDataResult) or client is None:
            return
        if "1.0" not in client.frontend_command_schema_versions:
            return
        now = datetime.now(UTC)
        area_codes = _action_area_codes(action)
        # S1-A：semantic_query 的前端命令按血缘中的规范 Tool 判定，
        # 与直接调用规范 Tool 的行为完全一致（含地图分级设色）。
        canonical_tool_id = (
            tool_result.semantic_lineage.canonical_tool_id
            if tool_result.semantic_lineage is not None
            else action.tool_id
        )
        commands: list[FrontendCommand] = []
        if "panel.show_table" in client.supported_commands:
            commands.append(
                FrontendCommand(
                    command_id=new_id("cmd"),
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
                    command_id=new_id("cmd"),
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
        for command in commands:
            await self._save_and_publish_frontend_command(
                user_id=user_id,
                run=run,
                command=command,
            )

    async def _save_and_publish_frontend_command(
        self,
        *,
        user_id: str,
        run,
        command: FrontendCommand,
    ) -> None:
        await self._store.save_frontend_command(
            user_id=user_id,
            run_id=run.run_id,
            command=command,
        )
        await self._publish(
            run,
            "frontend.command.requested",
            {"command": command.model_dump(mode="json")},
        )

    async def _complete_success(
        self,
        *,
        user_id: str,
        run,
        summary: str,
        result_references: list[ResultReferenceContent],
        evidence_ids: list[str],
        warning_count: int,
    ) -> None:
        content: list[TextContent | ResultReferenceContent] = [
            TextContent(type="text", text=summary),
            *result_references,
        ]
        result_message = AgentMessage(
            message_id=new_id("msg"),
            session_id=run.session_id,
            run_id=run.run_id,
            role="assistant",
            content=content,
            evidence_ids=evidence_ids,
        )
        await self._store.save_message(
            user_id=user_id,
            run_id=run.run_id,
            message=result_message,
        )
        await self._publish(
            run,
            "assistant.message.completed",
            {"message": result_message.model_dump(mode="json")},
        )
        completed = await self._service.complete_run(
            user_id=user_id,
            run_id=run.run_id,
            outcome="success",
            completion_reason_code="goal_completed",
        )
        await self._publish(
            completed,
            "run.completed",
            {
                "status": completed.status,
                "outcome": completed.outcome,
                "completion_reason_code": completed.completion_reason_code,
                "result_message_id": result_message.message_id,
                "warning_count": warning_count,
            },
        )

    async def _publish(
        self, run, event_type: str, data: dict[str, object]
    ) -> None:
        await self._events.publish(
            event_type=event_type,
            session_id=run.session_id,
            run_id=run.run_id,
            data=data,
        )

    # ------------------------------------------------------------------
    # OrchestrationPort lifecycle methods
    # ------------------------------------------------------------------

    def schedule(self, *, user_id: str, run_id: str) -> None:
        """Fire-and-forget: create an asyncio task for execute()."""
        existing = self._run_tasks.get(run_id)
        if existing is not None and not existing.done():
            return
        task = asyncio.create_task(
            self.execute(user_id=user_id, run_id=run_id)
        )
        self._run_tasks[run_id] = task
        task.add_done_callback(lambda t: self._on_task_done(run_id, t))

    @property
    def task_failures(self) -> list[str]:
        """Read-only list of non-cancelled task exceptions (for observability)."""
        return list(self._task_failures)

    def _on_task_done(
        self, run_id: str, task: asyncio.Task[None]
    ) -> None:
        if self._run_tasks.get(run_id) is task:
            self._run_tasks.pop(run_id, None)
        if task.cancelled():
            return  # normal cancellation – not a failure
        exc = task.exception()
        if exc is not None:
            self._task_failures.append(repr(exc))
            logger.error(
                "orchestrator task exception",
                exc_info=exc,
                extra={"run_id": run_id},
            )

    async def cancel(self, *, user_id: str, run_id: str) -> None:
        """Cancel a Run.  No-op when already terminal."""
        current = await self._store.get_run(
            user_id=user_id, run_id=run_id
        )
        if current.status in (
            "completed", "failed", "cancelled", "expired",
        ):
            return  # terminal – no events, no tool calls
        run = await self._service.cancel_run(
            user_id=user_id, run_id=run_id
        )
        task = self._run_tasks.pop(run_id, None)
        if task is not None and not task.done():
            task.cancel()
        await self._publish(
            run,
            "run.cancelled",
            {
                "status": run.status,
                "outcome": run.outcome,
                "completion_reason_code": run.completion_reason_code,
            },
        )

    async def resume(
        self,
        *,
        user_id: str,
        run_id: str,
        input_request_id: str,
        run_state_version: int,
    ) -> None:
        """Resume after user input / reauth.

        Only performs the controlled state transition.  The caller (API)
        must call ``schedule()`` after idempotency succeeds and
        ``input.received`` is published.
        """
        await self._service.resume_from_input(
            user_id=user_id,
            run_id=run_id,
            input_request_id=input_request_id,
            run_state_version=run_state_version,
        )

    async def steer(
        self,
        *,
        user_id: str,
        run_id: str,
        client_instance_id: str,
        content: str,
    ) -> Steer:
        """Record a steer instruction and publish event.  Returns Steer."""
        steer = await self._service.steer_run(
            user_id=user_id,
            run_id=run_id,
            client_instance_id=client_instance_id,
            content=content,
        )
        run = await self._store.get_run(
            user_id=user_id, run_id=run_id
        )
        await self._publish(
            run,
            "steer.accepted",
            {
                "steer_id": steer.steer_id,
                "delivery": steer.delivery,
            },
        )
        return steer

    async def shutdown(self) -> None:
        """Cancel all running asyncio tasks."""
        for task in list(self._run_tasks.values()):
            if not task.done():
                task.cancel()
        self._run_tasks.clear()


def _action_area_codes(action: ToolAction) -> list[str]:
    # 规范 Tool 参数结构为 {"query": {"scope": ...}}；S1-A 语义入口
    # 的原始动作为 {"spec": {"scope": ...}}，两者都能提取声明区域。
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


class MockRunExecutor(NativeOrchestrator):
    """Deprecated alias for transition. Use NativeOrchestrator."""
