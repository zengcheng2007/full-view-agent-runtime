import httpx
from pydantic import SecretStr

from full_view_agent.application.capability_service import ToolAdapter
from full_view_agent.application.run_admission import RunAdmissionService
from full_view_agent.domain.models import AuthContext, LegacyIdentitySnapshot
from full_view_agent.evaluation.contracts import EvalCase
from full_view_agent.infrastructure.auth_context_store import InMemoryRunAuthContextStore
from full_view_agent.infrastructure.credential_broker import InMemoryCredentialBroker
from full_view_agent.infrastructure.governance_adapter import HttpGovernanceAdapter
from full_view_agent.infrastructure.legacy_identity import HttpLegacyIdentityAdapter


class HttpEvalEnvironment:
    """One-shot evaluation environment backed by the existing HTTP services."""

    def __init__(
        self,
        *,
        raw_token: SecretStr,
        legacy_gateway_url: str,
        governance_base_url: str,
        p0_allowed_user_ids: set[str] | frozenset[str],
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._raw_token = raw_token
        self._client = client or httpx.AsyncClient(follow_redirects=False)
        self._identity_adapter = HttpLegacyIdentityAdapter(
            base_url=legacy_gateway_url,
            client=self._client,
        )
        self._credential_broker = InMemoryCredentialBroker()
        self._auth_context_store = InMemoryRunAuthContextStore()
        self._admission = RunAdmissionService(
            credential_broker=self._credential_broker,
            auth_context_store=self._auth_context_store,
            p0_allowed_user_ids=p0_allowed_user_ids,
        )
        self._adapter = HttpGovernanceAdapter(
            base_url=governance_base_url,
            credential_broker=self._credential_broker,
            client=self._client,
        )
        self._identity: LegacyIdentitySnapshot | None = None

    @property
    def adapter(self) -> ToolAdapter:
        return self._adapter

    @property
    def evidence_source_system(self) -> str:
        return "legacy_geo_qxst"

    async def resolve_user_id(self, case: EvalCase) -> str:
        del case
        identity = await self._resolve_identity()
        return identity.principal.user_id

    async def build_auth_context(
        self,
        *,
        case: EvalCase,
        user_id: str,
        session_id: str,
        run_id: str,
    ) -> AuthContext:
        del case
        identity = await self._resolve_identity()
        if identity.principal.user_id != user_id:
            raise RuntimeError("resolved legacy identity changed during evaluation")
        return await self._admission.admit(
            identity=identity,
            raw_token=self._raw_token,
            session_id=session_id,
            run_id=run_id,
        )

    async def aclose(self) -> None:
        if self._identity is not None:
            await self._credential_broker.revoke_subject(
                subject_user_id=self._identity.principal.user_id
            )
        self._raw_token = SecretStr("")
        await self._client.aclose()

    async def _resolve_identity(self) -> LegacyIdentitySnapshot:
        if self._identity is None:
            self._identity = await self._identity_adapter.resolve(self._raw_token)
        return self._identity
