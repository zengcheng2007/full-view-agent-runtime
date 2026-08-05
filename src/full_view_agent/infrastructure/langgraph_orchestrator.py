"""LangGraph implementation of the bounded planning/execution loop."""

from typing import Any, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from full_view_agent.application.checkpoint_mapping import CheckpointMappingStore
from full_view_agent.application.errors import ReauthenticationRequired
from full_view_agent.application.harness import (
    AfterToolCall,
    BeforeToolCall,
    FinishAction,
    HarnessAction,
    HarnessControl,
    HarnessResult,
    HarnessToolExecution,
    Planner,
    ToolAction,
)
from full_view_agent.application.native_orchestrator import NativeOrchestrator
from full_view_agent.domain.models import AuthContext
from full_view_agent.infrastructure.checkpoint_mapping_store import (
    InMemoryCheckpointMappingStore,
)
from full_view_agent.infrastructure.langgraph_checkpoint import (
    InMemoryCheckpointManager,
    LangGraphCheckpointManager,
)


class _LoopState(TypedDict):
    control: HarnessControl | None
    action: HarnessAction | None
    execution: HarnessToolExecution | None
    summary: str | None
    use_inherited_references: bool


class LangGraphOrchestrator(NativeOrchestrator):
    """R2 adapter: LangGraph owns every plan/execute/observe transition.

    Harness remains the sole authority for budgets, duplicate detection, tool
    admission and completion validation.  The graph never calls a provider or
    business adapter directly.
    """

    def __init__(
        self,
        *,
        checkpoint_manager: LangGraphCheckpointManager | None = None,
        checkpoint_mappings: CheckpointMappingStore | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._checkpoint_manager = (
            checkpoint_manager or InMemoryCheckpointManager()
        )
        self._checkpoint_mappings = (
            checkpoint_mappings or InMemoryCheckpointMappingStore()
        )

    async def _run_harness(
        self,
        *,
        user_id: str,
        run_id: str,
        session_id: str,
        planner: Planner,
        auth_context: AuthContext,
        before_tool_call: BeforeToolCall | None,
        after_tool_call: AfterToolCall | None,
        inherited_result_ids: tuple[str, ...] = (),
        inherited_evidence_ids: tuple[str, ...] = (),
    ) -> HarnessResult:
        def require_control(state: _LoopState) -> HarnessControl:
            control = state["control"]
            if control is None:
                raise RuntimeError("LangGraph node ran before Harness initialization")
            return control

        def prepare_context(state: _LoopState) -> dict[str, object]:
            del state
            return {
                "control": self._harness.begin(
                    inherited_result_ids=inherited_result_ids,
                    inherited_evidence_ids=inherited_evidence_ids,
                )
            }

        async def plan(state: _LoopState) -> dict[str, object]:
            control, action = await self._harness.plan_action_once(
                planner=planner,
                control=require_control(state),
            )
            return {"control": control, "action": action}

        async def execute(state: _LoopState) -> dict[str, object]:
            action = state["action"]
            if not isinstance(action, ToolAction):
                raise RuntimeError("Tool execution node requires a ToolAction")
            while True:
                try:
                    execution = await self._harness.authorize_and_execute_once(
                        action=action,
                        auth_context=auth_context,
                        control=require_control(state),
                        before_tool_call=before_tool_call,
                        after_tool_call=after_tool_call,
                    )
                    break
                except ReauthenticationRequired as exc:
                    interrupt(
                        {
                            "kind": "reauth",
                            "reason": "credential_required",
                            "message": str(exc),
                        }
                    )
            return {"execution": execution}

        def observe(state: _LoopState) -> dict[str, object]:
            execution = state["execution"]
            if execution is None:
                raise RuntimeError("Observe node requires a Tool execution")
            control = self._harness.observe_once(
                execution=execution,
                control=require_control(state),
            )
            return {"control": control}

        async def validate(state: _LoopState) -> dict[str, object]:
            action = state["action"]
            if action is None:
                raise RuntimeError("Validate node requires a Harness action")
            control, summary = await self._harness.validate_once(
                action=action,
                control=require_control(state),
            )
            return {
                "control": control,
                "action": None,
                "execution": None,
                "summary": summary,
                "use_inherited_references": bool(
                    summary is not None
                    and isinstance(action, FinishAction)
                    and action.structured_finish is not None
                    and action.structured_finish.kind == "reference_only"
                ),
            }

        def route_plan(state: _LoopState) -> str:
            return "validate" if isinstance(state["action"], FinishAction) else "execute"

        def route_validation(state: _LoopState) -> str:
            return "finalize" if state["summary"] is not None else "plan"

        def finalize(state: _LoopState) -> dict[str, object]:
            return {"summary": state["summary"]}

        graph = StateGraph(_LoopState)
        graph.add_node("prepare_context", prepare_context)
        graph.add_node("plan", plan)
        graph.add_node("authorize_and_execute_tool", execute)
        graph.add_node("observe", observe)
        graph.add_node("validate", validate)
        graph.add_node("finalize", finalize)
        graph.add_edge(START, "prepare_context")
        graph.add_edge("prepare_context", "plan")
        graph.add_conditional_edges(
            "plan",
            route_plan,
            {
                "execute": "authorize_and_execute_tool",
                "validate": "validate",
            },
        )
        graph.add_edge("authorize_and_execute_tool", "observe")
        graph.add_edge("observe", "validate")
        graph.add_conditional_edges(
            "validate",
            route_validation,
            {
                "plan": "plan",
                "finalize": "finalize",
            },
        )
        graph.add_edge("finalize", END)
        mapping = await self._checkpoint_mappings.ensure_mapping(
            user_id=user_id,
            run_id=run_id,
            session_id=session_id,
        )
        configurable: dict[str, str] = {
            "thread_id": mapping.thread_id,
            "checkpoint_ns": mapping.checkpoint_ns,
        }
        config: RunnableConfig = {"configurable": configurable}
        # One model action can span plan/execute/observe/validate.  LangGraph's
        # default recursion limit (25) is lower than a valid Harness path with
        # the default eight model turns, so size the framework guard above the
        # authoritative Harness budget.  Harness remains responsible for the
        # user-visible budget/loop outcome.
        config["recursion_limit"] = (4 * self._harness.model_turn_limit) + 5

        async with self._checkpoint_manager.saver() as saver:
            compiled = graph.compile(checkpointer=saver)
            checkpoint = await saver.aget_tuple(config)
            snapshot = (
                await compiled.aget_state(config)
                if checkpoint is not None
                else None
            )
            has_pending_interrupt = bool(
                snapshot is not None
                and any(task.interrupts for task in snapshot.tasks)
            )
            graph_input: _LoopState | Command[Any] | None
            if has_pending_interrupt:
                graph_input = Command(
                    resume={
                        "type": "reauthenticated",
                        "run_id": run_id,
                    }
                )
            elif checkpoint is not None:
                graph_input = None
            else:
                graph_input = {
                    "control": None,
                    "action": None,
                    "execution": None,
                    "summary": None,
                    "use_inherited_references": False,
                }
            try:
                final = await compiled.ainvoke(graph_input, config)
            finally:
                latest = await saver.aget_tuple(
                    {
                        "configurable": {
                            "thread_id": mapping.thread_id,
                            "checkpoint_ns": mapping.checkpoint_ns,
                        }
                    }
                )
                if latest is not None:
                    latest_checkpoint_id = latest.config.get(
                        "configurable", {}
                    ).get("checkpoint_id")
                    if latest_checkpoint_id is None:
                        raise RuntimeError(
                            "LangGraph checkpoint is missing checkpoint_id"
                        )
                    await self._checkpoint_mappings.record_checkpoint(
                        user_id=user_id,
                        run_id=run_id,
                        checkpoint_id=str(latest_checkpoint_id),
                        expected_version=mapping.version,
                    )
            current_snapshot = await compiled.aget_state(config)
            if any(task.interrupts for task in current_snapshot.tasks):
                raise ReauthenticationRequired(
                    "LangGraph execution is waiting for reauthentication"
                )
        summary = final["summary"]
        if summary is None:
            raise RuntimeError("LangGraph loop finalized without a summary")
        control = final["control"]
        if control is None:
            raise RuntimeError("LangGraph loop finalized without Harness state")
        return HarnessResult(
            summary=summary,
            state=control.state,
            # Checkpoints written before this field existed remain readable;
            # absence must fail closed to no historical references.
            use_inherited_references=bool(
                final.get("use_inherited_references", False)
            ),
        )
