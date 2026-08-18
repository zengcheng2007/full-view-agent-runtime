import asyncio
import base64
import binascii
import csv
import hashlib
import io
import json
import logging
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from secrets import token_bytes
from threading import RLock
from typing import Annotated, Literal, cast

from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response, StreamingResponse
from httpx import AsyncBaseTransport
from pydantic import Field, JsonValue, SecretStr, model_validator

from full_view_agent.api._api_deps import (
    CurrentUser,
    ResponseMeta,
    UnauthenticatedError,
    require_geotoken,
)
from full_view_agent.api.agent_routes import create_agent_router
from full_view_agent.api.capability_routes import create_capability_router
from full_view_agent.api.knowledge_routes import create_knowledge_router
from full_view_agent.api.prompt_routes import create_prompt_router
from full_view_agent.api.runtime_observability_routes import (
    create_runtime_observability_router,
)
from full_view_agent.application.agent_management_service import (
    AgentManagementService,
    AgentRepository,
)
from full_view_agent.application.analysis_graph import AnalysisRunOutcome
from full_view_agent.application.analysis_plan_repository import (
    AnalysisPlanRepository,
    AnalysisPlanStoreRejected,
)
from full_view_agent.application.analysis_planner import AnalysisPlanner
from full_view_agent.application.analysis_run_binding import AnalysisRunBindingStore
from full_view_agent.application.analysis_service import AnalysisPlanningService
from full_view_agent.application.application_identity_registry import (
    ApplicationIdentityAdapterRegistry,
    TrustedApplicationIdentityContext,
)
from full_view_agent.application.application_management_service import (
    ApplicationManagementService,
    ApplicationRegistry,
)
from full_view_agent.application.auth_context_refresh import RunAuthContextRefresher
from full_view_agent.application.builtin_capability_seeds import population_tool_v1_2
from full_view_agent.application.capability_consistency import (
    validate_production_http_capabilities,
)
from full_view_agent.application.capability_management_service import (
    CapabilityManagementService,
)
from full_view_agent.application.capability_service import DynamicToolAdapter, ToolAdapter
from full_view_agent.application.context_builder import AgentContextBuilder
from full_view_agent.application.cursor_codec import SignedCursorCodec
from full_view_agent.application.deployment_capabilities import (
    EVENT_CATEGORY_ENV,
    HOUSING_NEXT_AREA_ENV,
    parse_event_category_enabled,
    parse_housing_next_area_enabled,
)
from full_view_agent.application.dynamic_skill_workflow_bridge import (
    PublishedRuntimeCapabilityLoader,
    RuntimeCapabilityDefinitionInvalid,
    RuntimeSkillContract,
    RuntimeWorkflowGraphSnapshot,
)
from full_view_agent.application.errors import (
    AnalysisExecutionUnavailable,
    AnalysisPlanningUnavailable,
    AnalysisRequestRejected,
    ApplicationError,
    AuthenticationFailed,
    AuthorizationDenied,
    CommandClientMismatch,
    CommandReceiptConflict,
    ConnectorConfigurationInvalid,
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
from full_view_agent.application.knowledge_service import KnowledgeService
from full_view_agent.application.model_config_repository import (
    InMemoryRunModelBindingRepository,
    PostgresRunModelBindingRepository,
    RunModelBindingRepository,
)
from full_view_agent.application.model_config_service import (
    InMemoryModelConfigKeyStore,
    ModelConfigService,
)
from full_view_agent.application.model_inference_policy import (
    model_reasoning_capability_from_environment,
)
from full_view_agent.application.model_planner import (
    ModelPlannerFactory,
    RunBoundModelPlannerFactory,
)
from full_view_agent.application.model_provider import ModelProvider
from full_view_agent.application.native_orchestrator import RunPlannerFactory
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
from full_view_agent.application.prompt_template_service import PromptTemplateService
from full_view_agent.application.run_admission import RunAdmissionService
from full_view_agent.application.run_capability_snapshot import (
    RunCapabilitySnapshotService,
)
from full_view_agent.application.run_capability_snapshot_store import (
    InMemoryRunCapabilitySnapshotStore,
    PostgresRunCapabilitySnapshotStore,
    RunCapabilitySnapshotStore,
)
from full_view_agent.application.runtime_prompt_registry import RuntimePromptRegistry
from full_view_agent.application.runtime_skill_registry import RuntimeSkillRegistry
from full_view_agent.application.runtime_workflow_registry import (
    RuntimeWorkflowRegistry,
)
from full_view_agent.application.semantic_wiring import (
    build_semantic_capability_stack,
)
from full_view_agent.application.session_run_service import SessionRunService, new_id
from full_view_agent.application.tool_registry import (
    PRODUCTION_HTTP_TOOL_IDS,
    ToolRegistry,
)
from full_view_agent.application.workflow_registry import WorkflowRegistry
from full_view_agent.domain.agent_definition import AgentDefinition
from full_view_agent.domain.analysis_plan import AnalysisPlan, AnalysisRequest
from full_view_agent.domain.capability import CapabilityBase, CapabilityStatus
from full_view_agent.domain.models import (
    AgentMessage,
    AgentRun,
    AgentSession,
    ContractModel,
    DataResult,
    EnterpriseIndustryDistributionRow,
    EnterpriseMetricRow,
    EnterpriseScaleDistributionRow,
    EnterpriseTypeDistributionRow,
    EventCategoryRow,
    EventFinishRateRow,
    EventTrendRow,
    Evidence,
    FrontendCommandReceipt,
    GovernanceOverviewRow,
    GovernancePowerMetricRow,
    HousingAreaGroupRow,
    HousingLeaseTypeRow,
    HousingRoomUseRow,
    HousingStockOverviewRow,
    PendingInputRequest,
    PopulationAggregateRow,
    PopulationMetricRow,
    PopulationRankingRow,
    ResultMetadata,
    ResultReferenceContent,
    RunCreateRequest,
    RunInputBody,
    Steer,
    TableDataResult,
    TextContent,
    WorkflowRef,
)
from full_view_agent.infrastructure.agent_repository import (
    InMemoryAgentRepository,
    PostgresAgentRepository,
)
from full_view_agent.infrastructure.analysis_plan_repository import (
    InMemoryAnalysisPlanRepository,
    PostgresAnalysisPlanRepository,
)
from full_view_agent.infrastructure.analysis_run_binding_store import (
    InMemoryAnalysisRunBindingStore,
    PostgresAnalysisRunBindingStore,
)
from full_view_agent.infrastructure.application_registry import (
    default_application_registry,
)
from full_view_agent.infrastructure.auth_context_store import (
    InMemoryRunAuthContextStore,
)
from full_view_agent.infrastructure.capability_repository import (
    CapabilityRepository,
    InMemoryCapabilityRepository,
    InMemoryModelConfigRepository,
    PostgresCapabilityRepository,
    PostgresModelConfigRepository,
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
from full_view_agent.infrastructure.http_connector_executor import (
    ConnectorConnectionTester,
    configured_connector_allowed_private_hosts,
)
from full_view_agent.infrastructure.idempotency_store import InMemoryIdempotencyStore
from full_view_agent.infrastructure.knowledge_repository import (
    InMemoryKeywordRetriever,
    InMemoryKnowledgeRepository,
    TextDocumentParser,
)
from full_view_agent.infrastructure.knowledge_tool_adapter import (
    KnowledgeAwareToolAdapter,
)
from full_view_agent.infrastructure.legacy_identity import HttpLegacyIdentityAdapter
from full_view_agent.infrastructure.memory_store import InMemoryAgentStore
from full_view_agent.infrastructure.model_provider_factory import build_model_provider
from full_view_agent.infrastructure.openai_compatible_model import (
    OpenAICompatibleModelProvider,
)
from full_view_agent.infrastructure.postgres_knowledge_repository import (
    PostgresKnowledgeRepository,
)
from full_view_agent.infrastructure.postgres_persistence import PostgresAgentPersistence
from full_view_agent.infrastructure.prompt_template_repository import (
    InMemoryPromptTemplateRepository,
    PostgresPromptTemplateRepository,
)
from full_view_agent.infrastructure.redis_event_notifier import RedisEventNotifier

logger = logging.getLogger(__name__)


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
        PopulationAggregateRow
        | PopulationMetricRow
        | PopulationRankingRow
        | HousingLeaseTypeRow
        | HousingAreaGroupRow
        | HousingRoomUseRow
        | HousingStockOverviewRow
        | EventFinishRateRow
        | EventCategoryRow
        | EventTrendRow
        | EnterpriseMetricRow
        | EnterpriseScaleDistributionRow
        | EnterpriseTypeDistributionRow
        | EnterpriseIndustryDistributionRow
        | GovernanceOverviewRow
        | GovernancePowerMetricRow
        | dict[str, JsonValue]
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


class _ProjectedCapabilityRepository:
    """Read-only repository view with lifecycle changes overlaid in memory."""

    def __init__(
        self,
        repository: CapabilityRepository,
        overrides: dict[tuple[str, str], CapabilityBase],
    ) -> None:
        self._repository = repository
        self._overrides = overrides

    async def list_capabilities(
        self,
        *,
        capability_type=None,
        status=None,
    ) -> list[CapabilityBase]:
        capabilities = await self._repository.list_capabilities(
            capability_type=capability_type,
            status=None,
        )
        projected = [
            self._overrides.get(
                (capability.capability_id, capability.version), capability
            )
            for capability in capabilities
        ]
        if status is not None:
            projected = [item for item in projected if item.status == status]
        return sorted(projected, key=lambda item: (item.capability_id, item.version))


@dataclass
class RuntimeContainer:
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC), init=False)
    store: AgentStore | None = None
    events: EventStore | None = None
    idempotency: IdempotencyStore | None = None
    identity_port: LegacyIdentityPort = field(
        default_factory=default_identity_port
    )
    application_identity_adapters: ApplicationIdentityAdapterRegistry | None = None
    capability_identity_port: LegacyIdentityPort | None = None
    credentials: CredentialBroker | None = None
    auth_contexts: RunAuthContextStore | None = None
    denial_ledger: InMemoryDenialLedger = field(default_factory=InMemoryDenialLedger)
    workflow_registry: WorkflowRegistry = field(default_factory=WorkflowRegistry.default)
    runtime_skills: tuple[RuntimeSkillContract, ...] = field(
        default=(), init=False
    )
    runtime_workflows: tuple[RuntimeWorkflowGraphSnapshot, ...] = field(
        default=(), init=False
    )
    runtime_skill_registry: RuntimeSkillRegistry = field(
        default_factory=RuntimeSkillRegistry, init=False
    )
    runtime_workflow_registry: RuntimeWorkflowRegistry = field(
        default_factory=RuntimeWorkflowRegistry, init=False
    )
    runtime_prompt_registry: RuntimePromptRegistry = field(
        default_factory=RuntimePromptRegistry, init=False
    )
    runtime_capability_generation: int = field(default=0, init=False)
    _runtime_capability_reload_lock: asyncio.Lock = field(
        default_factory=asyncio.Lock, init=False, repr=False
    )
    _runtime_capability_state_lock: RLock = field(
        default_factory=RLock, init=False, repr=False
    )
    _static_tool_registry: ToolRegistry = field(init=False, repr=False)
    application_registry: ApplicationRegistry | None = None
    application_management_service: ApplicationManagementService | None = field(
        default=None, init=False
    )
    agent_repository: AgentRepository | None = field(default=None, init=False)
    agent_management_service: AgentManagementService | None = field(
        default=None, init=False
    )
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
    # P2: Capability Center services
    capability_repository: CapabilityRepository | None = None
    dynamic_tool_adapter: DynamicToolAdapter | None = None
    connector_connection_transport: AsyncBaseTransport | None = None
    connector_allowed_private_hosts: frozenset[str] = field(
        default_factory=configured_connector_allowed_private_hosts
    )
    capability_management_service: CapabilityManagementService | None = field(
        default=None, init=False
    )
    model_config_service: ModelConfigService | None = field(
        default=None, init=False
    )
    prompt_template_service: PromptTemplateService | None = field(
        default=None, init=False
    )
    knowledge_service: KnowledgeService | None = field(default=None, init=False)
    run_capability_snapshot_service: RunCapabilitySnapshotService | None = field(
        default=None, init=False
    )
    # Run-scoped model config binding repository: persists (run_id,
    # config_id, config_version) bindings plus immutable snapshots of the
    # config metadata (with encrypted key material) at binding time.
    run_model_binding_repository: RunModelBindingRepository | None = field(
        default=None, init=False
    )
    # Run-scoped capability snapshot store: persists (run_id, tool_id,
    # version) triples so that a Run's pinned dynamic-tool versions
    # survive process restart.
    run_capability_snapshot_store: RunCapabilitySnapshotStore | None = field(
        default=None, init=False
    )
    # Set True once async ``initialize()`` has completed.  Prevents
    # re-initialisation on repeated lifespan calls.
    _initialized: bool = field(default=False, init=False, repr=False)

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
        self.application_registry = self.application_registry or default_application_registry(
            dsn=database_url,
            schema=os.getenv("FULL_VIEW_POSTGRES_SCHEMA", "full_view_agent"),
        )
        self.application_identity_adapters = (
            self.application_identity_adapters
            or ApplicationIdentityAdapterRegistry(
                {"identity.legacy_geo": self.identity_port}
            )
        )
        assert self.store is not None
        assert self.events is not None
        assert self.idempotency is not None
        assert self.credentials is not None
        assert self.auth_contexts is not None
        assert self.application_registry is not None
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
            application_registry=self.application_registry,
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
        event_category_enabled = parse_event_category_enabled(
            os.getenv(EVENT_CATEGORY_ENV)
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
            if (
                self.tool_registry is not None
                and self.tool_registry.event_category_enabled
                != event_category_enabled
            ):
                raise RuntimeError(
                    "injected ToolRegistry disagrees with "
                    f"{EVENT_CATEGORY_ENV}"
                )
            self.tool_registry = self.tool_registry or ToolRegistry.default(
                housing_next_area_enabled=housing_next_area_enabled,
                event_category_enabled=event_category_enabled,
            )
            self.tool_registry = self.tool_registry.subset(
                set(PRODUCTION_HTTP_TOOL_IDS) | {"knowledge.search"}
            )
        else:
            self.tool_registry = self.tool_registry or ToolRegistry.default()
        self.tool_registry.bind_lock(self._runtime_capability_state_lock)
        self.runtime_skill_registry.bind_lock(self._runtime_capability_state_lock)
        self.runtime_workflow_registry.bind_lock(
            self._runtime_capability_state_lock
        )
        self._static_tool_registry = self.tool_registry.snapshot()

        # P2: Initialize Capability Center services EARLY so we can check for
        # enabled model configs before creating model_provider
        if database_url:
            self.capability_repository = self.capability_repository or (
                PostgresCapabilityRepository(
                    dsn=database_url,
                    schema=os.getenv("FULL_VIEW_POSTGRES_SCHEMA", "full_view_agent"),
                )
            )
            model_config_repo = PostgresModelConfigRepository(
                dsn=database_url,
                schema=os.getenv("FULL_VIEW_POSTGRES_SCHEMA", "full_view_agent"),
            )
            self.agent_repository = PostgresAgentRepository(
                dsn=database_url,
                schema=os.getenv("FULL_VIEW_POSTGRES_SCHEMA", "full_view_agent"),
            )
            prompt_template_repo = PostgresPromptTemplateRepository(
                dsn=database_url,
                schema=os.getenv("FULL_VIEW_POSTGRES_SCHEMA", "full_view_agent"),
            )
            knowledge_repo = PostgresKnowledgeRepository(
                dsn=database_url,
                schema=os.getenv("FULL_VIEW_POSTGRES_SCHEMA", "full_view_agent"),
            )
            # Run-scoped binding repository: persists (run_id, config_id,
            # config_version) plus the immutable snapshot row for that
            # tuple. Uses the same PostgreSQL instance as the other
            # repositories so bindings survive process restarts.
            self.run_model_binding_repository = PostgresRunModelBindingRepository(
                dsn=database_url,
                schema=os.getenv("FULL_VIEW_POSTGRES_SCHEMA", "full_view_agent"),
            )
            # Run-scoped capability snapshot store: persists (run_id,
            # tool_id, version) triples for cross-process durability.
            self.run_capability_snapshot_store = PostgresRunCapabilitySnapshotStore(
                dsn=database_url,
                schema=os.getenv("FULL_VIEW_POSTGRES_SCHEMA", "full_view_agent"),
            )
            # Use PostgreSQL-backed key store for true restart persistence
            credential_key = _configured_credential_key()
            if credential_key:
                from full_view_agent.infrastructure.postgres_model_config_key_store import (
                    PostgresModelConfigKeyStore,
                )
                model_config_key_store = PostgresModelConfigKeyStore(
                    dsn=database_url,
                    schema=os.getenv("FULL_VIEW_POSTGRES_SCHEMA", "full_view_agent"),
                    encryption_key=credential_key,
                )
                logger.info("Using PostgresModelConfigKeyStore for persistent API key storage")
            else:
                # Fallback to in-memory for development without credential key
                model_config_key_store = InMemoryModelConfigKeyStore()
                logger.warning(
                    "Using InMemoryModelConfigKeyStore"
                    " - keys will not persist across restarts"
                )
        else:
            self.capability_repository = (
                self.capability_repository or InMemoryCapabilityRepository()
            )
            model_config_repo = InMemoryModelConfigRepository()
            self.agent_repository = InMemoryAgentRepository()
            model_config_key_store = InMemoryModelConfigKeyStore()
            prompt_template_repo = InMemoryPromptTemplateRepository()
            knowledge_repo = InMemoryKnowledgeRepository()
            self.run_model_binding_repository = InMemoryRunModelBindingRepository()
            self.run_capability_snapshot_store = InMemoryRunCapabilitySnapshotStore()

        self.capability_management_service = CapabilityManagementService(
            repository=self.capability_repository,
            allowed_private_hosts=self.connector_allowed_private_hosts,
        )
        assert self.application_registry is not None
        self.application_management_service = ApplicationManagementService(
            application_registry=self.application_registry,
            capability_repository=self.capability_repository,
        )
        self.model_config_service = ModelConfigService(
            repository=model_config_repo,
            key_store=model_config_key_store,
            agent_release_reader=self.agent_repository,
        )
        self.prompt_template_service = PromptTemplateService(prompt_template_repo)
        self.knowledge_service = KnowledgeService(
            repository=knowledge_repo,
            retriever=InMemoryKeywordRetriever(),
            parsers=(TextDocumentParser(),),
        )
        assert self.agent_repository is not None
        self.agent_management_service = AgentManagementService(
            repository=self.agent_repository,
            application_registry=self.application_registry,
            model_config_service=self.model_config_service,
            capability_repository=self.capability_repository,
            prompt_reader=self.prompt_template_service,
            knowledge_reader=self.knowledge_service,
            model_snapshot_repository=self.run_model_binding_repository,
        )
        # P2-2: RunCapabilitySnapshotService – creates per-run immutable
        # capability snapshots so that dynamic tool version changes don't
        # affect in-flight runs. The ``store`` (when set) provides
        # cross-process durability for the pinned version triples.
        assert self.run_capability_snapshot_store is not None
        self.run_capability_snapshot_service = RunCapabilitySnapshotService(
            repository=self.capability_repository,
            store=self.run_capability_snapshot_store,
            application_registry=self.application_registry,
            runtime_skill_registry=self.runtime_skill_registry,
            runtime_workflow_registry=self.runtime_workflow_registry,
            runtime_prompt_registry=self.runtime_prompt_registry,
            prompt_snapshot_loader=self.prompt_template_service.load_snapshot,
            prompt_history_snapshot_loader=(
                self.prompt_template_service.load_historical_snapshot
            ),
        )

        # ── Async-initialised concerns (require event loop) ──────────
        # Dynamic tool loading from capability repository and model config
        # resolution from the database are deferred to the async lifecycle
        # (``initialize()`` called from the FastAPI lifespan).  This avoids
        # fragile ``run_until_complete()`` patterns that produce "coroutine
        # was never awaited" warnings in async test contexts.
        #
        # Database config resolution MUST fail explicitly when a database is
        # configured (A3 requirement: 数据库配置存在但读取失败时必须显式失
        # 败，禁止静默回退环境变量).  Env-var fallback is ONLY used when no
        # database URL is configured at all.

        if (
            self.model_provider is None
            and model_provider_mode == "openai_compatible"
            and not database_url
        ):
            # No database: fall back to env vars (the only allowed fallback
            # path per A3).
            model_base_url = os.getenv("FULL_VIEW_MODEL_BASE_URL")
            model_name = os.getenv("FULL_VIEW_MODEL_NAME")
            if not model_base_url:
                raise RuntimeError(
                    "FULL_VIEW_MODEL_BASE_URL is required for openai_compatible "
                    "when no enabled model config exists in database"
                )
            if not model_name:
                raise RuntimeError(
                    "FULL_VIEW_MODEL_NAME is required for openai_compatible "
                    "when no enabled model config exists in database"
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

        # Build downstream components (semantic stack, executor, analysis
        # services).  When model_provider was deferred (openai_compatible +
        # database), it will be None here and the components are rebuilt in
        # ``initialize()`` once the provider is resolved.
        self._build_downstream_components(database_url)

    def _build_downstream_components(self, database_url: str | None) -> None:
        """Build semantic stack, executor, and analysis services.

        Called from ``__post_init__`` (with the sync-available model_provider)
        and again from ``initialize()`` once the DB-backed model_provider has
        been resolved.
        """
        runtime_profile = os.getenv("FULL_VIEW_RUNTIME_PROFILE", "development").lower()

        # Type narrowing: these are always set by __post_init__ before this
        # method is called.
        assert self.tool_registry is not None
        assert self.governance_adapter is not None
        assert self.store is not None
        assert self.events is not None

        # P2: Create dynamic tool adapter for executing dynamic tools via HTTP connectors
        dynamic_tool_adapter = self.dynamic_tool_adapter
        if dynamic_tool_adapter is None and self.capability_repository is not None:
            try:
                from full_view_agent.application.dynamic_tool_adapter import (
                    HttpDynamicToolAdapter,
                )
                from full_view_agent.infrastructure.http_connector_executor import (
                    HttpConnectorExecutor,
                )

                http_executor = HttpConnectorExecutor(
                    repository=self.capability_repository,
                    follow_redirects=False,
                    default_timeout_ms=8000,
                    allowed_private_hosts=self.connector_allowed_private_hosts,
                    credential_broker=self.credentials,
                )
                dynamic_tool_adapter = HttpDynamicToolAdapter(
                    repository=self.capability_repository,
                    http_executor=http_executor,
                )
                self.dynamic_tool_adapter = dynamic_tool_adapter
                logger.info("Dynamic tool adapter initialized for HTTP connector execution")
            except Exception as e:
                logger.warning(f"Failed to initialize dynamic tool adapter: {e}")

        # S1-A：语义入口与既有能力栈共享一份接线（Catalog/Resolver/
        # Executor/Fingerprinter/Presenter），Native 与 LangGraph 走同一
        # Harness 包装，不改变任何既有 Tool 的行为。
        assert self.knowledge_service is not None
        assert self.run_capability_snapshot_service is not None
        runtime_tool_adapter = KnowledgeAwareToolAdapter(
            inner=self.governance_adapter,
            knowledge_service=self.knowledge_service,
            snapshot_reader=self.run_capability_snapshot_service,
        )
        self.semantic_stack = build_semantic_capability_stack(
            registry=self.tool_registry,
            adapter=runtime_tool_adapter,
            auth_context_refresher=self.auth_context_refresher,
            denial_ledger=self.denial_ledger,
            dynamic_tool_adapter=dynamic_tool_adapter,
        )
        if isinstance(self.governance_adapter, HttpGovernanceAdapter):
            validate_production_http_capabilities(
                registry=self.tool_registry.subset(set(PRODUCTION_HTTP_TOOL_IDS)),
                catalog=self.semantic_stack.catalog,
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
        planner_factory: RunPlannerFactory | None = (
            ModelPlannerFactory(
                provider=self.model_provider,
                event_publisher=self.events,
                context_builder=AgentContextBuilder(
                    store=self.store,
                    registry=self.tool_registry,
                    semantic_presenter=self.semantic_stack.presenter,
                    skill_registry=self.runtime_skill_registry,
                    prompt_registry=self.runtime_prompt_registry,
                ),
                max_total_tokens=int(
                    os.getenv("FULL_VIEW_MODEL_TOKEN_BUDGET", "32000")
                ),
                max_output_tokens=int(
                    os.getenv("FULL_VIEW_MODEL_MAX_OUTPUT_TOKENS", "32000")
                ),
            )
            if self.model_provider is not None
            else None
        )
        if planner_factory is not None and self.model_config_service is not None:
            assert self.run_model_binding_repository is not None
            planner_factory = RunBoundModelPlannerFactory(
                base_factory=planner_factory,
                config_resolver=self.model_config_service,
                provider_builder=build_model_provider,
                config_repository=self.run_model_binding_repository,
                agent_release_repository=self.agent_repository,
                fallback_reasoning_capability=(
                    model_reasoning_capability_from_environment()
                ),
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
            dynamic_tool_adapter=dynamic_tool_adapter,
            run_capability_snapshot_service=self.run_capability_snapshot_service,
            runtime_workflow_registry=self.runtime_workflow_registry,
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

    async def resolve_application_identity(
        self,
        *,
        app_id: str,
        raw_token: SecretStr,
    ) -> TrustedApplicationIdentityContext:
        """Resolve identity for a server-selected application.

        Data-plane routes must pass a fixed application identifier or one
        derived from trusted gateway routing, never a client header/query.
        """
        assert self.application_registry is not None
        assert self.application_identity_adapters is not None
        application = await self.application_registry.get_application(app_id)
        if application is None:
            raise ResourceNotFound("application is not registered")
        return await self.application_identity_adapters.resolve_for_application(
            application=application,
            raw_token=raw_token,
        )

    async def initialize(self) -> None:
        """Async lifecycle initialisation.

        Resolves deferred concerns that require an event loop:

        * Load published dynamic tools from the capability repository and
          merge them into the ``ToolRegistry``.
        * Resolve model config from the database when ``openai_compatible``
          mode is active and a ``FULL_VIEW_DATABASE_URL`` is configured.
          **If the database config exists but resolution fails, the error
          is raised explicitly** — no silent fallback to env vars (A3).
        * Env-var fallback for model config is used ONLY when no database
          URL is configured.

        Idempotent — safe to call multiple times.  Must be called from the
        FastAPI lifespan (via ``create_app()``) before the first request.
        Tests that instantiate ``RuntimeContainer`` directly and need async
        resolution should call ``await runtime.initialize()`` explicitly.
        """
        if self._initialized:
            return
        self._initialized = True

        database_url = os.getenv("FULL_VIEW_DATABASE_URL")
        model_provider_mode = os.getenv(
            "FULL_VIEW_MODEL_PROVIDER", "deterministic"
        ).lower()

        # Managed capability, prompt, and knowledge repositories share the
        # persistence schema. Apply its ordered idempotent migrations before
        # any of those repositories are queried during startup.
        if self.persistence is not None:
            await self.persistence.initialize()

        # The no-DB composition consumes a published control-plane Tool too;
        # the physical built-in Registry deliberately remains contract-free.
        if isinstance(self.capability_repository, InMemoryCapabilityRepository):
            population_seed = population_tool_v1_2()
            existing_population = await self.capability_repository.get(
                population_seed.capability_id, population_seed.version
            )
            if existing_population is None:
                await self.capability_repository.save_tool(population_seed)

        # Materialise the legacy default Agent as a first-class definition.
        # Existing deployments therefore gain an application-centred control
        # plane without changing any data-plane behavior until an Agent
        # version is explicitly published.
        if self.agent_management_service is not None:
            existing_agents = await self.agent_management_service.list_agents(
                "full_information_view"
            )
            if not existing_agents:
                await self.agent_management_service.create_agent(
                    AgentDefinition(
                        app_id="full_information_view",
                        agent_id="governance_general_agent",
                        name="全量信息视图智能体",
                        description="全量信息视图默认治理问答智能体",
                    )
                )

        # ── Dynamic tool loading ──────────────────────────────────────
        await self.reload_runtime_capabilities()
        effective_prompt = None
        if self.prompt_template_service is not None:
            effective_prompt = await self.prompt_template_service.get_effective(
                app_id="full_information_view"
            )
            self.runtime_prompt_registry.activate(effective_prompt)

        # Backfill the historical default only after migrations and managed
        # resources are readable. The release freezes existing grants and can
        # never broaden what the application is allowed to use.
        if (
            self.agent_management_service is not None
            and self.prompt_template_service is not None
            and self.knowledge_service is not None
        ):
            knowledge_refs: list[str] = []
            for knowledge_base in await self.knowledge_service.list_knowledge_bases(
                tenant_id="legacy",
                app_id="full_information_view",
            ):
                version = knowledge_base.published_version
                if version is not None and await self.knowledge_service.is_ready_version(
                    app_id="full_information_view",
                    knowledge_base_id=knowledge_base.knowledge_base_id,
                    version=version,
                ):
                    knowledge_refs.append(
                        f"{knowledge_base.knowledge_base_id}@{version}.0.0"
                    )
            baseline = (
                await self.agent_management_service.ensure_legacy_baseline_release(
                    app_id="full_information_view",
                    agent_id="governance_general_agent",
                    # Application policy is pinned separately by the Run prompt
                    # bundle. It must never be disguised as an Agent prompt_ref.
                    prompt_ref=None,
                    knowledge_base_refs=tuple(knowledge_refs),
                )
            )
            if baseline is None:
                logger.warning(
                    "Legacy default Agent has no active release and no unique "
                    "trusted model; retaining explicit legacy compatibility"
                )

        # ── Model config resolution ───────────────────────────────────
        if (
            self.model_provider is None
            and model_provider_mode == "openai_compatible"
            and database_url
        ):
            # Database URL is configured — resolution MUST succeed or raise
            # explicitly.  Silent fallback to env vars is forbidden (A3).
            assert self.model_config_service is not None
            enabled_config = await self.model_config_service.resolve_for_runtime(
                required=False
            )
            if enabled_config:
                logger.info(
                    f"Using database model config: {enabled_config.name} "
                    f"({enabled_config.model_name})"
                )
                self.model_provider = build_model_provider(enabled_config)
            else:
                logger.warning(
                    "Model control plane started without an explicit legacy "
                    "default; legacy model execution remains unavailable until "
                    "an administrator publishes and selects one"
                )
            # Rebuild model-dependent components only after a provider is
            # actually resolved.  Control-plane routes remain available while
            # the model centre is still unconfigured.
            if self.model_provider is not None:
                self._build_downstream_components(database_url)
        # If not deferred (openai_compatible + database_url), downstream
        # components were already built in __post_init__.  Nothing more to do.

    async def reload_runtime_capabilities(self) -> int:
        """Fail closed while switching the published runtime generation."""

        async with self._runtime_capability_reload_lock:
            candidate = await self._build_runtime_capability_candidate(
                self.capability_repository
            )
            return self._activate_runtime_capability_candidate(candidate)

    async def validate_runtime_capability_transition(
        self,
        capability_id: str,
        version: str,
        to_status: CapabilityStatus,
    ) -> None:
        """Compile a projected lifecycle state before any repository mutation."""

        if self.capability_repository is None:
            raise RuntimeError("runtime capability dependencies not initialized")
        existing = await self.capability_repository.get(capability_id, version)
        if existing is None:
            raise ResourceNotFound("capability not found")
        projected = existing.model_copy(update={"status": to_status})
        repository = _ProjectedCapabilityRepository(
            self.capability_repository,
            {(capability_id, version): projected},
        )
        async with self._runtime_capability_reload_lock:
            await self._build_runtime_capability_candidate(
                cast(CapabilityRepository, repository)
            )

    async def validate_runtime_capability_rollback(
        self,
        capability_id: str,
        to_version: str,
    ) -> None:
        """Compile the rollback target and deactivated current version first."""

        if self.capability_repository is None:
            raise RuntimeError("runtime capability dependencies not initialized")
        target = await self.capability_repository.get(capability_id, to_version)
        if target is None:
            raise ResourceNotFound("target version not found")
        overrides: dict[tuple[str, str], CapabilityBase] = {
            (capability_id, to_version): target.model_copy(
                update={"status": "published"}
            )
        }
        current = await self.capability_repository.get_active_snapshot(capability_id)
        if current is not None and current.version != to_version:
            current_capability = await self.capability_repository.get(
                capability_id, current.version
            )
            if current_capability is not None:
                overrides[(capability_id, current.version)] = (
                    current_capability.model_copy(update={"status": "disabled"})
                )
        repository = _ProjectedCapabilityRepository(
            self.capability_repository,
            overrides,
        )
        async with self._runtime_capability_reload_lock:
            await self._build_runtime_capability_candidate(
                cast(CapabilityRepository, repository)
            )

    async def _build_runtime_capability_candidate(
        self,
        repository: CapabilityRepository | None,
    ) -> tuple[
        ToolRegistry,
        tuple[RuntimeSkillContract, ...],
        tuple[RuntimeWorkflowGraphSnapshot, ...],
        int,
    ]:
        """Build and validate one complete generation without activating it."""

        from full_view_agent.application.dynamic_tool_bridge import (
            build_dynamic_input_schemas,
            build_dynamic_tool_registry_entries,
            load_published_tools,
        )

        if repository is None or self.tool_registry is None:
            raise RuntimeError("runtime capability dependencies not initialized")

        published_tools = await load_published_tools(repository)
        manifests, descriptors = build_dynamic_tool_registry_entries(
            published_tools,
            base_registry=self._static_tool_registry,
        )
        if len(manifests) != len(published_tools) or len(descriptors) != len(
            published_tools
        ):
            raise RuntimeError(
                "one or more published Tools could not be converted; "
                "runtime generation was not changed"
            )
        candidate_tools = self._static_tool_registry.snapshot()
        candidate_tools.replace_dynamic(
            manifests=manifests,
            descriptors=descriptors,
            dynamic_input_schemas=build_dynamic_input_schemas(published_tools),
        )

        loader = PublishedRuntimeCapabilityLoader(
            repository,
            base_tool_registry=candidate_tools,
        )
        candidate_skills = await loader.load_skills()
        available_tool_ids = set(candidate_tools.list_tool_ids()) | {
            "governance.semantic_query",
            "agent.request_regional_analysis",
        }
        for skill in candidate_skills:
            missing = set(skill.allowed_tool_ids).difference(available_tool_ids)
            if missing:
                raise RuntimeCapabilityDefinitionInvalid(
                    skill.skill_id,
                    "references unavailable Tool: " + ", ".join(sorted(missing)),
                )
        candidate_skill_registry = RuntimeSkillRegistry(candidate_skills)

        candidate_workflows = await loader.load_workflows()
        candidate_workflow_registry = RuntimeWorkflowRegistry(candidate_workflows)
        for workflow in candidate_workflows:
            try:
                candidate_workflow_registry.create_planner(
                    WorkflowRef(
                        workflow_id=workflow.workflow_id,
                        workflow_version=workflow.version,
                    ),
                    tool_registry=candidate_tools,
                    skill_registry=candidate_skill_registry,
                )
            except WorkflowNotAvailable as exc:
                raise RuntimeCapabilityDefinitionInvalid(
                    workflow.workflow_id,
                    str(exc),
                ) from exc
        return candidate_tools, candidate_skills, candidate_workflows, len(manifests)

    def _activate_runtime_capability_candidate(
        self,
        candidate: tuple[
            ToolRegistry,
            tuple[RuntimeSkillContract, ...],
            tuple[RuntimeWorkflowGraphSnapshot, ...],
            int,
        ],
    ) -> int:
        """Atomically switch the in-process registries to a built candidate."""

        if self.tool_registry is None:
            raise RuntimeError("runtime capability dependencies not initialized")
        candidate_tools, candidate_skills, candidate_workflows, manifest_count = candidate
        with self._runtime_capability_state_lock:
            old_tools = self.tool_registry.snapshot()
            old_skills = self.runtime_skill_registry.list()
            old_workflows = self.runtime_workflow_registry.list()
            try:
                self.tool_registry.replace_with(candidate_tools)
                self.runtime_skill_registry.replace(candidate_skills)
                self.runtime_workflow_registry.replace(candidate_workflows)
            except Exception:
                self.tool_registry.replace_with(old_tools)
                self.runtime_skill_registry.replace(old_skills)
                self.runtime_workflow_registry.replace(old_workflows)
                raise

            self.runtime_skills = candidate_skills
            self.runtime_workflows = candidate_workflows
            self.runtime_capability_generation += 1
        logger.info(
            "Activated runtime capability generation %d (%d Tools, %d Skills, %d Workflows)",
            self.runtime_capability_generation,
            manifest_count,
            len(candidate_skills),
            len(candidate_workflows),
        )
        return self.runtime_capability_generation

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
        await runtime.initialize()
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

    # P2: Mount Capability Center routes
    if (
        runtime.capability_management_service
        and runtime.application_management_service
        and runtime.model_config_service
        and runtime.capability_repository
    ):
        capability_router = create_capability_router(
            management_service=runtime.capability_management_service,
            application_management_service=runtime.application_management_service,
            model_config_service=runtime.model_config_service,
            agent_management_service=runtime.agent_management_service,
            connector_connection_tester=ConnectorConnectionTester(
                runtime.capability_repository,
                transport=runtime.connector_connection_transport,
                allowed_private_hosts=runtime.connector_allowed_private_hosts,
            ),
            reload_runtime_capabilities=runtime.reload_runtime_capabilities,
            validate_runtime_transition=(
                runtime.validate_runtime_capability_transition
            ),
            validate_runtime_rollback=runtime.validate_runtime_capability_rollback,
        )
        app.include_router(capability_router)
        app.include_router(create_runtime_observability_router(runtime))
    if runtime.agent_management_service is not None:
        app.include_router(create_agent_router(runtime.agent_management_service))
    if runtime.prompt_template_service is not None:
        prompt_service = runtime.prompt_template_service

        async def refresh_runtime_prompt(app_id: str) -> None:
            runtime.runtime_prompt_registry.activate(
                await prompt_service.get_effective(app_id=app_id),
                app_id=app_id,
            )

        app.include_router(
            create_prompt_router(
                prompt_service,
                refresh_runtime=refresh_runtime_prompt,
            )
        )
    if runtime.knowledge_service is not None:
        app.include_router(create_knowledge_router(runtime.knowledge_service))

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
        elif isinstance(exc, (AuthorizationDenied, CommandClientMismatch)):
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
            (
                AnalysisRequestRejected,
                ConnectorConfigurationInvalid,
                WorkflowNotAvailable,
                InvalidCursor,
                RuntimeCapabilityDefinitionInvalid,
            ),
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
                tenant_id=user.identity.principal.tenant_id,
                user_id=user.user_id,
                app_id="full_information_view",
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
            tenant_id=user.identity.principal.tenant_id,
            user_id=user.user_id,
            app_id="full_information_view",
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
            tenant_id=user.identity.principal.tenant_id,
            user_id=user.user_id,
            app_id="full_information_view",
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
                tenant_id=user.identity.principal.tenant_id,
                user_id=user.user_id,
                app_id="full_information_view",
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
            app.state.runtime.runtime_workflow_registry.create_planner(
                body.workflow_ref,
                tool_registry=app.state.runtime.tool_registry,
                skill_registry=app.state.runtime.runtime_skill_registry,
            )

        natural_replayed = False

        async def operation():
            nonlocal natural_replayed

            async def create_from_message():
                session = await app.state.runtime.service.get_session(
                    tenant_id=user.identity.principal.tenant_id,
                    user_id=user.user_id,
                    app_id="full_information_view",
                    session_id=session_id,
                )
                run = await app.state.runtime.service.create_run(
                    tenant_id=user.identity.principal.tenant_id,
                    user_id=user.user_id,
                    app_id="full_information_view",
                    session_id=session_id,
                    request=body,
                )
                admitted_context = None
                try:
                    admitted_context = await app.state.runtime.admission.admit(
                        identity=user.identity,
                        raw_token=user.raw_token,
                        session_id=session_id,
                        run_id=run.run_id,
                        app_id=session.app_id,
                    )
                    assert app.state.runtime.run_capability_snapshot_service is not None
                    assert app.state.runtime.tool_registry is not None
                    agent_release = None
                    # Resolve and persist the immutable Agent release first;
                    # the resource snapshot below must be derived from it.
                    # A deployment with no published release remains on the
                    # explicit legacy application-level path.
                    if (
                        app.state.runtime.agent_management_service is not None
                        and app.state.runtime._initialized
                    ):
                        try:
                            agent_release = (
                                await app.state.runtime.agent_management_service.bind_run(
                                    run_id=run.run_id,
                                    app_id=session.app_id,
                                    agent_id=admitted_context.application.agent_id,
                                    tenant_id=admitted_context.principal.tenant_id,
                                )
                            )
                        except ResourceNotFound as exc:
                            legacy_unreleased = (
                                admitted_context.application.agent_id
                                == "governance_general_agent"
                                and "no active release" in str(exc)
                            )
                            if not legacy_unreleased:
                                raise
                    await app.state.runtime.run_capability_snapshot_service.create_snapshot_for_run(
                        run_id=run.run_id,
                        base_registry=app.state.runtime.tool_registry,
                        app_id=session.app_id,
                        agent_release=agent_release,
                    )
                except Exception:
                    if admitted_context is not None:
                        await app.state.runtime.credentials.revoke(
                            credential_ref=admitted_context.credential_ref
                        )
                        assert app.state.runtime.auth_contexts is not None
                        await app.state.runtime.auth_contexts.delete(run_id=run.run_id)
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
        await app.state.runtime.service.get_session(
            tenant_id=user.identity.principal.tenant_id,
            user_id=user.user_id,
            app_id="full_information_view",
            session_id=session_id,
        )
        messages = await app.state.runtime.store.list_messages(
            user_id=user.user_id,
            session_id=session_id,
            tenant_id=user.identity.principal.tenant_id,
            app_id="full_information_view",
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
        run = await app.state.runtime.store.get_run(
            tenant_id=user.identity.principal.tenant_id,
            app_id="full_information_view",
            user_id=user.user_id,
            run_id=run_id,
        )
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
            tenant_id=user.identity.principal.tenant_id,
            app_id="full_information_view",
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
            tenant_id=user.identity.principal.tenant_id,
            app_id="full_information_view",
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
        try:
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
            if replayed:
                authoritative = (
                    await app.state.runtime.analysis_plan_repository.get(
                        tenant_id=auth_context.principal.tenant_id,
                        user_id=user.user_id,
                        run_id=run_id,
                        plan_id=plan.plan_id,
                    )
                )
                if authoritative is None or authoritative != plan:
                    raise AnalysisPlanStoreRejected(
                        "PLAN_REPLAY_MISMATCH",
                        "cached plan does not match the server-side authority",
                    )
                plan = authoritative
        except AnalysisPlanStoreRejected as exc:
            raise AnalysisPlanningUnavailable(
                "server-side analysis plan authority rejected the stored plan"
            ) from exc
        except ApplicationError:
            raise
        except Exception as exc:
            raise AnalysisPlanningUnavailable(
                "server-side analysis plan authority is unavailable"
            ) from exc
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
            tenant_id=user.identity.principal.tenant_id,
            app_id="full_information_view",
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
        try:
            plan = await app.state.runtime.analysis_plan_repository.get_latest_for_run(
                tenant_id=auth_context.principal.tenant_id,
                user_id=user.user_id,
                run_id=run_id,
            )
        except AnalysisPlanStoreRejected as exc:
            raise AnalysisPlanningUnavailable(
                "server-side analysis plan authority rejected the stored plan"
            ) from exc
        except Exception as exc:
            raise AnalysisPlanningUnavailable(
                "server-side analysis plan authority is unavailable"
            ) from exc
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
            tenant_id=user.identity.principal.tenant_id,
            app_id="full_information_view",
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
                    tenant_id=user.identity.principal.tenant_id,
                    app_id="full_information_view",
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
                tenant_id=user.identity.principal.tenant_id,
                app_id="full_information_view",
                user_id=user.user_id,
                run_id=run_id,
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
        await app.state.runtime.store.get_run(
            tenant_id=user.identity.principal.tenant_id,
            app_id="full_information_view",
            user_id=user.user_id,
            run_id=run_id,
        )

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
                tenant_id=user.identity.principal.tenant_id,
                app_id="full_information_view",
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
                tenant_id=user.identity.principal.tenant_id,
                app_id="full_information_view",
                user_id=user.user_id,
                run_id=run_id,
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
            tenant_id=user.identity.principal.tenant_id,
            app_id="full_information_view",
            user_id=user.user_id,
            run_id=run_id,
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
        await app.state.runtime.store.get_run(
            tenant_id=user.identity.principal.tenant_id,
            app_id="full_information_view",
            user_id=user.user_id,
            run_id=run_id,
        )

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
        await app.state.runtime.store.get_run(
            tenant_id=user.identity.principal.tenant_id,
            app_id="full_information_view",
            user_id=user.user_id,
            run_id=run_id,
        )
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
        await app.state.runtime.store.get_run(
            tenant_id=user.identity.principal.tenant_id,
            app_id="full_information_view",
            user_id=user.user_id,
            run_id=run_id,
        )
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
            tenant_id=user.identity.principal.tenant_id,
            app_id="full_information_view",
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
            tenant_id=user.identity.principal.tenant_id,
            app_id="full_information_view",
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

    @app.get("/agent-api/v1/results/{result_id}/download")
    async def download_result(
        result_id: str,
        user: Annotated[CurrentUser, Depends(require_geotoken)],
        format: Literal["csv"] = "csv",
    ) -> Response:
        result = await app.state.runtime.store.get_result(
            tenant_id=user.identity.principal.tenant_id,
            app_id="full_information_view",
            user_id=user.user_id,
            result_id=result_id,
        )
        if result.payload_expires_at <= datetime.now(UTC):
            raise ResultPayloadExpired("result payload is no longer available")
        if not isinstance(result, TableDataResult):
            raise ResourceNotFound("result download is not available")
        csv_text = _table_result_csv(result)
        return Response(
            content="\ufeff" + csv_text,
            media_type="text/csv",
            headers={"Content-Disposition": 'attachment; filename="result.csv"'},
        )

    @app.get("/agent-api/v1/evidence/{evidence_id}")
    async def get_evidence(
        evidence_id: str,
        user: Annotated[CurrentUser, Depends(require_geotoken)],
    ) -> EvidenceResponse:
        evidence = await app.state.runtime.store.get_evidence(
            tenant_id=user.identity.principal.tenant_id,
            app_id="full_information_view",
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


def _table_result_csv(result: TableDataResult) -> str:
    rows = [
        row if isinstance(row, dict) else row.model_dump(mode="json")
        for row in result.data.rows
    ]
    field_names: list[str] = []
    for row in rows:
        for field_name in row:
            if field_name not in field_names:
                field_names.append(field_name)
    display_labels = {
        field.field: field.label
        for field in (result.presentation.fields if result.presentation else [])
    }
    export_names: dict[str, str] = {}
    used_labels: set[str] = set()
    for field_name in field_names:
        label = display_labels.get(field_name, field_name)
        if label in used_labels:
            label = f"{label}（{field_name}）"
        export_names[field_name] = label
        used_labels.add(label)
    output = io.StringIO(newline="")
    writer = csv.DictWriter(
        output,
        fieldnames=[export_names[field_name] for field_name in field_names],
        extrasaction="ignore",
    )
    if field_names:
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    export_names[field_name]: _csv_safe_cell(row.get(field_name))
                    for field_name in field_names
                }
            )
    return output.getvalue()


def _csv_safe_cell(value: object) -> object:
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


app = create_app()
