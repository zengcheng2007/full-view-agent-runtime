from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query

from full_view_agent.api._api_deps import (
    CurrentUser,
    ResponseMeta,
    require_capability_identity,
)
from full_view_agent.application.control_plane_authorization import (
    ControlPlaneAuthorizer,
    ControlPlanePermission,
)
from full_view_agent.application.errors import InvalidCursor, ResourceNotFound
from full_view_agent.application.runtime_observability_service import (
    RuntimeObservabilityService,
)
from full_view_agent.application.session_run_service import new_id
from full_view_agent.domain.models import RunOutcome, RunStatus
from full_view_agent.domain.runtime_observability import (
    RuntimeAlertListResponse,
    RuntimeCapabilityMetricListResponse,
    RuntimeCursorMeta,
    RuntimeEventMetricMeta,
    RuntimeModelMetricListResponse,
    RuntimeOverviewResponse,
    RuntimeRunListResponse,
    RuntimeSessionListResponse,
    RuntimeTimelineResponse,
)
from full_view_agent.infrastructure.runtime_observability_repository import (
    runtime_observability_repository,
)


def create_runtime_observability_router(runtime: Any) -> APIRouter:
    router = APIRouter(
        prefix="/capability-api/v1/applications/{app_id}/runtime",
        tags=["Runtime Observability"],
    )
    authorizer = ControlPlaneAuthorizer.compatibility_default()
    retention = getattr(runtime.events, "_retention_seconds", None)
    if retention is None:
        retention_delta = getattr(runtime.events, "_event_retention", None)
        retention = int(retention_delta.total_seconds()) if retention_delta is not None else 3600
    event_now_candidate = getattr(runtime.events, "_now", None)

    def _event_now() -> datetime:
        value = event_now_candidate() if callable(event_now_candidate) else None
        return value if isinstance(value, datetime) else datetime.now(UTC)

    service = RuntimeObservabilityService(
        repository=runtime_observability_repository(
            runtime.store,
            runtime.events,
            model_bindings=runtime.run_model_binding_repository,
            agent_repository=runtime.agent_repository,
        ),
        event_store=runtime.events,
        runtime_started_at=runtime.started_at,
        store=runtime.store,
        capability_generation=lambda: runtime.runtime_capability_generation,
        loaded_counts=lambda: (
            len(runtime.tool_registry.list_tool_ids()),
            len(runtime.runtime_skills),
            len(runtime.runtime_workflows),
        ),
        model_binding_repository=runtime.run_model_binding_repository,
        auth_context_store=runtime.auth_contexts,
        agent_repository=runtime.agent_repository,
        capability_snapshot_store=runtime.run_capability_snapshot_store,
        event_retention_seconds=int(retention),
        capability_repository=runtime.capability_repository,
        event_now=_event_now,
    )

    async def _scope(user: CurrentUser, app_id: str) -> str:
        principal = authorizer.require(user.identity, ControlPlanePermission.CAPABILITY_READ)
        application_registry = runtime.application_registry
        if application_registry is None:
            raise ResourceNotFound("application not found")
        application = await application_registry.get_application(app_id)
        if application is None:
            raise ResourceNotFound("application not found")
        return principal.tenant_id

    def _meta() -> ResponseMeta:
        return ResponseMeta(request_id=new_id("req"))

    def _event_meta(
        requested_from: datetime,
        effective_from: datetime,
        effective_to: datetime,
        truncated: bool,
    ) -> RuntimeEventMetricMeta:
        return RuntimeEventMetricMeta(
            request_id=new_id("req"),
            event_retention_seconds=int(retention),
            requested_from=requested_from,
            requested_to=effective_to,
            effective_from=effective_from,
            effective_to=effective_to,
            truncated=truncated,
        )

    def _resource_id(*, tenant_id: str, app_id: str, resource: str, filters: str) -> str:
        return f"runtime:{tenant_id}:{app_id}:{resource}:{filters}"

    def _before(
        *,
        app_id: str,
        tenant_id: str,
        user: CurrentUser,
        resource: str,
        filters: str,
        limit: int,
        cursor: str | None,
        time_field: str,
        id_field: str,
    ) -> tuple[datetime, str] | None:
        if cursor is None:
            return None
        keyset = runtime.cursor_codec.decode_keyset(
            cursor,
            user_id=user.user_id,
            resource_id=_resource_id(
                tenant_id=tenant_id,
                app_id=app_id,
                resource=resource,
                filters=filters,
            ),
            limit=limit,
            fields=frozenset({time_field, id_field}),
        )
        try:
            timestamp = datetime.fromisoformat(keyset[time_field])
        except (KeyError, ValueError) as exc:
            raise InvalidCursor("cursor is invalid or expired") from exc
        if timestamp.tzinfo is None:
            raise InvalidCursor("cursor is invalid or expired")
        return timestamp, keyset[id_field]

    def _page(
        *,
        values: list[Any],
        app_id: str,
        tenant_id: str,
        user: CurrentUser,
        resource: str,
        filters: str,
        limit: int,
        time_field: str,
        id_field: str,
    ) -> tuple[list[Any], RuntimeCursorMeta]:
        resource_id = _resource_id(
            tenant_id=tenant_id,
            app_id=app_id,
            resource=resource,
            filters=filters,
        )
        page = values[:limit]
        has_next = len(values) > limit
        next_cursor = (
            runtime.cursor_codec.encode_keyset(
                user_id=user.user_id,
                resource_id=resource_id,
                keyset={
                    time_field: getattr(page[-1], time_field).isoformat(),
                    id_field: getattr(page[-1], id_field),
                },
                limit=limit,
            )
            if has_next
            else None
        )
        return page, RuntimeCursorMeta(
            request_id=new_id("req"), has_next=has_next, next_cursor=next_cursor
        )

    @router.get("/overview")
    async def overview(
        app_id: str,
        user: Annotated[CurrentUser, Depends(require_capability_identity)],
        window_from: Annotated[datetime | None, Query(alias="from")] = None,
        window_to: Annotated[datetime | None, Query(alias="to")] = None,
    ) -> RuntimeOverviewResponse:
        tenant_id = await _scope(user, app_id)
        return RuntimeOverviewResponse(
            data=await service.overview(
                tenant_id=tenant_id,
                app_id=app_id,
                window_from=window_from,
                window_to=window_to,
            ),
            meta=_meta(),
        )

    @router.get("/sessions")
    async def sessions(
        app_id: str,
        user: Annotated[CurrentUser, Depends(require_capability_identity)],
        status: Annotated[Literal["active", "archived"] | None, Query()] = None,
        created_from: Annotated[datetime | None, Query(alias="from")] = None,
        created_to: Annotated[datetime | None, Query(alias="to")] = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        cursor: Annotated[str | None, Query(min_length=1)] = None,
    ) -> RuntimeSessionListResponse:
        tenant_id = await _scope(user, app_id)
        filters = f"{status}:{created_from}:{created_to}"
        before = _before(
            app_id=app_id,
            tenant_id=tenant_id,
            user=user,
            resource="sessions",
            filters=filters,
            limit=limit,
            cursor=cursor,
            time_field="updated_at",
            id_field="session_id",
        )
        values = await service.list_sessions(
            tenant_id=tenant_id,
            app_id=app_id,
            status=status,
            created_from=created_from,
            created_to=created_to,
            limit=limit + 1,
            before=before,
        )
        page, meta = _page(
            values=values,
            app_id=app_id,
            tenant_id=tenant_id,
            user=user,
            resource="sessions",
            filters=filters,
            limit=limit,
            time_field="updated_at",
            id_field="session_id",
        )
        return RuntimeSessionListResponse(data=page, meta=meta)

    @router.get("/runs")
    async def runs(
        app_id: str,
        user: Annotated[CurrentUser, Depends(require_capability_identity)],
        session_id: Annotated[str | None, Query(min_length=1, max_length=128)] = None,
        status: Annotated[RunStatus | None, Query()] = None,
        outcome: Annotated[RunOutcome | None, Query()] = None,
        mode: Annotated[Literal["agent", "workflow", "analysis"] | None, Query()] = None,
        created_from: Annotated[datetime | None, Query(alias="from")] = None,
        created_to: Annotated[datetime | None, Query(alias="to")] = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        cursor: Annotated[str | None, Query(min_length=1)] = None,
    ) -> RuntimeRunListResponse:
        tenant_id = await _scope(user, app_id)
        filters = f"{session_id}:{status}:{outcome}:{mode}:{created_from}:{created_to}"
        before = _before(
            app_id=app_id,
            tenant_id=tenant_id,
            user=user,
            resource="runs",
            filters=filters,
            limit=limit,
            cursor=cursor,
            time_field="created_at",
            id_field="run_id",
        )
        values = await service.list_runs(
            tenant_id=tenant_id,
            app_id=app_id,
            session_id=session_id,
            status=status,
            outcome=outcome,
            mode=mode,
            created_from=created_from,
            created_to=created_to,
            limit=limit + 1,
            before=before,
        )
        page, meta = _page(
            values=values,
            app_id=app_id,
            tenant_id=tenant_id,
            user=user,
            resource="runs",
            filters=filters,
            limit=limit,
            time_field="created_at",
            id_field="run_id",
        )
        return RuntimeRunListResponse(data=page, meta=meta)

    @router.get("/runs/{run_id}/timeline")
    async def timeline(
        app_id: str,
        run_id: str,
        user: Annotated[CurrentUser, Depends(require_capability_identity)],
    ) -> RuntimeTimelineResponse:
        tenant_id = await _scope(user, app_id)
        return RuntimeTimelineResponse(
            data=await service.timeline(tenant_id=tenant_id, app_id=app_id, run_id=run_id),
            meta=_meta(),
        )

    @router.get("/models")
    async def models(
        app_id: str,
        user: Annotated[CurrentUser, Depends(require_capability_identity)],
        window_from: Annotated[datetime | None, Query(alias="from")] = None,
        window_to: Annotated[datetime | None, Query(alias="to")] = None,
    ) -> RuntimeModelMetricListResponse:
        tenant_id = await _scope(user, app_id)
        requested_from, effective_to, effective_from, truncated = service.event_window(
            window_from, window_to
        )
        return RuntimeModelMetricListResponse(
            data=await service.model_metrics(
                tenant_id=tenant_id,
                app_id=app_id,
                window_from=effective_from,
                window_to=effective_to,
            ),
            meta=_event_meta(requested_from, effective_from, effective_to, truncated),
        )

    @router.get("/capabilities")
    async def capabilities(
        app_id: str,
        user: Annotated[CurrentUser, Depends(require_capability_identity)],
        capability_type: Annotated[Literal["tool", "skill", "workflow"] | None, Query()] = None,
        window_from: Annotated[datetime | None, Query(alias="from")] = None,
        window_to: Annotated[datetime | None, Query(alias="to")] = None,
    ) -> RuntimeCapabilityMetricListResponse:
        tenant_id = await _scope(user, app_id)
        requested_from, effective_to, effective_from, truncated = service.event_window(
            window_from, window_to
        )
        return RuntimeCapabilityMetricListResponse(
            data=await service.capability_metrics(
                tenant_id=tenant_id,
                app_id=app_id,
                window_from=effective_from,
                window_to=effective_to,
                capability_type=capability_type,
            ),
            meta=_event_meta(requested_from, effective_from, effective_to, truncated),
        )

    @router.get("/alerts")
    async def alerts(
        app_id: str,
        user: Annotated[CurrentUser, Depends(require_capability_identity)],
        severity: Annotated[Literal["warning", "critical"] | None, Query()] = None,
        window_from: Annotated[datetime | None, Query(alias="from")] = None,
        window_to: Annotated[datetime | None, Query(alias="to")] = None,
    ) -> RuntimeAlertListResponse:
        tenant_id = await _scope(user, app_id)
        return RuntimeAlertListResponse(
            data=await service.alerts(
                tenant_id=tenant_id,
                app_id=app_id,
                window_from=window_from,
                window_to=window_to,
                severity=severity,
            ),
            meta=_meta(),
        )

    return router
