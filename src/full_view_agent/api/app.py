import base64
import binascii
import hashlib
import json
import logging
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from secrets import token_bytes
from typing import Annotated, Literal, cast

from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.security import APIKeyHeader
from pydantic import Field, SecretStr, model_validator

from full_view_agent.application.analysis_graph import AnalysisRunOutcome
from full_view_agent.application.analysis_plan_repository import AnalysisPlanRepository
from full_view_agent.application.analysis_planner import AnalysisPlanner
from full_view_agent.application.analysis_run_binding import AnalysisRunBindingStore
from full_view_agent.application.analysis_service import AnalysisPlanningService
from full_view_agent.application.auth_context_refresh import RunAuthContextRefresher
from full_view_agent.application.capability_service import ToolAdapter
from full_view_agent.application.context_builder import AgentContextBuilder
from full_view_agent.application.cursor_codec import SignedCursorCodec
from full_view_agent.application.deployment_capabilities import (
    HOUSING_NEXT_AREA_ENV,
    parse_housing_next_area_enabled,
)
from full_view_agent.application.errors import (
    AnalysisExecutionUnavailable,
    AnalysisPlanningUnavailable,
    AnalysisRequestRejected,
    ApplicationError,
    AuthenticationFailed,
    CommandClientMismatch,
    CommandReceiptConflict,
    EventHistoryExpired,
    IdempotencyConflict,
    IdentityProviderUnavailable,
    InputRequestClosed,
    InvalidAuthenticationTransport,
    InvalidCursor,
    PolicyBindingMismatch,
    ReauthenticationRequired,
    ResourceNotFound,
    ResultPayloadExpired,
    RunStateConflict,
    SessionActiveRunConflict,
    WorkflowNotAvailable,
)
from full_view_agent.application.model_planner import ModelPlannerFactory
from full_view_agent.application.model_provider import ModelProvider
from full_view_agent.application.orchestrator_factory import (
    create_analysis_orchestrator,
    create_orchestrator,
)
from full_view_agent.application.ports import (
    AgentStore,
    AnalysisOrchestratorPort,
    CredentialBroker,
    EventStore,
    IdempotencyStore,
    LegacyIdentityPort,
    OrchestrationPort,
    RunAuthContextStore,
)
from full_view_agent.application.run_admission import RunAdmissionService
from full_view_agent.application.semantic_wiring import (
    build_semantic_capability_stack,
)
from full_view_agent.application.session_run_service import SessionRunService, new_id
from full_view_agent.application.tool_registry import (
    PRODUCTION_HTTP_TOOL_IDS,
    ToolRegistry,
)
from full_view_agent.application.workflow_registry import WorkflowRegistry
from full_view_agent.domain.analysis_plan import AnalysisPlan, AnalysisRequest
from full_view_agent.domain.models import (
    AgentMessage,
    AgentRun,
    AgentSession,
    ContractModel,
    DataResult,
    EventFinishRateRow,
    Evidence,
    FrontendCommandReceipt,
    HousingAreaGroupRow,
    HousingLeaseTypeRow,
    LegacyIdentitySnapshot,
    PendingInputRequest,
    PopulationMetricRow,
    ResultMetadata,
    ResultReferenceContent,
    RunCreateRequest,
    RunInputBody,
    Steer,
    TableDataResult,
    TextContent,
)
from full_view_agent.infrastructure.analysis_plan_repository import (
    InMemoryAnalysisPlanRepository,
    PostgresAnalysisPlanRepository,
)
from full_view_agent.infrastructure.analysis_run_binding_store import (
    InMemoryAnalysisRunBindingStore,
    PostgresAnalysisRunBindingStore,
)
from full_view_agent.infrastructure.auth_context_store import (
    InMemoryRunAuthContextStore,
)
from full_view_agent.infrastructure.credential_broker import (
    EncryptedSqliteCredentialBroker,
    UnconfiguredCredentialBroker,
)
from full_view_agent.infrastructure.denial_ledger import InMemoryDenialLedger
from full_view_agent.infrastructure.event_broker import InMemoryEventBroker
from full_view_agent.infrastructure.governance_adapter import (
    HttpGovernanceAdapter,
    InMemoryGovernanceAdapter,
)
from full_view_agent.infrastructure.idempotency_store import InMemoryIdempotencyStore
from full_view_agent.infrastructure.legacy_identity import HttpLegacyIdentityAdapter
from full_view_agent.infrastructure.memory_store import InMemoryAgentStore
from full_view_agent.infrastructure.openai_compatible_model import (
    OpenAICompatibleModelProvider,
)
from full_view_agent.infrastructure.postgres_persistence import PostgresAgentPersistence
from full_view_agent.infrastructure.redis_event_notifier import RedisEventNotifier

logger = logging.getLogger(__name__)


class UnauthenticatedError(Exception):
    pass


@dataclass(frozen=True, repr=False)
class CurrentUser:
    user_id: str
    identity: LegacyIdentitySnapshot
    raw_token: SecretStr


class SessionCreateBody(ContractModel):
    title: str = Field(min_length=1, max_length=200)


class SessionUpdateBody(ContractModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    status: Literal["archived"] | None = None

    @model_validator(mode="after")
    def require_update(self) -> "SessionUpdateBody":
        if self.title is None and self.status is None:
            raise ValueError("at least one session field must be updated")
        return self


class SteerCreateBody(ContractModel):
    client_instance_id: str = Field(min_length=1, max_length=128)
    content: str = Field(min_length=1, max_length=10_000)


class ResponseMeta(ContractModel):
    request_id: str
    trace_id: str = Field(default_factory=lambda: new_id("trc"))
    idempotency_replayed: bool | None = None


class ErrorDetail(ContractModel):
    field: str
    code: str
    message: str


class ErrorPayload(ContractModel):
    code: str
    message: str
    retryable: bool = False
    details: list[ErrorDetail] = Field(default_factory=list)


class ErrorResponse(ContractModel):
    error: ErrorPayload
    meta: ResponseMeta


class SessionResponse(ContractModel):
    data: AgentSession
    meta: ResponseMeta


class RunResponse(ContractModel):
    data: AgentRun
    meta: ResponseMeta


class AnalysisPlanResponse(ContractModel):
    data: AnalysisPlan
    meta: ResponseMeta


class AnalysisExecutionBody(ContractModel):
    request_id: str = Field(min_length=1, max_length=128)


class AnalysisExecutionResponse(ContractModel):
    data: AnalysisRunOutcome
    meta: ResponseMeta


class PendingInputResponse(ContractModel):
    data: PendingInputRequest
    meta: ResponseMeta


class SteerResponse(ContractModel):
    data: Steer
    meta: ResponseMeta


class ResultResponse(ContractModel):
    data: DataResult | ResultMetadata
    meta: ResponseMeta


class CursorPageMeta(ContractModel):
    request_id: str
    trace_id: str = Field(default_factory=lambda: new_id("trc"))
    has_next: bool
    next_cursor: str | None = None


class SessionListResponse(ContractModel):
    data: list[AgentSession]
    meta: CursorPageMeta


class ResultItemsResponse(ContractModel):
    data: list[
        PopulationMetricRow
        | HousingLeaseTypeRow
        | HousingAreaGroupRow
        | EventFinishRateRow
    ]
    meta: CursorPageMeta


class MessageListResponse(ContractModel):
    data: list[AgentMessage]
    meta: CursorPageMeta


class EvidenceResponse(ContractModel):
    data: Evidence
    meta: ResponseMeta


class FrontendCommandReceiptResponse(ContractModel):
    data: FrontendCommandReceipt
    meta: ResponseMeta


class EventStreamResponse(StreamingResponse):
    media_type = "text/event-stream"


geo_token_header = APIKeyHeader(
    name="geoToken",
    scheme_name="GeoToken",
    auto_error=False,
)


def default_identity_port() -> LegacyIdentityPort:
    return HttpLegacyIdentityAdapter(
        base_url=os.getenv("FULL_VIEW_LEGACY_GATEWAY_URL", "http://127.0.0.1:9666")
    )


def default_credential_broker():
    encoded_key = os.getenv("FULL_VIEW_CREDENTIAL_KEY")
    if not encoded_key:
        return UnconfiguredCredentialBroker()
    try:
        encryption_key = base64.b64decode(
            encoded_key,
            altchars=b"-_",
            validate=True,
        )
    except (ValueError, binascii.Error) as exc:
        raise RuntimeError("FULL_VIEW_CREDENTIAL_KEY is not valid base64") from exc
    database_path = Path(
        os.getenv(
            "FULL_VIEW_CREDENTIAL_DB_PATH",
            "data/full-view-agent-credentials.db",
        )
    )
    return EncryptedSqliteCredentialBroker(
        database_path=database_path,
        encryption_key=encryption_key,
    )


def _configured_credential_key() -> bytes | None:
    encoded_key = os.getenv("FULL_VIEW_CREDENTIAL_KEY")
    if not encoded_key:
        return None
    try:
        return base64.b64decode(encoded_key, altchars=b"-_", validate=True)
    except (ValueError, binascii.Error) as exc:
        raise RuntimeError("FULL_VIEW_CREDENTIAL_KEY is not valid base64") from exc


def configured_p0_allowed_user_ids() -> set[str]:
    return {
        user_id.strip()
        for user_id in os.getenv("FULL_VIEW_P0_ALLOWED_USER_IDS", "").split(",")
        if user_id.strip()
    }


@dataclass
class RuntimeContainer:
    store: AgentStore | None = None
    events: EventStore | None = None
    idempotency: IdempotencyStore | None = None
    identity_port: LegacyIdentityPort = field(
        default_factory=default_identity_port
    )
    credentials: CredentialBroker | None = None
    auth_contexts: RunAuthContextStore | None = None
    denial_ledger: InMemoryDenialLedger = field(default_factory=InMemoryDenialLedger)
    workflow_registry: WorkflowRegistry = field(default_factory=WorkflowRegistry.default)
    persistence: PostgresAgentPersistence | None = field(default=None, init=False)
    event_notifier: RedisEventNotifier | None = None
    cursor_codec: SignedCursorCodec | None = None
    governance_adapter: ToolAdapter | None = None
    tool_registry: ToolRegistry | None = None
    model_provider: ModelProvider | None = None
    analysis_plan_repository: AnalysisPlanRepository | None = None
    analysis_binding_store: AnalysisRunBindingStore | None = None
    # Injected analysis graph orchestrator. Production composition is wired
    # once the persistent execution service lands; until then the port stays
    # unset and the execution endpoint fails closed with 503 (never falls
    # back to the legacy AnalysisPlanExecutor).
    analysis_orchestrator: AnalysisOrchestratorPort | None = None

    def __post_init__(self) -> None:
        runtime_profile = os.getenv("FULL_VIEW_RUNTIME_PROFILE", "development").lower()
        if runtime_profile not in {"development", "test", "production"}:
            raise RuntimeError(
                "FULL_VIEW_RUNTIME_PROFILE must be development, test, or production"
            )
        database_url = os.getenv("FULL_VIEW_DATABASE_URL")
        if runtime_profile == "production" and not database_url:
            raise RuntimeError("FULL_VIEW_DATABASE_URL is required in production")
        if runtime_profile == "production" and not os.getenv(
            "FULL_VIEW_CREDENTIAL_KEY"
        ):
            raise RuntimeError("FULL_VIEW_CREDENTIAL_KEY is required in production")
        if runtime_profile == "production" and not os.getenv("FULL_VIEW_CURSOR_KEY"):
            raise RuntimeError("FULL_VIEW_CURSOR_KEY is required in production")
        adapter_mode = os.getenv("FULL_VIEW_GOVERNANCE_ADAPTER", "memory").lower()
        if runtime_profile == "production" and adapter_mode != "http":
            raise RuntimeError(
                "production requires FULL_VIEW_GOVERNANCE_ADAPTER=http"
            )
        model_provider_mode = os.getenv(
            "FULL_VIEW_MODEL_PROVIDER",
            "deterministic",
        ).lower()
        if model_provider_mode not in {"deterministic", "openai_compatible"}:
            raise RuntimeError(
                "FULL_VIEW_MODEL_PROVIDER must be deterministic or openai_compatible"
            )
        if runtime_profile == "production" and model_provider_mode != (
            "openai_compatible"
        ):
            raise RuntimeError(
                "production requires FULL_VIEW_MODEL_PROVIDER=openai_compatible"
            )
        redis_url = os.getenv("FULL_VIEW_REDIS_URL")
        if database_url and redis_url and self.event_notifier is None:
            self.event_notifier = RedisEventNotifier(
                url=redis_url,
                channel_prefix=os.getenv(
                    "FULL_VIEW_REDIS_EVENT_PREFIX",
                    "full-view-agent:events",
                ),
            )
        if database_url and any(
            adapter is None
            for adapter in (
                self.store,
                self.events,
                self.idempotency,
                self.credentials,
                self.auth_contexts,
            )
        ):
            self.persistence = PostgresAgentPersistence(
                dsn=database_url,
                schema=os.getenv("FULL_VIEW_POSTGRES_SCHEMA", "full_view_agent"),
                credential_encryption_key=_configured_credential_key(),
                event_retention_seconds=int(
                    os.getenv("FULL_VIEW_EVENT_RETENTION_SECONDS", "3600")
                ),
                event_notifier=self.event_notifier,
            )
        self.store = self.store or self.persistence or InMemoryAgentStore()
        self.events = self.events or self.persistence or InMemoryEventBroker()
        self.idempotency = (
            self.idempotency or self.persistence or InMemoryIdempotencyStore()
        )
        self.credentials = (
            self.credentials or self.persistence or default_credential_broker()
        )
        self.auth_contexts = (
            self.auth_contexts or self.persistence or InMemoryRunAuthContextStore()
        )
        assert self.store is not None
        assert self.events is not None
        assert self.idempotency is not None
        assert self.credentials is not None
        assert self.auth_contexts is not None
        if self.cursor_codec is None:
            encoded_cursor_key = os.getenv("FULL_VIEW_CURSOR_KEY")
            cursor_key = (
                base64.b64decode(
                    encoded_cursor_key,
                    altchars=b"-_",
                    validate=True,
                )
                if encoded_cursor_key
                else token_bytes(32)
            )
            self.cursor_codec = SignedCursorCodec(signing_key=cursor_key)
        self.service = SessionRunService(self.store)
        self.admission = RunAdmissionService(
            credential_broker=self.credentials,
            auth_context_store=self.auth_contexts,
            p0_allowed_user_ids=configured_p0_allowed_user_ids(),
        )
        self.auth_context_refresher = RunAuthContextRefresher(
            credential_broker=self.credentials,
            identity_port=self.identity_port,
            admission=self.admission,
        )
        housing_next_area_enabled = parse_housing_next_area_enabled(
            os.getenv(HOUSING_NEXT_AREA_ENV)
        )
        if self.governance_adapter is None:
            if adapter_mode == "memory":
                self.governance_adapter = InMemoryGovernanceAdapter()
                housing_next_area_enabled = True
            elif adapter_mode == "http":
                self.governance_adapter = HttpGovernanceAdapter(
                    base_url=os.getenv(
                        "FULL_VIEW_GOVERNANCE_BASE_URL",
                        f"{os.getenv('FULL_VIEW_LEGACY_GATEWAY_URL', 'http://127.0.0.1:9666')}/geo-qxst",
                    ),
                    credential_broker=self.credentials,
                    housing_next_area_enabled=housing_next_area_enabled,
                )
            else:
                raise RuntimeError(
                    "FULL_VIEW_GOVERNANCE_ADAPTER must be 'memory' or 'http'"
                )
        if isinstance(self.governance_adapter, HttpGovernanceAdapter):
            if (
                self.governance_adapter.housing_next_area_enabled
                != housing_next_area_enabled
            ):
                raise RuntimeError(
                    "injected HttpGovernanceAdapter disagrees with "
                    f"{HOUSING_NEXT_AREA_ENV}"
                )
            if (
                self.tool_registry is not None
                and self.tool_registry.housing_next_area_enabled
                != housing_next_area_enabled
            ):
                raise RuntimeError(
                    "injected ToolRegistry disagrees with "
                    f"{HOUSING_NEXT_AREA_ENV}"
                )
            self.tool_registry = self.tool_registry or ToolRegistry.default(
                housing_next_area_enabled=housing_next_area_enabled
            )
            self.tool_registry = self.tool_registry.subset(
                set(PRODUCTION_HTTP_TOOL_IDS)
            )
        else:
            self.tool_registry = self.tool_registry or ToolRegistry.default()
        if self.model_provider is None and model_provider_mode == "openai_compatible":
            model_base_url = os.getenv("FULL_VIEW_MODEL_BASE_URL")
            model_name = os.getenv("FULL_VIEW_MODEL_NAME")
            if not model_base_url:
                raise RuntimeError(
                    "FULL_VIEW_MODEL_BASE_URL is required for openai_compatible"
                )
            if not model_name:
                raise RuntimeError(
                    "FULL_VIEW_MODEL_NAME is required for openai_compatible"
                )
            model_api_key = os.getenv("FULL_VIEW_MODEL_API_KEY")
            self.model_provider = OpenAICompatibleModelProvider(
                base_url=model_base_url,
                model=model_name,
                api_key=SecretStr(model_api_key) if model_api_key else None,
                timeout_seconds=float(
                    os.getenv("FULL_VIEW_MODEL_TIMEOUT_SECONDS", "60")
                ),
            )
        # S1-A：语义入口与既有能力栈共享一份接线（Catalog/Resolver/
        # Executor/Fingerprinter/Presenter），Native 与 LangGraph 走同一
        # Harness 包装，不改变任何既有 Tool 的行为。
        self.semantic_stack = build_semantic_capability_stack(
            registry=self.tool_registry,
            adapter=self.governance_adapter,
            auth_context_refresher=self.auth_context_refresher,
            denial_ledger=self.denial_ledger,
        )
        self.analysis_plan_repository = self.analysis_plan_repository or (
            PostgresAnalysisPlanRepository(
                dsn=database_url,
                schema=os.getenv("FULL_VIEW_POSTGRES_SCHEMA", "full_view_agent"),
            )
            if database_url
            else InMemoryAnalysisPlanRepository()
        )
        self.analysis_planner = AnalysisPlanner(self.semantic_stack.catalog)
        self.analysis_planning = AnalysisPlanningService(
            planner=self.analysis_planner,
            repository=self.analysis_plan_repository,
        )
        self.analysis_binding_store = self.analysis_binding_store or (
            PostgresAnalysisRunBindingStore(
                dsn=database_url,
                schema=os.getenv("FULL_VIEW_POSTGRES_SCHEMA", "full_view_agent"),
            )
            if database_url
            else InMemoryAnalysisRunBindingStore()
        )
        planner_factory = (
            ModelPlannerFactory(
                provider=self.model_provider,
                context_builder=AgentContextBuilder(
                    store=self.store,
                    registry=self.tool_registry,
                    semantic_presenter=self.semantic_stack.presenter,
                ),
                max_total_tokens=int(
                    os.getenv("FULL_VIEW_MODEL_TOKEN_BUDGET", "32000")
                ),
            )
            if self.model_provider is not None
            else None
        )
        self.executor: OrchestrationPort = create_orchestrator(
            service=self.service,
            store=self.store,
            events=self.events,
            auth_context_provider=self.auth_contexts,
            governance_adapter=self.governance_adapter,
            tool_registry=self.tool_registry,
            evidence_source_system=(
                "geo-qxst"
                if isinstance(self.governance_adapter, HttpGovernanceAdapter)
                else "in_memory_fixture"
            ),
            auth_context_refresher=self.auth_context_refresher,
            denial_ledger=self.denial_ledger,
            model_provider=self.model_provider,
            planner_factory=planner_factory,
            semantic_stack=self.semantic_stack,
        )
        analysis_enabled = os.getenv(
            "FULL_VIEW_ANALYSIS_EXECUTION_ENABLED",
            "true" if runtime_profile == "production" else "false",
        ).strip().lower()
        if analysis_enabled not in {"true", "false"}:
            raise RuntimeError(
                "FULL_VIEW_ANALYSIS_EXECUTION_ENABLED must be true or false"
            )
        if self.analysis_orchestrator is None and analysis_enabled == "true":
            self.analysis_orchestrator = create_analysis_orchestrator(
                service=self.service,
                store=self.store,
                events=self.events,
                planner=self.analysis_planner,
                plan_repository=self.analysis_plan_repository,
                semantic_stack=self.semantic_stack,
                tool_registry=self.tool_registry,
                evidence_source_system=(
                    "geo-qxst"
                    if isinstance(self.governance_adapter, HttpGovernanceAdapter)
                    else "in_memory_fixture"
                ),
                database_url=database_url,
                postgres_schema=os.getenv(
                    "FULL_VIEW_POSTGRES_SCHEMA", "full_view_agent"
                ),
                binding_store=self.analysis_binding_store,
            )

    def schedule_run(self, *, user_id: str, run_id: str) -> None:
        """Delegate scheduling to the OrchestrationPort."""
        self.executor.schedule(user_id=user_id, run_id=run_id)

    async def recover_runs(self) -> int:
        assert self.store is not None
        assert self.auth_contexts is not None
        assert self.analysis_binding_store is not None
        recoverable = await self.store.list_recoverable_runs()
        recovered_count = 0
        for user_id, run in recoverable:
            if run.mode != "analysis" and run.status in {"queued", "running"}:
                self.schedule_run(user_id=user_id, run_id=run.run_id)
                recovered_count += 1
                continue
            if run.mode != "analysis" or run.status == "queued":
                continue
            try:
                auth_context = await self.auth_contexts.get(
                    user_id=user_id,
                    run_id=run.run_id,
                )
                binding = await self.analysis_binding_store.get_binding_for_run(
                    tenant_id=auth_context.principal.tenant_id,
                    user_id=user_id,
                    run_id=run.run_id,
                )
                if binding.status in {"completed", "partial", "failed"}:
                    await _complete_analysis_run(
                        runtime=self,
                        user_id=user_id,
                        run=run,
                        outcome=AnalysisRunOutcome(
                            analysis_run_id=run.run_id,
                            plan_id=binding.plan_id,
                            request_id=binding.request_id,
                            status=cast(
                                Literal["completed", "partial", "failed"],
                                binding.status,
                            ),
                            reason_code=f"ANALYSIS_{binding.status.upper()}",
                            report_result_id=binding.report_result_id,
                        ),
                    )
                else:
                    await _publish_analysis_reauthentication(
                        runtime=self,
                        user_id=user_id,
                        run_id=run.run_id,
                        plan_id=binding.plan_id,
                        request_id=binding.request_id,
                    )
                recovered_count += 1
            except ResourceNotFound:
                logger.warning(
                    "analysis run could not expose a resumable descriptor",
                    extra={"run_id": run.run_id},
                )
        return recovered_count


async def require_geotoken(
    request: Request,
    geo_token: Annotated[str | None, Depends(geo_token_header)],
    authorization: Annotated[str | None, Header()] = None,
) -> CurrentUser:
    if any(key.casefold() == "geotoken" for key in request.query_params):
        raise InvalidAuthenticationTransport("geoToken must not be sent in the URL")
    if geo_token and authorization and authorization.casefold().startswith("bearer "):
        raise InvalidAuthenticationTransport(
            "geoToken and Bearer authentication cannot be used together"
        )
    if not geo_token:
        raise UnauthenticatedError
    raw_token = SecretStr(geo_token)
    identity = await request.app.state.runtime.identity_port.resolve(raw_token)
    return CurrentUser(
        user_id=identity.principal.user_id,
        identity=identity,
        raw_token=raw_token,
    )


def request_fingerprint(*, domain: str, payload: dict[str, object]) -> str:
    canonical = json.dumps(
        {"domain": domain, "payload": payload},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


async def _complete_analysis_run(
    *,
    runtime: RuntimeContainer,
    user_id: str,
    run: AgentRun,
    outcome: AnalysisRunOutcome,
) -> None:
    """Persist the user-visible message and own the dedicated Run terminal state."""
    assert runtime.store is not None
    assert runtime.events is not None
    current = await runtime.store.get_run(user_id=user_id, run_id=run.run_id)
    messages = await runtime.store.list_messages(
        user_id=user_id, session_id=run.session_id
    )
    result_message = next(
        (
            message
            for message in messages
            if message.run_id == run.run_id and message.role == "assistant"
        ),
        None,
    )
    if result_message is None and current.status not in {
        "completed",
        "failed",
        "cancelled",
        "expired",
    }:
        content: list[TextContent | ResultReferenceContent] = [
            TextContent(type="text", text=_analysis_outcome_text(outcome))
        ]
        evidence_ids: list[str] = []
        if outcome.report_result_id is not None:
            content.append(
                ResultReferenceContent(
                    type="result_reference",
                    result_id=outcome.report_result_id,
                    label="区域研判报告",
                )
            )
        result_message = AgentMessage(
            message_id=f"msg_analysis_{run.run_id}",
            session_id=run.session_id,
            run_id=run.run_id,
            role="assistant",
            content=content,
            evidence_ids=evidence_ids,
            created_at=run.started_at or run.created_at,
        )
        result_message = await runtime.store.save_message(
            user_id=user_id, run_id=run.run_id, message=result_message
        )
    if result_message is not None:
        await runtime.events.publish(
            event_type="assistant.message.completed",
            session_id=run.session_id,
            run_id=run.run_id,
            data={"message": result_message.model_dump(mode="json")},
            idempotency_key=f"analysis:{run.run_id}:assistant",
        )
    # Another worker may have reached terminal while this worker was saving
    # the deterministic message. Always decide from fresh durable state.
    current = await runtime.store.get_run(user_id=user_id, run_id=run.run_id)
    if current.status not in {"completed", "failed", "cancelled", "expired"}:
        try:
            if outcome.status == "failed":
                current = await runtime.service.fail_run(
                    user_id=user_id,
                    run_id=run.run_id,
                    completion_reason_code=outcome.reason_code,
                )
            else:
                current = await runtime.service.complete_run(
                    user_id=user_id,
                    run_id=run.run_id,
                    outcome="success" if outcome.status == "completed" else "partial",
                    completion_reason_code=outcome.reason_code,
                )
        except RunStateConflict:
            current = await runtime.store.get_run(
                user_id=user_id, run_id=run.run_id
            )
    if not _analysis_terminal_matches(current, outcome):
        raise RunStateConflict("analysis terminal state conflicts with its outcome")
    await runtime.events.publish(
        event_type="run.completed" if current.status == "completed" else "run.failed",
        session_id=current.session_id,
        run_id=current.run_id,
        data={
            "status": current.status,
            "outcome": current.outcome,
            "completion_reason_code": current.completion_reason_code,
            "result_message_id": (
                result_message.message_id if result_message is not None else None
            ),
        },
        idempotency_key=f"analysis:{run.run_id}:terminal",
    )


def _analysis_outcome_text(outcome: AnalysisRunOutcome) -> str:
    if outcome.status == "completed":
        return "区域研判已完成，详细结果请查看研判报告。"
    if outcome.status == "partial":
        return "区域研判已完成，但部分主题未取得结果；详情请查看研判报告。"
    return "区域研判执行失败，未生成可用报告。"


def _analysis_terminal_matches(
    run: AgentRun, outcome: AnalysisRunOutcome
) -> bool:
    expected_status = "failed" if outcome.status == "failed" else "completed"
    expected_outcome = (
        "failed"
        if outcome.status == "failed"
        else "success" if outcome.status == "completed" else "partial"
    )
    return (
        run.status == expected_status
        and run.outcome == expected_outcome
        and run.completion_reason_code == outcome.reason_code
    )


async def _publish_analysis_reauthentication(
    *,
    runtime: RuntimeContainer,
    user_id: str,
    run_id: str,
    plan_id: str,
    request_id: str,
) -> None:
    """Expose a durable graph interrupt through the public event contract."""
    assert runtime.events is not None
    waiting, pending = await runtime.service.wait_for_reauthentication(
        user_id=user_id,
        run_id=run_id,
        analysis_plan_id=plan_id,
        analysis_request_id=request_id,
    )
    event_data = {
        "status": waiting.status,
        "waiting_for": waiting.waiting_for,
        "input_request_id": pending.input_request_id,
        "kind": pending.kind,
        "prompt": pending.prompt,
        "options": [option.model_dump(mode="json") for option in pending.options],
        "allow_free_text": pending.allow_free_text,
        "run_state_version": pending.run_state_version,
        "expires_at": pending.expires_at.isoformat(),
        "analysis_plan_id": pending.analysis_plan_id,
        "analysis_request_id": pending.analysis_request_id,
    }
    for event_type in ("run.waiting", "input.required", "reauth_required"):
        await runtime.events.publish(
            event_type=event_type,
            session_id=waiting.session_id,
            run_id=waiting.run_id,
            data=event_data,
            idempotency_key=(
                f"analysis:{run_id}:reauth:{pending.input_request_id}:{event_type}"
            ),
        )


def create_app(runtime: RuntimeContainer | None = None) -> FastAPI:
    runtime = runtime or RuntimeContainer()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        await runtime.recover_runs()
        yield
        await runtime.executor.shutdown()
        close_governance_adapter = getattr(runtime.governance_adapter, "aclose", None)
        if close_governance_adapter is not None:
            await close_governance_adapter()
        if runtime.event_notifier is not None:
            await runtime.event_notifier.close()

    app = FastAPI(
        title="Full Information View Agent API",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.runtime = runtime

    def error_response(
        *,
        status_code: int,
        code: str,
        message: str,
        retryable: bool = False,
        details: list[dict[str, str]] | None = None,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=status_code,
            content={
                "error": {
                    "code": code,
                    "message": message,
                    "retryable": retryable,
                    "details": details or [],
                },
                "meta": {
                    "request_id": new_id("req"),
                    "trace_id": new_id("trc"),
                },
            },
        )

    @app.exception_handler(UnauthenticatedError)
    async def unauthenticated_handler(_request, _exc) -> JSONResponse:
        return error_response(
            status_code=401,
            code="unauthenticated",
            message="需要登录后访问",
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        details: list[dict[str, str]] = []
        invalid_json = False
        for item in exc.errors():
            code = str(item.get("type", "validation_error"))
            invalid_json = invalid_json or code == "json_invalid"
            location = item.get("loc", ())
            field = ".".join(
                str(part) for part in location if part not in {"body", "query", "path"}
            )
            details.append(
                {
                    "field": field or "request",
                    "code": code,
                    "message": str(item.get("msg", "Invalid request")),
                }
            )
        return error_response(
            status_code=422,
            code="invalid_json" if invalid_json else "validation_error",
            message="请求参数不符合契约",
            details=details,
        )

    @app.exception_handler(ApplicationError)
    async def application_error_handler(_request, exc: ApplicationError) -> JSONResponse:
        status_code = 500
        retryable = False
        details: list[dict[str, str]] = []
        if isinstance(exc, (EventHistoryExpired, ResultPayloadExpired)):
            status_code = 410
        elif isinstance(exc, ResourceNotFound):
            status_code = 404
        elif isinstance(exc, CommandClientMismatch):
            status_code = 403
        elif isinstance(exc, SessionActiveRunConflict):
            status_code = 409
            details = [
                {
                    "field": "session.active_run_id",
                    "code": "active_run_exists",
                    "message": exc.active_run_id,
                }
            ]
        elif isinstance(
            exc,
            (
                IdempotencyConflict,
                CommandReceiptConflict,
                InputRequestClosed,
                RunStateConflict,
                PolicyBindingMismatch,
                ReauthenticationRequired,
            ),
        ):
            status_code = 409
        elif isinstance(exc, InvalidAuthenticationTransport):
            status_code = 400
        elif isinstance(exc, AuthenticationFailed):
            status_code = 401
        elif isinstance(
            exc,
            (
                IdentityProviderUnavailable,
                AnalysisPlanningUnavailable,
                AnalysisExecutionUnavailable,
            ),
        ):
            status_code = 503
            retryable = True
        elif isinstance(
            exc,
            (AnalysisRequestRejected, WorkflowNotAvailable, InvalidCursor),
        ):
            status_code = 422
        return error_response(
            status_code=status_code,
            code=exc.code,
            message=str(exc),
            retryable=retryable,
            details=details,
        )

    @app.exception_handler(Exception)
    async def unexpected_error_handler(_request: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled API error", exc_info=exc)
        return error_response(
            status_code=500,
            code="internal_error",
            message="服务暂时不可用",
        )

    @app.get("/health/live")
    async def liveness() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready")
    async def readiness() -> JSONResponse:
        checks: dict[str, str] = {}
        store = getattr(app.state, "runtime", None) and app.state.runtime.store
        if store is None:
            checks["store"] = "missing"
        else:
            try:
                await store.health_check()
                checks["store"] = "ok"
            except Exception as exc:
                checks["store"] = f"error: {exc}"
        event_broker = getattr(app.state, "runtime", None) and getattr(
            app.state.runtime, "events", None
        )
        if event_broker is not None and hasattr(event_broker, "health_check"):
            try:
                await event_broker.health_check()
                checks["event_broker"] = "ok"
            except Exception as exc:
                checks["event_broker"] = f"error: {exc}"
        healthy = all(value == "ok" for value in checks.values())
        return JSONResponse(
            status_code=200 if healthy else 503,
            content={"status": "ok" if healthy else "degraded", "checks": checks},
        )

    @app.post("/agent-api/v1/sessions", status_code=201)
    async def create_session(
        body: SessionCreateBody,
        user: Annotated[CurrentUser, Depends(require_geotoken)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1)],
    ) -> SessionResponse:
        async def operation():
            return await app.state.runtime.service.create_session(
                user_id=user.user_id,
                title=body.title,
            )

        session, replayed = await app.state.runtime.idempotency.execute(
            user_id=user.user_id,
            scope="sessions:create",
            key=idempotency_key,
            request_fingerprint=request_fingerprint(
                domain="sessions:create",
                payload=body.model_dump(mode="json"),
            ),
            operation=operation,
        )
        return SessionResponse(
            data=session,
            meta=ResponseMeta(
                request_id=new_id("req"),
                idempotency_replayed=replayed,
            ),
        )

    @app.get("/agent-api/v1/sessions")
    async def list_sessions(
        user: Annotated[CurrentUser, Depends(require_geotoken)],
        status: Annotated[Literal["active", "archived"] | None, Query()] = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        cursor: Annotated[str | None, Query(min_length=1)] = None,
    ) -> SessionListResponse:
        sessions = await app.state.runtime.service.list_sessions(
            user_id=user.user_id,
            status=status,
        )
        resource_id = f"sessions:status:{status or 'all'}"
        offset = (
            app.state.runtime.cursor_codec.decode(
                cursor,
                user_id=user.user_id,
                resource_id=resource_id,
                limit=limit,
            )
            if cursor is not None
            else 0
        )
        page = sessions[offset : offset + limit]
        next_offset = offset + len(page)
        has_next = next_offset < len(sessions)
        next_cursor = (
            app.state.runtime.cursor_codec.encode(
                user_id=user.user_id,
                resource_id=resource_id,
                offset=next_offset,
                limit=limit,
            )
            if has_next
            else None
        )
        return SessionListResponse(
            data=page,
            meta=CursorPageMeta(
                request_id=new_id("req"),
                has_next=has_next,
                next_cursor=next_cursor,
            ),
        )

    @app.get("/agent-api/v1/sessions/{session_id}")
    async def get_session(
        session_id: str,
        user: Annotated[CurrentUser, Depends(require_geotoken)],
    ) -> SessionResponse:
        session = await app.state.runtime.service.get_session(
            user_id=user.user_id,
            session_id=session_id,
        )
        return SessionResponse(
            data=session,
            meta=ResponseMeta(request_id=new_id("req")),
        )

    @app.patch("/agent-api/v1/sessions/{session_id}")
    async def update_session(
        session_id: str,
        body: SessionUpdateBody,
        user: Annotated[CurrentUser, Depends(require_geotoken)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1)],
    ) -> SessionResponse:
        scope = f"sessions:{session_id}:update"

        async def operation():
            return await app.state.runtime.service.update_session(
                user_id=user.user_id,
                session_id=session_id,
                title=body.title,
                status=body.status,
            )

        session, replayed = await app.state.runtime.idempotency.execute(
            user_id=user.user_id,
            scope=scope,
            key=idempotency_key,
            request_fingerprint=request_fingerprint(
                domain=scope,
                payload=body.model_dump(mode="json"),
            ),
            operation=operation,
        )
        return SessionResponse(
            data=session,
            meta=ResponseMeta(
                request_id=new_id("req"),
                idempotency_replayed=replayed,
            ),
        )

    @app.post("/agent-api/v1/sessions/{session_id}/runs", status_code=202)
    async def create_run(
        session_id: str,
        body: RunCreateRequest,
        user: Annotated[CurrentUser, Depends(require_geotoken)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1)],
    ) -> RunResponse:
        if body.mode == "workflow" and body.workflow_ref is not None:
            app.state.runtime.workflow_registry.require_available(
                workflow_ref=body.workflow_ref,
                identity=user.identity,
            )

        natural_replayed = False

        async def operation():
            nonlocal natural_replayed

            async def create_from_message():
                run = await app.state.runtime.service.create_run(
                    user_id=user.user_id,
                    session_id=session_id,
                    request=body,
                )
                try:
                    await app.state.runtime.admission.admit(
                        identity=user.identity,
                        raw_token=user.raw_token,
                        session_id=session_id,
                        run_id=run.run_id,
                    )
                except Exception:
                    try:
                        await app.state.runtime.service.start_run(
                            user_id=user.user_id,
                            run_id=run.run_id,
                        )
                        await app.state.runtime.service.fail_run(
                            user_id=user.user_id,
                            run_id=run.run_id,
                            completion_reason_code="run_admission_failed",
                        )
                    except Exception:
                        logger.exception(
                            "failed to compensate rejected run admission",
                            extra={"run_id": run.run_id},
                        )
                    raise
                return run

            run, natural_replayed = await app.state.runtime.idempotency.execute(
                user_id=user.user_id,
                scope=f"sessions:{session_id}:client-messages",
                key=body.input.client_message_id,
                request_fingerprint=request_fingerprint(
                    domain=f"sessions:{session_id}:client-messages",
                    payload=body.model_dump(mode="json"),
                ),
                operation=create_from_message,
            )
            return run

        scope = f"sessions:{session_id}:runs:create"
        run, replayed = await app.state.runtime.idempotency.execute(
            user_id=user.user_id,
            scope=scope,
            key=idempotency_key,
            request_fingerprint=request_fingerprint(
                domain=scope,
                payload=body.model_dump(mode="json"),
            ),
            operation=operation,
        )
        effective_replay = replayed or natural_replayed
        if not effective_replay and run.mode != "analysis":
            app.state.runtime.schedule_run(user_id=user.user_id, run_id=run.run_id)
        return RunResponse(
            data=run,
            meta=ResponseMeta(
                request_id=new_id("req"),
                idempotency_replayed=effective_replay,
            ),
        )

    @app.get("/agent-api/v1/sessions/{session_id}/messages")
    async def list_session_messages(
        session_id: str,
        user: Annotated[CurrentUser, Depends(require_geotoken)],
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        cursor: Annotated[str | None, Query(min_length=1)] = None,
    ) -> MessageListResponse:
        messages = await app.state.runtime.store.list_messages(
            user_id=user.user_id,
            session_id=session_id,
        )
        resource_id = f"session:{session_id}:messages"
        offset = (
            app.state.runtime.cursor_codec.decode(
                cursor,
                user_id=user.user_id,
                resource_id=resource_id,
                limit=limit,
            )
            if cursor is not None
            else 0
        )
        page = messages[offset : offset + limit]
        next_offset = offset + len(page)
        has_next = next_offset < len(messages)
        next_cursor = (
            app.state.runtime.cursor_codec.encode(
                user_id=user.user_id,
                resource_id=resource_id,
                offset=next_offset,
                limit=limit,
            )
            if has_next
            else None
        )
        return MessageListResponse(
            data=page,
            meta=CursorPageMeta(
                request_id=new_id("req"),
                has_next=has_next,
                next_cursor=next_cursor,
            ),
        )

    @app.get("/agent-api/v1/runs/{run_id}")
    async def get_run(
        run_id: str,
        user: Annotated[CurrentUser, Depends(require_geotoken)],
    ) -> RunResponse:
        run = await app.state.runtime.store.get_run(user_id=user.user_id, run_id=run_id)
        return RunResponse(
            data=run,
            meta=ResponseMeta(request_id=new_id("req")),
        )

    @app.get("/agent-api/v1/runs/{run_id}/pending-input")
    async def get_pending_run_input(
        run_id: str,
        user: Annotated[CurrentUser, Depends(require_geotoken)],
    ) -> PendingInputResponse:
        run = await app.state.runtime.store.get_run(
            user_id=user.user_id,
            run_id=run_id,
        )
        pending = await app.state.runtime.service.get_pending_input(
            user_id=user.user_id,
            run_id=run_id,
        )
        if (
            run.status != "waiting_input"
            or run.waiting_for != "reauth"
            or pending.closed_at is not None
        ):
            raise ResourceNotFound("pending input request not found")
        if pending.expires_at <= datetime.now(UTC):
            _waiting, pending = (
                await app.state.runtime.service.wait_for_reauthentication(
                    user_id=user.user_id,
                    run_id=run_id,
                    analysis_plan_id=pending.analysis_plan_id,
                    analysis_request_id=pending.analysis_request_id,
                )
            )
        return PendingInputResponse(
            data=pending,
            meta=ResponseMeta(request_id=new_id("req")),
        )

    @app.post(
        "/agent-api/v1/runs/{run_id}/analysis-plans",
        status_code=201,
        responses={
            400: {"model": ErrorResponse},
            401: {"model": ErrorResponse},
            404: {"model": ErrorResponse},
            409: {"model": ErrorResponse},
            422: {"model": ErrorResponse},
            503: {"model": ErrorResponse},
        },
    )
    async def create_analysis_plan(
        run_id: str,
        body: AnalysisRequest,
        user: Annotated[CurrentUser, Depends(require_geotoken)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1)],
    ) -> AnalysisPlanResponse:
        # Current identity and authorization are security preconditions, not
        # part of the cacheable operation. A replay must never bypass tenant
        # ownership checks or return a plan after permissions were revoked.
        run = await app.state.runtime.store.get_run(
            user_id=user.user_id,
            run_id=run_id,
        )
        if run.mode != "analysis":
            raise RunStateConflict(
                "analysis execution requires a run created with mode=analysis"
            )
        previous_context = await app.state.runtime.auth_contexts.get(
            user_id=user.user_id,
            run_id=run_id,
        )
        if (
            previous_context.principal.user_id != user.identity.principal.user_id
            or previous_context.principal.tenant_id
            != user.identity.principal.tenant_id
        ):
            raise ResourceNotFound("run was not found for the current identity")
        auth_context = await app.state.runtime.admission.admit(
            identity=user.identity,
            raw_token=user.raw_token,
            session_id=run.session_id,
            run_id=run_id,
        )
        try:
            await app.state.runtime.credentials.revoke(
                credential_ref=previous_context.credential_ref,
            )
        except Exception:
            await app.state.runtime.credentials.revoke(
                credential_ref=auth_context.credential_ref,
            )
            raise

        async def operation() -> AnalysisPlan:
            return await app.state.runtime.analysis_planning.create_plan(
                request=body,
                auth_context=auth_context,
            )

        scope = f"runs:{run_id}:analysis-plans:create"
        plan, replayed = await app.state.runtime.idempotency.execute(
            user_id=user.user_id,
            scope=scope,
            key=idempotency_key,
            request_fingerprint=request_fingerprint(
                domain=scope,
                payload={
                    "request": body.model_dump(mode="json"),
                    # Stable policy view only: never include credential refs,
                    # tokens, auth-context IDs or timestamps.
                    "authorization": {
                        "principal": auth_context.principal.model_dump(mode="json"),
                        "application": auth_context.application.model_dump(mode="json"),
                        "entitlements": sorted(auth_context.entitlements),
                        "data_scopes": auth_context.data_scopes.model_dump(mode="json"),
                        "purpose": auth_context.purpose,
                        "policy_version": auth_context.policy_version,
                    },
                },
            ),
            operation=operation,
        )
        return AnalysisPlanResponse(
            data=plan,
            meta=ResponseMeta(
                request_id=new_id("req"),
                idempotency_replayed=replayed,
            ),
        )

    @app.get(
        "/agent-api/v1/runs/{run_id}/analysis-plans/current",
        responses={
            401: {"model": ErrorResponse},
            404: {"model": ErrorResponse},
            409: {"model": ErrorResponse},
            503: {"model": ErrorResponse},
        },
    )
    async def get_current_analysis_plan(
        run_id: str,
        user: Annotated[CurrentUser, Depends(require_geotoken)],
    ) -> AnalysisPlanResponse:
        run = await app.state.runtime.store.get_run(
            user_id=user.user_id,
            run_id=run_id,
        )
        if run.mode != "analysis" or run.status != "queued":
            raise RunStateConflict(
                "current analysis plan is discoverable only for a queued analysis run"
            )
        auth_context = await app.state.runtime.auth_contexts.get(
            user_id=user.user_id,
            run_id=run_id,
        )
        if (
            auth_context.principal.user_id != user.identity.principal.user_id
            or auth_context.principal.tenant_id
            != user.identity.principal.tenant_id
        ):
            raise ResourceNotFound("run was not found for the current identity")
        plan = await app.state.runtime.analysis_plan_repository.get_latest_for_run(
            tenant_id=auth_context.principal.tenant_id,
            user_id=user.user_id,
            run_id=run_id,
        )
        if plan is None:
            raise ResourceNotFound("analysis plan was not found for this run")
        return AnalysisPlanResponse(
            data=plan,
            meta=ResponseMeta(request_id=new_id("req")),
        )

    @app.post(
        "/agent-api/v1/runs/{run_id}/analysis-plans/{plan_id}/executions",
        responses={
            400: {"model": ErrorResponse},
            401: {"model": ErrorResponse},
            404: {"model": ErrorResponse},
            409: {"model": ErrorResponse},
            422: {"model": ErrorResponse},
            503: {"model": ErrorResponse},
        },
    )
    async def execute_analysis_plan(
        run_id: str,
        plan_id: str,
        body: AnalysisExecutionBody,
        user: Annotated[CurrentUser, Depends(require_geotoken)],
    ) -> AnalysisExecutionResponse:
        # Fail closed before touching any state: a missing orchestrator is a
        # deployment condition, never a reason to fall back to ad-hoc
        # execution (the legacy AnalysisPlanExecutor stays unwired here).
        if app.state.runtime.analysis_orchestrator is None:
            raise AnalysisExecutionUnavailable(
                "analysis execution orchestrator is not configured"
            )
        run = await app.state.runtime.store.get_run(
            user_id=user.user_id,
            run_id=run_id,
        )
        if run.mode != "analysis":
            raise RunStateConflict(
                "analysis execution requires a run created with mode=analysis"
            )
        previous_context = await app.state.runtime.auth_contexts.get(
            user_id=user.user_id,
            run_id=run_id,
        )
        if (
            previous_context.principal.user_id != user.identity.principal.user_id
            or previous_context.principal.tenant_id
            != user.identity.principal.tenant_id
        ):
            raise ResourceNotFound("run was not found for the current identity")
        # The plan is always loaded from the server-side authority keyed by
        # tenant/user/run; clients can only reference it, never upload or
        # override plan, execution, or result payloads.
        plan = await app.state.runtime.analysis_plan_repository.get(
            tenant_id=user.identity.principal.tenant_id,
            user_id=user.user_id,
            run_id=run_id,
            plan_id=plan_id,
        )
        if plan is None:
            raise ResourceNotFound("analysis plan was not found for this run")
        if plan.request_id != body.request_id:
            raise RunStateConflict(
                "execution request id does not match the stored analysis plan"
            )
        auth_context = await app.state.runtime.admission.admit(
            identity=user.identity,
            raw_token=user.raw_token,
            session_id=run.session_id,
            run_id=run_id,
        )
        try:
            await app.state.runtime.credentials.revoke(
                credential_ref=previous_context.credential_ref,
            )
        except Exception:
            await app.state.runtime.credentials.revoke(
                credential_ref=auth_context.credential_ref,
            )
            raise
        if run.status == "queued":
            try:
                run = await app.state.runtime.service.start_run(
                    user_id=user.user_id, run_id=run_id
                )
            except RunStateConflict:
                # Another worker may have won the queued -> running transition
                # after our initial read. Re-read the authority store and only
                # join a state that the idempotent analysis orchestrator can
                # safely replay; every other conflict remains fail-closed.
                run = await app.state.runtime.store.get_run(
                    user_id=user.user_id,
                    run_id=run_id,
                )
                if run.status not in {
                    "running",
                    "waiting_input",
                    "completed",
                    "failed",
                }:
                    raise
        elif run.status not in {
            "running",
            "waiting_input",
            "completed",
            "failed",
        }:
            raise RunStateConflict("analysis run is not executable")
        try:
            outcome = await app.state.runtime.analysis_orchestrator.run(
                user_id=user.user_id,
                session_id=run.session_id,
                analysis_run_id=run_id,
                plan_id=plan_id,
                request_id=body.request_id,
                auth_context=auth_context,
            )
        except ReauthenticationRequired:
            await _publish_analysis_reauthentication(
                runtime=app.state.runtime,
                user_id=user.user_id,
                run_id=run_id,
                plan_id=plan_id,
                request_id=body.request_id,
            )
            raise
        except Exception:
            current = await app.state.runtime.store.get_run(
                user_id=user.user_id, run_id=run_id
            )
            if current.status in {"queued", "running"}:
                failed = await app.state.runtime.service.fail_run(
                    user_id=user.user_id,
                    run_id=run_id,
                    completion_reason_code="analysis_execution_failed",
                )
                await app.state.runtime.events.publish(
                    event_type="run.failed",
                    session_id=failed.session_id,
                    run_id=failed.run_id,
                    data={
                        "status": failed.status,
                        "outcome": failed.outcome,
                        "completion_reason_code": failed.completion_reason_code,
                    },
                    idempotency_key=f"analysis:{run_id}:terminal",
                )
            raise
        await _complete_analysis_run(
            runtime=app.state.runtime,
            user_id=user.user_id,
            run=run,
            outcome=outcome,
        )
        return AnalysisExecutionResponse(
            data=outcome,
            meta=ResponseMeta(request_id=new_id("req")),
        )

    @app.post("/agent-api/v1/runs/{run_id}/cancel", status_code=202)
    async def cancel_run(
        run_id: str,
        user: Annotated[CurrentUser, Depends(require_geotoken)],
    ) -> RunResponse:
        async def operation():
            await app.state.runtime.executor.cancel(
                user_id=user.user_id, run_id=run_id,
            )
            return await app.state.runtime.store.get_run(
                user_id=user.user_id, run_id=run_id,
            )

        scope = f"runs:{run_id}:cancel"
        run, replayed = await app.state.runtime.idempotency.execute(
            user_id=user.user_id,
            scope=scope,
            key="natural-cancel",
            request_fingerprint=request_fingerprint(domain=scope, payload={}),
            operation=operation,
        )
        return RunResponse(
            data=run,
            meta=ResponseMeta(
                request_id=new_id("req"),
                idempotency_replayed=replayed,
            ),
        )

    @app.post("/agent-api/v1/runs/{run_id}/inputs", status_code=202)
    async def submit_run_input(
        run_id: str,
        body: RunInputBody,
        user: Annotated[CurrentUser, Depends(require_geotoken)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1)],
    ) -> RunResponse:
        async def operation():
            if body.response.type != "reauthenticated":
                raise RunStateConflict(
                    "the pending reauthentication request requires a reauthenticated response"
                )
            current = await app.state.runtime.store.get_run(
                user_id=user.user_id,
                run_id=run_id,
            )
            previous_context = await app.state.runtime.auth_contexts.get(
                user_id=user.user_id,
                run_id=run_id,
            )
            try:
                auth_context = await app.state.runtime.admission.admit(
                    identity=user.identity,
                    raw_token=user.raw_token,
                    session_id=current.session_id,
                    run_id=run_id,
                )
            except Exception:
                waiting, pending = (
                    await app.state.runtime.service.wait_for_reauthentication(
                        user_id=user.user_id,
                        run_id=run_id,
                    )
                )
                event_data = {
                        "input_request_id": pending.input_request_id,
                        "kind": pending.kind,
                        "prompt": pending.prompt,
                        "options": [
                            option.model_dump(mode="json") for option in pending.options
                        ],
                        "allow_free_text": pending.allow_free_text,
                        "run_state_version": pending.run_state_version,
                        "expires_at": pending.expires_at.isoformat(),
                    }
                for event_type in ("input.required", "reauth_required"):
                    await app.state.runtime.events.publish(
                        event_type=event_type,
                        session_id=waiting.session_id,
                        run_id=waiting.run_id,
                        data=event_data,
                    )
                raise
            try:
                await app.state.runtime.credentials.revoke(
                    credential_ref=previous_context.credential_ref,
                )
            except Exception:
                await app.state.runtime.credentials.revoke(
                    credential_ref=auth_context.credential_ref,
                )
                raise
            if current.mode == "analysis":
                if app.state.runtime.analysis_orchestrator is None:
                    raise AnalysisExecutionUnavailable(
                        "analysis execution orchestrator is not configured"
                    )
                pending = await app.state.runtime.service.get_pending_input(
                    user_id=user.user_id,
                    run_id=run_id,
                )
                plan_id = pending.analysis_plan_id or body.analysis_plan_id
                request_id = pending.analysis_request_id or body.analysis_request_id
                if plan_id is None or request_id is None:
                    raise RunStateConflict(
                        "analysis resume requires trusted plan references"
                    )
                if (
                    body.analysis_plan_id is not None
                    and body.analysis_plan_id != plan_id
                ) or (
                    body.analysis_request_id is not None
                    and body.analysis_request_id != request_id
                ):
                    raise RunStateConflict(
                        "analysis resume references differ from the pending request"
                    )
                plan = await app.state.runtime.analysis_plan_repository.get(
                    tenant_id=user.identity.principal.tenant_id,
                    user_id=user.user_id,
                    run_id=run_id,
                    plan_id=plan_id,
                )
                if plan is None:
                    raise ResourceNotFound(
                        "analysis plan was not found for this run"
                    )
                if plan.request_id != request_id:
                    raise RunStateConflict(
                        "analysis resume request id does not match the stored plan"
                    )
                # Validate and atomically consume the exact durable pending
                # token before recording the audit fact. The graph repeats
                # this operation idempotently when it applies Command(resume).
                accepted = await app.state.runtime.service.resume_from_input(
                    user_id=user.user_id,
                    run_id=run_id,
                    input_request_id=body.input_request_id,
                    run_state_version=body.run_state_version,
                )
                await app.state.runtime.events.publish(
                    event_type="input.received",
                    session_id=accepted.session_id,
                    run_id=accepted.run_id,
                    data={
                        "input_request_id": body.input_request_id,
                        "kind": "reauth",
                        "client_instance_id": body.client_instance_id,
                        "run_state_version": body.run_state_version,
                    },
                    idempotency_key=(
                        f"analysis:{run_id}:input:{body.input_request_id}:received"
                    ),
                )
                try:
                    outcome = await app.state.runtime.analysis_orchestrator.resume(
                        user_id=user.user_id,
                        session_id=current.session_id,
                        analysis_run_id=run_id,
                        plan_id=plan_id,
                        request_id=request_id,
                        input_request_id=body.input_request_id,
                        run_state_version=body.run_state_version,
                        auth_context=auth_context,
                    )
                except ReauthenticationRequired:
                    await _publish_analysis_reauthentication(
                        runtime=app.state.runtime,
                        user_id=user.user_id,
                        run_id=run_id,
                        plan_id=plan_id,
                        request_id=request_id,
                    )
                    raise
                await _complete_analysis_run(
                    runtime=app.state.runtime,
                    user_id=user.user_id,
                    run=current,
                    outcome=outcome,
                )
            else:
                # General orchestrator owns resume_from_input + scheduling.
                await app.state.runtime.executor.resume(
                    user_id=user.user_id,
                    run_id=run_id,
                    input_request_id=body.input_request_id,
                    run_state_version=body.run_state_version,
                )
            resumed = await app.state.runtime.store.get_run(
                user_id=user.user_id, run_id=run_id,
            )
            if current.mode != "analysis":
                await app.state.runtime.events.publish(
                    event_type="input.received",
                    session_id=resumed.session_id,
                    run_id=resumed.run_id,
                    data={
                        "input_request_id": body.input_request_id,
                        "kind": "reauth",
                        "client_instance_id": body.client_instance_id,
                        "run_state_version": body.run_state_version,
                    },
                )
            return resumed

        scope = f"runs:{run_id}:inputs:create"
        resumed, replayed = await app.state.runtime.idempotency.execute(
            user_id=user.user_id,
            scope=scope,
            key=idempotency_key,
            request_fingerprint=request_fingerprint(
                domain=scope,
                payload=body.model_dump(mode="json"),
            ),
            operation=operation,
        )
        current = await app.state.runtime.store.get_run(
            user_id=user.user_id, run_id=run_id
        )
        if not replayed and current.mode != "analysis":
            app.state.runtime.schedule_run(user_id=user.user_id, run_id=run_id)
        return RunResponse(
            data=resumed,
            meta=ResponseMeta(
                request_id=new_id("req"),
                idempotency_replayed=replayed,
            ),
        )

    @app.post("/agent-api/v1/runs/{run_id}/steers", status_code=202)
    async def create_steer(
        run_id: str,
        body: SteerCreateBody,
        user: Annotated[CurrentUser, Depends(require_geotoken)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1)],
    ) -> SteerResponse:
        async def operation():
            return await app.state.runtime.executor.steer(
                user_id=user.user_id,
                run_id=run_id,
                client_instance_id=body.client_instance_id,
                content=body.content,
            )

        scope = f"runs:{run_id}:steers:create"
        steer, replayed = await app.state.runtime.idempotency.execute(
            user_id=user.user_id,
            scope=scope,
            key=idempotency_key,
            request_fingerprint=request_fingerprint(
                domain=scope,
                payload=body.model_dump(mode="json"),
            ),
            operation=operation,
        )
        return SteerResponse(
            data=steer,
            meta=ResponseMeta(
                request_id=new_id("req"),
                idempotency_replayed=replayed,
            ),
        )

    @app.put(
        "/agent-api/v1/runs/{run_id}/frontend-command-receipts/{command_id}"
    )
    async def put_frontend_command_receipt(
        run_id: str,
        command_id: str,
        body: FrontendCommandReceipt,
        user: Annotated[CurrentUser, Depends(require_geotoken)],
    ) -> FrontendCommandReceiptResponse:
        if body.command_id != command_id:
            raise RunStateConflict("receipt command does not match request path")
        receipt = await app.state.runtime.store.put_frontend_command_receipt(
            user_id=user.user_id,
            run_id=run_id,
            command_id=command_id,
            receipt=body,
        )
        return FrontendCommandReceiptResponse(
            data=receipt,
            meta=ResponseMeta(request_id=new_id("req")),
        )

    @app.get(
        "/agent-api/v1/runs/{run_id}/events",
        response_class=EventStreamResponse,
    )
    async def stream_run_events(
        run_id: str,
        user: Annotated[CurrentUser, Depends(require_geotoken)],
        last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
    ) -> StreamingResponse:
        await app.state.runtime.store.get_run(user_id=user.user_id, run_id=run_id)
        await app.state.runtime.events.validate_cursor(
            run_id=run_id,
            after_event_id=last_event_id,
        )

        async def event_stream():
            async for event in app.state.runtime.events.stream(
                run_id=run_id,
                after_event_id=last_event_id,
            ):
                data = json.dumps(
                    event.data,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                yield (
                    f"id: {event.event_id}\n"
                    f"event: {event.type}\n"
                    f"data: {data}\n\n"
                )

        return EventStreamResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    @app.get("/agent-api/v1/results/{result_id}")
    async def get_result(
        result_id: str,
        user: Annotated[CurrentUser, Depends(require_geotoken)],
    ) -> ResultResponse:
        result = await app.state.runtime.store.get_result(
            user_id=user.user_id,
            result_id=result_id,
        )
        if result.payload_expires_at <= datetime.now(UTC):
            return ResultResponse(
                data=_expired_result_metadata(result),
                meta=ResponseMeta(request_id=new_id("req")),
            )
        return ResultResponse(
            data=result,
            meta=ResponseMeta(request_id=new_id("req")),
        )

    @app.get("/agent-api/v1/results/{result_id}/items")
    async def get_result_items(
        result_id: str,
        user: Annotated[CurrentUser, Depends(require_geotoken)],
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        cursor: Annotated[str | None, Query(min_length=1)] = None,
    ) -> ResultItemsResponse:
        result = await app.state.runtime.store.get_result(
            user_id=user.user_id,
            result_id=result_id,
        )
        if result.payload_expires_at <= datetime.now(UTC):
            raise ResultPayloadExpired("result payload is no longer available")
        if not isinstance(result, TableDataResult):
            raise ResourceNotFound("result items are not available")
        offset = (
            app.state.runtime.cursor_codec.decode(
                cursor,
                user_id=user.user_id,
                resource_id=result_id,
                limit=limit,
            )
            if cursor is not None
            else 0
        )
        rows = result.data.rows[offset : offset + limit]
        next_offset = offset + len(rows)
        has_next = next_offset < len(result.data.rows)
        next_cursor = (
            app.state.runtime.cursor_codec.encode(
                user_id=user.user_id,
                resource_id=result_id,
                offset=next_offset,
                limit=limit,
            )
            if has_next
            else None
        )
        return ResultItemsResponse(
            data=list(rows),
            meta=CursorPageMeta(
                request_id=new_id("req"),
                has_next=has_next,
                next_cursor=next_cursor,
            ),
        )

    @app.get("/agent-api/v1/evidence/{evidence_id}")
    async def get_evidence(
        evidence_id: str,
        user: Annotated[CurrentUser, Depends(require_geotoken)],
    ) -> EvidenceResponse:
        evidence = await app.state.runtime.store.get_evidence(
            user_id=user.user_id,
            evidence_id=evidence_id,
        )
        return EvidenceResponse(
            data=evidence,
            meta=ResponseMeta(request_id=new_id("req")),
        )

    return app


def _expired_result_metadata(result: DataResult) -> ResultMetadata:
    summary: dict[str, object]
    if isinstance(result, TableDataResult):
        title = "表格结果"
        summary = {
            "row_count": result.row_count,
            "truncated": result.truncated,
            "text": f"已生成 {result.row_count} 条结果，Payload 已过期。",
        }
    elif result.kind == "area_candidates":
        title = "区划候选结果"
        summary = {
            "candidate_count": result.candidate_count,
            "text": "区划候选 Payload 已过期。",
        }
    elif result.kind == "analysis_report":
        title = "区域研判报告"
        summary = {
            "status": result.status,
            "section_count": len(result.sections),
            "text": "区域研判报告 Payload 已过期。",
        }
    else:
        title = result.data.title
        summary = {"text": "对象画像 Payload 已过期。"}
    return ResultMetadata(
        result_id=result.result_id,
        kind=result.kind,
        data_schema_ref=result.data_schema_ref,
        result_fingerprint=result.result_fingerprint,
        title=title,
        summary=summary,
        evidence_ids=result.evidence_ids,
        payload_expires_at=result.payload_expires_at,
    )


app = create_app()
