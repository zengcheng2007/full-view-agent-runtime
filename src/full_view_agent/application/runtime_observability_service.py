from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from importlib.metadata import PackageNotFoundError, version
from math import ceil
from typing import Any, Literal, cast

from full_view_agent.application.errors import ResourceNotFound, RunStateConflict
from full_view_agent.application.fingerprints import canonical_fingerprint
from full_view_agent.domain.models import AgentEvent, AgentRun
from full_view_agent.domain.runtime_observability import (
    RuntimeAlert,
    RuntimeCapabilityMetric,
    RuntimeCapabilityRef,
    RuntimeDependencyStatus,
    RuntimeIdentity,
    RuntimeLatency,
    RuntimeModelMetric,
    RuntimeModelRef,
    RuntimeOverview,
    RuntimeReadiness,
    RuntimeRunItem,
    RuntimeSessionItem,
    RuntimeTimeline,
    RuntimeTimelineItem,
)
from full_view_agent.infrastructure.runtime_observability_repository import (
    RuntimeObservabilityRepository,
)

TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled", "expired"})
CapabilityKind = Literal["tool", "skill", "workflow"]
TimelineCategory = Literal[
    "auth",
    "release",
    "capability",
    "model",
    "tool",
    "skill",
    "workflow",
    "result",
    "evidence",
    "final",
    "frontend_command",
    "run",
]
ACTIVE_STATUSES = frozenset(
    {"queued", "running", "waiting_input", "waiting_approval", "cancelling"}
)


def _runtime_version() -> str:
    try:
        return version("full-view-agent")
    except PackageNotFoundError:
        return "0.0.0+local"


def _duration_ms(run: AgentRun) -> int | None:
    if run.started_at is None or run.completed_at is None:
        return None
    return max(0, round((run.completed_at - run.started_at).total_seconds() * 1000))


def _percentile(values: list[int], percentile: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, ceil(percentile * len(ordered)) - 1)
    return ordered[index]


def _latency(values: list[int]) -> RuntimeLatency:
    return RuntimeLatency(p50=_percentile(values, 0.5), p95=_percentile(values, 0.95))


def _safe_window(
    window_from: datetime | None, window_to: datetime | None
) -> tuple[datetime, datetime]:
    end = window_to or datetime.now(UTC)
    start = window_from or end - timedelta(hours=24)
    if start.tzinfo is None or end.tzinfo is None:
        raise RunStateConflict("runtime observability timestamps must include timezone")
    if start > end:
        raise RunStateConflict("runtime observability window start must not exceed end")
    if end - start > timedelta(days=31):
        raise RunStateConflict("runtime observability window must not exceed 31 days")
    return start, end


class RuntimeObservabilityService:
    def __init__(
        self,
        *,
        repository: RuntimeObservabilityRepository,
        event_store: Any,
        runtime_started_at: datetime,
        store: Any,
        capability_generation: Callable[[], int],
        loaded_counts: Callable[[], tuple[int, int, int]],
        model_binding_repository: Any,
        auth_context_store: Any,
        agent_repository: Any,
        capability_snapshot_store: Any,
        event_retention_seconds: int,
        capability_repository: Any,
        event_now: Callable[[], datetime],
    ) -> None:
        self._repository = repository
        self._event_store = event_store
        self._runtime_started_at = runtime_started_at
        self._store = store
        self._capability_generation = capability_generation
        self._loaded_counts = loaded_counts
        self._model_binding_repository = model_binding_repository
        self._auth_context_store = auth_context_store
        self._agent_repository = agent_repository
        self._capability_snapshot_store = capability_snapshot_store
        self._event_retention_seconds = event_retention_seconds
        self._capability_repository = capability_repository
        self._event_now = event_now

    def event_window(
        self, window_from: datetime | None, window_to: datetime | None
    ) -> tuple[datetime, datetime, datetime, bool]:
        end = window_to or datetime.now(UTC)
        requested_start = window_from or end - timedelta(hours=24)
        requested_start, end = _safe_window(requested_start, end)
        effective_start = max(
            requested_start,
            end - timedelta(seconds=self._event_retention_seconds),
        )
        return requested_start, end, effective_start, effective_start > requested_start

    async def _readiness(self) -> RuntimeReadiness:
        dependencies: list[RuntimeDependencyStatus] = []
        for name, dependency in (
            ("store", self._store),
            ("event_broker", self._event_store),
        ):
            if dependency is None or not hasattr(dependency, "health_check"):
                dependencies.append(RuntimeDependencyStatus(name=name, status="missing"))
                continue
            try:
                await dependency.health_check()
            except Exception:
                dependencies.append(RuntimeDependencyStatus(name=name, status="error"))
            else:
                dependencies.append(RuntimeDependencyStatus(name=name, status="ok"))
        return RuntimeReadiness(
            status=("ok" if all(item.status == "ok" for item in dependencies) else "degraded"),
            dependencies=dependencies,
        )

    async def overview(
        self,
        *,
        tenant_id: str,
        app_id: str,
        window_from: datetime | None,
        window_to: datetime | None,
    ) -> RuntimeOverview:
        start, end = _safe_window(window_from, window_to)
        sessions = await self._repository.list_sessions(
            tenant_id=tenant_id,
            app_id=app_id,
            created_from=start,
            created_to=end,
        )
        runs = await self._repository.list_runs(
            tenant_id=tenant_id,
            app_id=app_id,
            created_from=start,
            created_to=end,
        )
        terminal = [run for run in runs if run.status in TERMINAL_STATUSES]
        terminal_statuses = Counter(run.status for run in terminal)
        outcomes = Counter(run.outcome for run in terminal if run.outcome is not None)
        success_count = sum(1 for run in terminal if run.outcome == "success")
        latencies = [value for run in terminal if (value := _duration_ms(run)) is not None]
        tools, skills, workflows = self._loaded_counts()
        return RuntimeOverview(
            application_id=app_id,
            window_started_at=start,
            window_ended_at=end,
            runtime=RuntimeIdentity(
                version=_runtime_version(),
                started_at=self._runtime_started_at,
                capability_generation=self._capability_generation(),
                global_loaded_tools=tools,
                global_loaded_skills=skills,
                global_loaded_workflows=workflows,
            ),
            readiness=await self._readiness(),
            session_count=len(sessions),
            run_count=len(runs),
            active_run_count=sum(1 for run in runs if run.status in ACTIVE_STATUSES),
            terminal_status_distribution=dict(sorted(terminal_statuses.items())),
            outcome_distribution=dict(sorted(outcomes.items())),
            success_rate=(success_count / len(terminal) if terminal else None),
            latency_ms=_latency(latencies),
        )

    async def list_sessions(
        self,
        *,
        tenant_id: str,
        app_id: str,
        status: Literal["active", "archived"] | None,
        created_from: datetime | None,
        created_to: datetime | None,
        limit: int | None = None,
        before: tuple[datetime, str] | None = None,
    ) -> list[RuntimeSessionItem]:
        start, end = _safe_window(created_from, created_to)
        sessions = await self._repository.list_sessions(
            tenant_id=tenant_id,
            app_id=app_id,
            status=status,
            created_from=start,
            created_to=end,
            limit=limit,
            before=before,
        )
        counts = await self._repository.count_runs_by_session(
            tenant_id=tenant_id,
            app_id=app_id,
            session_ids=[item.session_id for item in sessions],
        )
        return [
            RuntimeSessionItem(
                session_id=item.session_id,
                title=item.title,
                status=item.status,
                active_run_id=item.active_run_id,
                created_at=item.created_at,
                updated_at=item.updated_at,
                version=item.version,
                run_count=counts[item.session_id],
            )
            for item in sessions
        ]

    async def list_runs(
        self,
        *,
        tenant_id: str,
        app_id: str,
        session_id: str | None,
        status: str | None,
        outcome: str | None,
        mode: str | None,
        created_from: datetime | None,
        created_to: datetime | None,
        limit: int | None = None,
        before: tuple[datetime, str] | None = None,
    ) -> list[RuntimeRunItem]:
        start, end = _safe_window(created_from, created_to)
        runs = await self._repository.list_runs(
            tenant_id=tenant_id,
            app_id=app_id,
            session_id=session_id,
            status=status,
            outcome=outcome,
            mode=mode,
            created_from=start,
            created_to=end,
            limit=limit,
            before=before,
        )
        return [self._run_item(run) for run in runs]

    @staticmethod
    def _run_item(run: AgentRun) -> RuntimeRunItem:
        return RuntimeRunItem(
            run_id=run.run_id,
            session_id=run.session_id,
            status=run.status,
            outcome=run.outcome,
            mode=run.mode,
            current_phase=run.current_phase,
            waiting_for=run.waiting_for,
            completion_reason_code=run.completion_reason_code,
            created_at=run.created_at,
            started_at=run.started_at,
            completed_at=run.completed_at,
            duration_ms=_duration_ms(run),
        )

    async def timeline(self, *, tenant_id: str, app_id: str, run_id: str) -> RuntimeTimeline:
        run = await self._repository.get_run(tenant_id=tenant_id, app_id=app_id, run_id=run_id)
        if run is None:
            raise ResourceNotFound("runtime run not found")
        events: list[AgentEvent] = await self._event_store.list_events(run_id=run_id)
        retention_cutoff = self._event_now() - timedelta(seconds=self._event_retention_seconds)
        event_history_status: Literal["within_retention", "expired_or_partial"] = (
            "expired_or_partial"
            if run.created_at < retention_cutoff or (events and events[0].sequence > 1)
            else "within_retention"
        )
        items = [
            RuntimeTimelineItem(
                timeline_id=f"run:{run.run_id}:created",
                occurred_at=run.created_at,
                category="run",
                event_type="run.created",
                stage="setup",
                detail_level="technical",
                display_label="创建运行任务",
                display_summary="Runtime 已创建任务并等待执行。",
                status="queued",
            )
        ]
        owner_user_id = await self._repository.get_run_owner(
            tenant_id=tenant_id, app_id=app_id, run_id=run_id
        )
        if owner_user_id is None:
            raise ResourceNotFound("runtime run not found")
        if self._auth_context_store is not None:
            try:
                auth_context = await self._auth_context_store.get(
                    user_id=owner_user_id, run_id=run_id
                )
            except ResourceNotFound:
                auth_context = None
            if (
                auth_context is not None
                and auth_context.principal.tenant_id == tenant_id
                and auth_context.application.app_id == app_id
            ):
                items.append(
                    RuntimeTimelineItem(
                        timeline_id=f"auth:{auth_context.auth_context_id}",
                        occurred_at=auth_context.issued_at,
                        category="auth",
                        event_type="auth.bound",
                        stage="setup",
                        detail_level="technical",
                        display_label="绑定访问身份",
                        display_summary="已固定本次运行的身份与数据权限。",
                        status=(
                            "active" if auth_context.expires_at > datetime.now(UTC) else "expired"
                        ),
                        details={
                            "auth_context_id": auth_context.auth_context_id,
                            "auth_context_fingerprint": auth_context.auth_context_fingerprint,
                            "policy_version": auth_context.policy_version,
                            "expires_at": auth_context.expires_at.isoformat(),
                        },
                    )
                )
        if self._agent_repository is not None:
            release = await self._agent_repository.get_run_snapshot(run_id)
            if release is not None and release.tenant_id == tenant_id and release.app_id == app_id:
                items.append(
                    RuntimeTimelineItem(
                        timeline_id=f"release:{release.release_id}",
                        occurred_at=release.bound_at,
                        category="release",
                        event_type="agent_release.bound",
                        stage="setup",
                        detail_level="technical",
                        display_label="固定智能体版本",
                        display_summary="已固定本次运行使用的智能体发布版本。",
                        status="pinned",
                        details={
                            "release_id": release.release_id,
                            "agent_id": release.agent_id,
                            "agent_version": release.agent_version,
                        },
                    )
                )
        if self._capability_snapshot_store is not None:
            snapshot = await self._capability_snapshot_store.load(run_id)
            if snapshot is not None:
                items.append(
                    RuntimeTimelineItem(
                        timeline_id=f"capability:{run_id}:snapshot",
                        occurred_at=snapshot.captured_at,
                        category="capability",
                        event_type="capability.snapshot.pinned",
                        stage="setup",
                        detail_level="technical",
                        display_label="固定能力清单",
                        display_summary="已固定本次运行允许使用的能力版本。",
                        status="pinned",
                        details={
                            "tool_count": len(snapshot.tool_versions)
                            + len(snapshot.static_tool_versions),
                            "skill_count": len(snapshot.skill_versions),
                            "workflow_count": len(snapshot.workflow_versions),
                            "application_scoped": snapshot.application_scoped,
                            "agent_scoped": snapshot.agent_scoped,
                        },
                    )
                )
                for capability_type, versions in (
                    (
                        "tool",
                        {**snapshot.static_tool_versions, **snapshot.tool_versions},
                    ),
                    ("skill", snapshot.skill_versions),
                    ("workflow", snapshot.workflow_versions),
                ):
                    capability_type = cast(CapabilityKind, capability_type)
                    for capability_id, capability_version in sorted(versions.items()):
                        items.append(
                            RuntimeTimelineItem(
                                timeline_id=(
                                    f"{capability_type}:{capability_id}:{capability_version}"
                                ),
                                occurred_at=snapshot.captured_at,
                                category=capability_type,
                                event_type=f"{capability_type}.pinned",
                                stage="setup",
                                detail_level="technical",
                                display_label="固定能力版本",
                                display_summary="该能力版本已纳入本次运行快照。",
                                status="pinned",
                                capability_ref=RuntimeCapabilityRef(
                                    capability_id=capability_id,
                                    version=capability_version,
                                    type=capability_type,
                                ),
                            )
                        )
        if self._model_binding_repository is not None:
            binding = await self._model_binding_repository.load_binding(run_id)
            if binding is not None:
                model_snapshot = await self._model_binding_repository.load_snapshot(
                    config_id=binding.config_id,
                    config_version=binding.config_version,
                )
                items.append(
                    RuntimeTimelineItem(
                        timeline_id=(f"model:{binding.config_id}:{binding.config_version}"),
                        occurred_at=binding.bound_at,
                        category="model",
                        event_type="model.bound",
                        stage="setup",
                        detail_level="technical",
                        display_label="固定模型版本",
                        display_summary="已固定本次运行使用的模型配置版本。",
                        status=("pinned" if model_snapshot is not None else "snapshot_missing"),
                        stable_error_code=(
                            None if model_snapshot is not None else "model_snapshot_missing"
                        ),
                        model_ref=RuntimeModelRef(
                            config_id=binding.config_id,
                            config_version=binding.config_version,
                            model_name=(
                                model_snapshot.model_name if model_snapshot is not None else None
                            ),
                        ),
                    )
                )
        starts: dict[tuple[str, str], datetime] = {}
        for event in events:
            item = self._timeline_event(event, starts)
            if item is not None:
                items.append(item)
        if run.completed_at is not None and not any(
            item.event_type in {"run.completed", "run.failed", "run.cancelled"} for item in items
        ):
            items.append(
                RuntimeTimelineItem(
                    timeline_id=f"run:{run.run_id}:terminal",
                    occurred_at=run.completed_at,
                    category="run",
                    event_type=f"run.{run.status}",
                    stage="terminal",
                    detail_level="summary",
                    display_label="运行结束",
                    display_summary="本次运行已结束。",
                    status=run.status,
                    stable_error_code=run.completion_reason_code,
                    duration_ms=_duration_ms(run),
                )
            )
        items.sort(key=lambda item: (item.occurred_at, item.timeline_id))
        return RuntimeTimeline(
            application_id=app_id,
            run_id=run.run_id,
            session_id=run.session_id,
            event_history_status=event_history_status,
            event_retention_cutoff=retention_cutoff,
            run=self._run_item(run),
            items=items,
            result_ids=_unique(value for item in items for value in item.result_ids),
            evidence_ids=_unique(value for item in items for value in item.evidence_ids),
            frontend_command_ids=_unique(
                value for item in items for value in item.frontend_command_ids
            ),
        )

    def _timeline_event(
        self,
        event: AgentEvent,
        starts: dict[tuple[str, str], datetime],
    ) -> RuntimeTimelineItem | None:
        category = _event_category(event.type)
        if category is None:
            return None
        data = event.data
        capability_ref = _capability_ref(event.type, data)
        identity = _operation_identity(event.type, data)
        duration_ms = None
        if event.type.endswith(".started") or event.type == "model.requested":
            if identity is not None:
                starts[(category, identity)] = event.occurred_at
        elif identity is not None:
            started = starts.get((category, identity))
            if started is not None:
                duration_ms = max(0, round((event.occurred_at - started).total_seconds() * 1000))
        result_ids = _safe_ids(data, "result_id")
        evidence_ids = _safe_ids(data, "evidence_id")
        command_ids = _frontend_command_ids(data)
        tool_result = data.get("tool_result")
        status = _safe_scalar(data.get("status"))
        error_code = _safe_scalar(data.get("error_code"))
        if isinstance(tool_result, dict):
            status = _safe_scalar(tool_result.get("status")) or status
            result_ids.extend(_tool_result_result_ids(tool_result))
            evidence_ids.extend(_safe_string_list(tool_result.get("evidence_ids")))
            warnings = _safe_string_list(tool_result.get("warnings"))
            error_code = warnings[0] if warnings else error_code
        details = _safe_details(event.type, data)
        stage, detail_level, display_label, display_summary = _timeline_display(
            event.type, data=data, status=status
        )
        return RuntimeTimelineItem(
            timeline_id=event.event_id,
            occurred_at=event.occurred_at,
            category=category,
            event_type=event.type,
            stage=stage,
            detail_level=detail_level,
            display_label=display_label,
            display_summary=display_summary,
            status=status,
            duration_ms=duration_ms,
            stable_error_code=error_code,
            capability_ref=capability_ref,
            result_ids=_unique(result_ids),
            evidence_ids=_unique(evidence_ids),
            frontend_command_ids=_unique(command_ids),
            details=details,
        )

    async def model_metrics(
        self,
        *,
        tenant_id: str,
        app_id: str,
        window_from: datetime | None,
        window_to: datetime | None,
    ) -> list[RuntimeModelMetric]:
        _, end, start, _ = self.event_window(window_from, window_to)
        runs = await self._repository.list_runs(
            tenant_id=tenant_id, app_id=app_id, created_from=start, created_to=end
        )
        buckets: dict[tuple[str, int, str | None, str | None], dict[str, Any]] = defaultdict(
            lambda: {
                "requests": 0,
                "responses": 0,
                "failures": 0,
                "last_error": None,
                "latencies": [],
                "fallback_runs": set(),
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "usage_events": 0,
            }
        )
        events_by_run: dict[str, list[AgentEvent]] = defaultdict(list)
        for event in await self._repository.list_events(
            tenant_id=tenant_id,
            app_id=app_id,
            run_ids=[run.run_id for run in runs],
        ):
            events_by_run[event.run_id].append(event)
        authorities = await self._repository.list_model_authorities(
            tenant_id=tenant_id,
            app_id=app_id,
            run_ids=[run.run_id for run in runs],
        )
        for run in runs:
            events = events_by_run[run.run_id]
            authority = authorities.get(run.run_id)
            agent_id = authority.agent_id if authority is not None else None
            fallback_key = (
                authority.config_id if authority is not None else "unknown",
                authority.config_version if authority is not None else 0,
                authority.model_name if authority is not None else None,
                agent_id,
            )
            selected_is_fallback = authority.is_fallback if authority is not None else False
            started: dict[str, tuple[datetime, tuple[str, int, str | None, str | None]]] = {}
            for event in events:
                turn = str(event.data.get("model_turn", ""))
                event_config_id = event.data.get("config_id")
                event_config_version = event.data.get("config_version")
                event_model_name = event.data.get("model_name")
                key = (
                    (
                        event_config_id,
                        event_config_version,
                        event_model_name if isinstance(event_model_name, str) else None,
                        agent_id,
                    )
                    if isinstance(event_config_id, str) and isinstance(event_config_version, int)
                    else fallback_key
                )
                bucket = buckets[key]
                if selected_is_fallback:
                    bucket["fallback_runs"].add(run.run_id)
                if event.type == "model.requested":
                    bucket["requests"] += 1
                    started[turn] = (event.occurred_at, key)
                elif event.type == "model.responded":
                    bucket["responses"] += 1
                    usage = event.data.get("usage")
                    if isinstance(usage, dict):
                        token_values = tuple(
                            usage.get(name)
                            for name in ("prompt_tokens", "completion_tokens", "total_tokens")
                        )
                        if all(
                            isinstance(value, int) and not isinstance(value, bool) and value >= 0
                            for value in token_values
                        ):
                            bucket["prompt_tokens"] += token_values[0]
                            bucket["completion_tokens"] += token_values[1]
                            bucket["total_tokens"] += token_values[2]
                            bucket["usage_events"] += 1
                    if turn in started:
                        started_at, started_key = started[turn]
                        buckets[started_key]["latencies"].append(
                            max(
                                0,
                                round((event.occurred_at - started_at).total_seconds() * 1000),
                            )
                        )
                elif event.type == "model.failed":
                    bucket["failures"] += 1
                    bucket["last_error"] = _safe_scalar(event.data.get("error_code"))
                    if turn in started:
                        started_at, started_key = started[turn]
                        buckets[started_key]["latencies"].append(
                            max(0, round((event.occurred_at - started_at).total_seconds() * 1000))
                        )
        return [
            RuntimeModelMetric(
                config_id=config_id,
                config_version=config_version,
                model_name=model_name,
                agent_id=agent_id,
                request_count=bucket["requests"],
                response_count=bucket["responses"],
                failure_count=bucket["failures"],
                fallback_run_count=len(bucket["fallback_runs"]),
                prompt_tokens=bucket["prompt_tokens"],
                completion_tokens=bucket["completion_tokens"],
                total_tokens=bucket["total_tokens"],
                usage_event_count=bucket["usage_events"],
                success_rate=(
                    bucket["responses"] / bucket["requests"] if bucket["requests"] else None
                ),
                latency_ms=_latency(bucket["latencies"]),
                last_error_code=bucket["last_error"],
            )
            for (config_id, config_version, model_name, agent_id), bucket in sorted(
                buckets.items(),
                key=lambda item: (item[0][0], item[0][1], item[0][2] or "", item[0][3] or ""),
            )
            if bucket["requests"]
        ]

    async def capability_metrics(
        self,
        *,
        tenant_id: str,
        app_id: str,
        window_from: datetime | None,
        window_to: datetime | None,
        capability_type: Literal["tool", "skill", "workflow"] | None,
    ) -> list[RuntimeCapabilityMetric]:
        _, end, start, _ = self.event_window(window_from, window_to)
        runs = await self._repository.list_runs(
            tenant_id=tenant_id, app_id=app_id, created_from=start, created_to=end
        )
        buckets: dict[tuple[str, str | None, str], dict[str, Any]] = defaultdict(
            lambda: {
                "invocations": 0,
                "success": 0,
                "failure": 0,
                "denied": 0,
                "latencies": [],
                "last_error": None,
            }
        )
        events_by_run: dict[str, list[AgentEvent]] = defaultdict(list)
        for event in await self._repository.list_events(
            tenant_id=tenant_id,
            app_id=app_id,
            run_ids=[run.run_id for run in runs],
        ):
            events_by_run[event.run_id].append(event)
        for run in runs:
            starts: dict[tuple[str, str], tuple[datetime, RuntimeCapabilityRef]] = {}
            for event in events_by_run[run.run_id]:
                category = _event_category(event.type)
                if category not in {"tool", "skill", "workflow"}:
                    continue
                ref = _capability_ref(event.type, event.data)
                if ref is None or (capability_type is not None and ref.type != capability_type):
                    continue
                identity = _operation_identity(event.type, event.data) or ref.capability_id
                if event.type.endswith(".started"):
                    starts[(ref.type, identity)] = (event.occurred_at, ref)
                    continue
                started_entry = starts.pop((ref.type, identity), None)
                effective_ref = ref
                if ref.version is None and started_entry is not None:
                    effective_ref = started_entry[1]
                key = (effective_ref.capability_id, effective_ref.version, effective_ref.type)
                bucket = buckets[key]
                bucket["invocations"] += 1
                tool_result = event.data.get("tool_result")
                status = event.data.get("status")
                if isinstance(tool_result, dict):
                    status = tool_result.get("status")
                    warnings = _safe_string_list(tool_result.get("warnings"))
                    if warnings:
                        bucket["last_error"] = warnings[0]
                if status == "denied":
                    bucket["denied"] += 1
                elif status in {"success", "partial"} or event.type.endswith(".completed"):
                    bucket["success"] += 1
                else:
                    bucket["failure"] += 1
                if started_entry is not None:
                    bucket["latencies"].append(
                        max(0, round((event.occurred_at - started_entry[0]).total_seconds() * 1000))
                    )
            for started_at, ref in starts.values():
                del started_at
                bucket = buckets[(ref.capability_id, ref.version, ref.type)]
                bucket["invocations"] += 1
        connector_refs: dict[tuple[str, str], str] = {}
        if self._capability_repository is not None:
            capabilities = await self._capability_repository.list_capabilities(
                capability_type="tool"
            )
            connector_refs = {
                (item.capability_id, item.version): item.connector_ref
                for item in capabilities
                if isinstance(getattr(item, "connector_ref", None), str)
            }
        metrics: list[RuntimeCapabilityMetric] = []
        for (capability_id, capability_version, cap_type), bucket in sorted(
            buckets.items(), key=lambda item: (item[0][0], item[0][1] or "", item[0][2])
        ):
            connector_id = None
            if cap_type == "tool" and capability_version is not None:
                connector_id = connector_refs.get((capability_id, capability_version))
            metrics.append(
                RuntimeCapabilityMetric(
                    capability_id=capability_id,
                    capability_version=capability_version,
                    capability_type=cast(CapabilityKind, cap_type),
                    connector_id=connector_id,
                    invocation_count=bucket["invocations"],
                    success_count=bucket["success"],
                    failure_count=bucket["failure"],
                    denied_count=bucket["denied"],
                    success_rate=(
                        bucket["success"] / bucket["invocations"] if bucket["invocations"] else None
                    ),
                    latency_ms=_latency(bucket["latencies"]),
                    last_error_code=bucket["last_error"],
                )
            )
        return metrics

    async def alerts(
        self,
        *,
        tenant_id: str,
        app_id: str,
        window_from: datetime | None,
        window_to: datetime | None,
        severity: Literal["warning", "critical"] | None,
    ) -> list[RuntimeAlert]:
        start, end = _safe_window(window_from, window_to)
        overview = await self.overview(
            tenant_id=tenant_id,
            app_id=app_id,
            window_from=start,
            window_to=end,
        )
        alerts: list[RuntimeAlert] = []
        if overview.readiness.status == "degraded":
            alerts.append(
                _alert(
                    app_id,
                    "critical",
                    "readiness_degraded",
                    "Runtime 就绪依赖处于降级状态。",
                    1,
                    0,
                    start,
                    end,
                )
            )
        terminal = sum(overview.terminal_status_distribution.values())
        failed_count = overview.outcome_distribution.get("failed", 0)
        failure_rate = failed_count / terminal if terminal else 0
        if terminal >= 5 and failure_rate >= 0.2:
            alerts.append(
                _alert(
                    app_id,
                    "warning",
                    "run_failure_rate_high",
                    "终态运行失败率达到或超过确定性阈值。",
                    failure_rate,
                    0.2,
                    start,
                    end,
                )
            )
        if (overview.latency_ms.p95 or 0) >= 30_000 and terminal >= 5:
            alerts.append(
                _alert(
                    app_id,
                    "warning",
                    "run_p95_latency_high",
                    "运行 P95 耗时达到或超过确定性阈值。",
                    float(overview.latency_ms.p95 or 0),
                    30_000,
                    start,
                    end,
                )
            )
        if severity is not None:
            alerts = [alert for alert in alerts if alert.severity == severity]
        return alerts


def _event_category(event_type: str) -> TimelineCategory | None:
    if event_type.startswith("model."):
        return "model"
    if event_type.startswith("tool."):
        return "tool"
    if event_type.startswith("skill."):
        return "skill"
    if event_type.startswith("workflow."):
        return "workflow"
    if event_type == "result.available":
        return "result"
    if event_type == "evidence.available":
        return "evidence"
    if event_type == "frontend.command.requested":
        return "frontend_command"
    if event_type == "assistant.message.completed":
        return "final"
    if event_type.startswith("run.") or event_type in {
        "input.required",
        "reauth_required",
    }:
        return "run"
    return None


def _operation_identity(event_type: str, data: dict[str, object]) -> str | None:
    if event_type.startswith("model."):
        return _safe_scalar(data.get("model_turn"))
    for key in ("tool_call_id", "skill_call_id", "workflow_run_id"):
        value = _safe_scalar(data.get(key))
        if value:
            return value
    result = data.get("tool_result")
    return _safe_scalar(result.get("tool_call_id")) if isinstance(result, dict) else None


def _capability_ref(event_type: str, data: dict[str, object]) -> RuntimeCapabilityRef | None:
    category = _event_category(event_type)
    if category not in {"tool", "skill", "workflow"}:
        return None
    capability_kind = cast(CapabilityKind, category)
    result = data.get("tool_result")
    source = result if isinstance(result, dict) else data
    key = {
        "tool": "tool_id",
        "skill": "skill_id",
        "workflow": "workflow_id",
    }[capability_kind]
    capability_id = _safe_scalar(source.get(key))
    if not capability_id:
        return None
    version_value = source.get(f"{capability_kind}_version")
    return RuntimeCapabilityRef(
        capability_id=capability_id,
        version=_safe_scalar(version_value),
        type=capability_kind,
    )


def _safe_scalar(value: object) -> str | None:
    if isinstance(value, (str, int)) and not isinstance(value, bool):
        return str(value)[:256]
    return None


def _safe_string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item[:256] for item in value if isinstance(item, str)]


def _safe_ids(data: dict[str, object], key: str) -> list[str]:
    value = data.get(key)
    return [value] if isinstance(value, str) else []


def _tool_result_result_ids(tool_result: dict[str, object]) -> list[str]:
    result = tool_result.get("data_result")
    if not isinstance(result, dict):
        return []
    value = result.get("result_id")
    return [value] if isinstance(value, str) else []


def _frontend_command_ids(data: dict[str, object]) -> list[str]:
    command = data.get("command")
    if not isinstance(command, dict):
        return []
    value = command.get("command_id")
    return [value] if isinstance(value, str) else []


def _timeline_display(
    event_type: str,
    *,
    data: dict[str, object],
    status: str | None,
) -> tuple[
    Literal["setup", "reasoning", "execution", "output", "terminal"],
    Literal["summary", "technical"],
    str,
    str,
]:
    if event_type.startswith("model."):
        turn = _safe_scalar(data.get("model_turn")) or "?"
        if event_type == "model.requested":
            return (
                "reasoning",
                "summary",
                f"第 {turn} 轮模型分析",
                "模型正在理解当前步骤并选择下一项操作。",
            )
        if event_type == "model.responded":
            return (
                "reasoning",
                "summary",
                f"第 {turn} 轮模型分析",
                "模型已完成本轮判断。",
            )
        return (
            "reasoning",
            "summary",
            f"第 {turn} 轮模型分析",
            "模型调用失败。",
        )
    if event_type.startswith(("tool.", "skill.", "workflow.")):
        display = data.get("display")
        tool_result = data.get("tool_result")
        if not isinstance(display, dict) and isinstance(tool_result, dict):
            display = tool_result.get("display")
        label = (
            _safe_scalar(display.get("label"))
            if isinstance(display, dict)
            else None
        ) or "执行业务能力"
        if event_type.endswith(".started"):
            summary = "业务能力正在执行。"
        elif status in {"failed", "denied"} or event_type.endswith(".failed"):
            summary = "业务能力执行失败。"
        else:
            summary = "业务能力已执行完成。"
        if isinstance(display, dict):
            safe_summary = _safe_scalar(display.get("summary"))
            if safe_summary and any("\u3400" <= char <= "\u9fff" for char in safe_summary):
                summary = safe_summary
        return "execution", "summary", label, summary
    if event_type == "result.available":
        return "output", "summary", "生成查询结果", "结构化查询结果已生成。"
    if event_type == "evidence.available":
        return "output", "summary", "生成证据记录", "结果证据已生成并关联。"
    if event_type == "assistant.message.completed":
        return "output", "summary", "生成最终回答", "最终回答已生成。"
    if event_type == "frontend.command.requested":
        return "output", "technical", "下发界面联动", "已请求前端执行结果联动。"
    if event_type in {"run.completed", "run.failed", "run.cancelled"}:
        summary = {
            "run.completed": "本次运行已完成。",
            "run.failed": "本次运行失败。",
            "run.cancelled": "本次运行已取消。",
        }[event_type]
        return "terminal", "summary", "运行结束", summary
    return "execution", "technical", "运行状态变化", "Runtime 状态已更新。"


def _safe_details(
    event_type: str, data: dict[str, object]
) -> dict[str, str | int | float | bool | None]:
    allowed = {
        "model.requested": (
            "model_turn",
            "message_count",
            "prompt_version",
            "requested_inference_mode",
            "effective_inference_mode",
            "thinking_enabled",
            "reasoning_effort",
        ),
        "model.responded": ("model_turn", "finish_reason", "content_present"),
        "run.completed": ("status", "outcome", "completion_reason_code", "warning_count"),
        "run.failed": ("status", "outcome", "error_code"),
    }.get(event_type, ())
    details: dict[str, str | int | float | bool | None] = {}
    for key in allowed:
        value = data.get(key)
        if value is None or isinstance(value, (str, int, float, bool)):
            details[key] = value[:256] if isinstance(value, str) else value
    if event_type == "model.responded":
        usage = data.get("usage")
        if isinstance(usage, dict):
            for key in (
                "prompt_tokens",
                "completion_tokens",
                "total_tokens",
                "reasoning_tokens",
            ):
                value = usage.get(key)
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                    details[key] = value
    return details


def _unique(values: Any) -> list[str]:
    return list(dict.fromkeys(value for value in values if isinstance(value, str)))


def _alert(
    app_id: str,
    severity: Literal["warning", "critical"],
    code: Any,
    summary: str,
    observed: float,
    threshold: float,
    start: datetime,
    end: datetime,
) -> RuntimeAlert:
    return RuntimeAlert(
        alert_id=canonical_fingerprint(
            domain="runtime-alert:1.0",
            value={"app_id": app_id, "code": code, "start": start, "end": end},
        ),
        severity=severity,
        code=code,
        summary=summary,
        observed_value=observed,
        threshold=threshold,
        window_started_at=start,
        window_ended_at=end,
    )
