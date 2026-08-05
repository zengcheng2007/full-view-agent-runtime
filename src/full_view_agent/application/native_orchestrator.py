"""Native orchestrator migrated from MockRunExecutor.

This is the current production loop. It will coexist with
LangGraphOrchestrator behind OrchestrationPort until the latter
passes acceptance and becomes the default.
"""

import asyncio
import logging
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
from full_view_agent.application.tool_observation_service import (
    PersistedToolObservation,
    ToolObservationPort,
    ToolObservationService,
    action_area_codes,
)
from full_view_agent.application.tool_registry import ToolRegistry
from full_view_agent.domain.models import (
    AgentMessage,
    AuthContext,
    ResultReferenceContent,
    Steer,
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
        observation_service: ToolObservationPort | None = None,
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
        self._observation_service = observation_service or ToolObservationService(
            store=store,
            events=events,
            registry=self._registry,
            evidence_source_system=evidence_source_system,
        )
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
        persisted_observations: dict[str, PersistedToolObservation] = {}

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
            if result.status in {"success", "partial"}:
                action = tool_actions.get(result.tool_call_id)
                if action is None:
                    raise RuntimeError("completed tool call is missing its action")
                persisted_observations[result.tool_call_id] = (
                    await self._observation_service.persist(
                        user_id=user_id,
                        run=latest,
                        action=action,
                        tool_result=result,
                    )
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
            inherited_result_ids = (
                harness_result.state.inherited_result_ids
                if harness_result.use_inherited_references
                else ()
            )
            inherited_evidence_ids = (
                harness_result.state.inherited_evidence_ids
                if harness_result.use_inherited_references
                else ()
            )
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
                    for result_id in inherited_result_ids
                ],
                evidence_ids=list(inherited_evidence_ids),
                warning_count=0,
            )
            return
        tool_result = tool_results[-1]
        if harness_result.outcome != "partial" and tool_result.status == "failed":
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
        if harness_result.outcome != "partial" and tool_result.status == "denied":
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
            observation = persisted_observations.get(observed_result.tool_call_id)
            if observation is None:
                observation = await self._observation_service.persist(
                    user_id=user_id,
                    run=running,
                    action=tool_actions[observed_result.tool_call_id],
                    tool_result=observed_result,
                )
            result_references.append(
                ResultReferenceContent(
                    type="result_reference",
                    result_id=observation.data_result.result_id,
                    label=observed_result.summary,
                )
            )
            evidence_ids.append(observation.evidence.evidence_id)
        await self._complete_success(
            user_id=user_id,
            run=running,
            summary=harness_result.summary,
            result_references=result_references,
            evidence_ids=evidence_ids,
            warning_count=sum(len(result.warnings) for result in tool_results),
            outcome=harness_result.outcome,
            completion_reason_code=harness_result.completion_reason_code,
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

    async def _complete_success(
        self,
        *,
        user_id: str,
        run,
        summary: str,
        result_references: list[ResultReferenceContent],
        evidence_ids: list[str],
        warning_count: int,
        outcome: str = "success",
        completion_reason_code: str = "goal_completed",
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
            outcome="partial" if outcome == "partial" else "success",
            completion_reason_code=completion_reason_code,
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
    return action_area_codes(action)


class MockRunExecutor(NativeOrchestrator):
    """Deprecated alias for transition. Use NativeOrchestrator."""
