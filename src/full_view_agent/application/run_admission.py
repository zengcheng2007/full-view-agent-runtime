from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Protocol

from pydantic import SecretStr

from full_view_agent.application.fingerprints import canonical_fingerprint
from full_view_agent.application.session_run_service import new_id
from full_view_agent.domain.application import AgentApplicationDefinition
from full_view_agent.domain.models import (
    AuthContext,
    CredentialGrant,
    LegacyIdentitySnapshot,
)


class CredentialIssuer(Protocol):
    async def issue(
        self,
        *,
        raw_token: SecretStr,
        subject_user_id: str,
        app_id: str,
        run_id: str,
        source_expires_at: datetime,
    ) -> CredentialGrant: ...

    async def revoke(self, *, credential_ref: str) -> None: ...


class AuthContextWriter(Protocol):
    async def put(self, auth_context: AuthContext) -> AuthContext: ...


class ApplicationRegistryReader(Protocol):
    async def get_application(
        self, app_id: str
    ) -> AgentApplicationDefinition | None: ...


class _FullViewCompatibilityRegistry:
    """Temporary compatibility boundary until callers always supply an app."""

    async def get_application(
        self, app_id: str
    ) -> AgentApplicationDefinition | None:
        if app_id != "full_information_view":
            return None
        return AgentApplicationDefinition(
            app_id="full_information_view",
            name="全量信息视图",
            default_agent_id="governance_general_agent",
            identity_adapter_id="identity.legacy_geo",
        )


class RunAdmissionService:
    def __init__(
        self,
        *,
        credential_broker: CredentialIssuer,
        auth_context_store: AuthContextWriter,
        application_registry: ApplicationRegistryReader | None = None,
        p0_allowed_user_ids: set[str] | frozenset[str] | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._credential_broker = credential_broker
        self._auth_context_store = auth_context_store
        self._application_registry = (
            application_registry or _FullViewCompatibilityRegistry()
        )
        self._p0_allowed_user_ids = frozenset(p0_allowed_user_ids or ())
        self._now = now or (lambda: datetime.now(UTC))

    async def admit(
        self,
        *,
        identity: LegacyIdentitySnapshot,
        raw_token: SecretStr,
        session_id: str,
        run_id: str,
        app_id: str = "full_information_view",
    ) -> AuthContext:
        application = await self._application_registry.get_application(app_id)
        if application is None or application.status != "active":
            from full_view_agent.application.errors import ResourceNotFound

            raise ResourceNotFound("application is not available")
        now = self._now()
        grant = await self._credential_broker.issue(
            raw_token=raw_token,
            subject_user_id=identity.principal.user_id,
            app_id=application.app_id,
            run_id=run_id,
            source_expires_at=identity.source_session_expires_at,
        )
        entitlements, datasets = _authorization_profile(
            identity,
            p0_allowed_user_ids=self._p0_allowed_user_ids,
        )
        claims = {
            "principal": identity.principal.model_dump(mode="json"),
            "application": {
                "app_id": application.app_id,
                "agent_id": application.default_agent_id,
            },
            "entitlements": entitlements,
            "data_scopes": {
                "areas": [
                    {"area_code": area_code, "include_descendants": True}
                    for area_code in identity.base_area_codes
                ],
                "datasets": datasets,
                "field_policy_set": "governance_analyst_v1",
            },
            "purpose": "interactive_analysis",
            "session_id": session_id,
            "run_id": run_id,
            "credential_ref": grant.credential_ref,
            "issued_at": now,
            "expires_at": min(
                identity.source_session_expires_at,
                grant.expires_at,
                now + timedelta(minutes=5),
            ),
            "policy_version": "fixture-policy-v1",
        }
        auth_context = AuthContext.model_validate(
            {
                "auth_context_id": new_id("authctx"),
                "auth_context_fingerprint": canonical_fingerprint(
                    domain="auth-context:1.1",
                    value=claims,
                ),
                **claims,
            }
        )
        try:
            return await self._auth_context_store.put(auth_context)
        except Exception:
            await self._credential_broker.revoke(
                credential_ref=grant.credential_ref,
            )
            raise


def _authorization_profile(
    identity: LegacyIdentitySnapshot,
    *,
    p0_allowed_user_ids: frozenset[str] = frozenset(),
) -> tuple[list[str], list[str]]:
    is_test_fixture = (
        identity.source == "legacy_geo_user_fixture"
        and "governance_analyst" in identity.principal.roles
    )
    if identity.principal.user_id not in p0_allowed_user_ids and not is_test_fixture:
        return [], []
    return (
        [
            "governance.area.read",
            "governance.event.aggregate.read",
            "governance.enterprise.aggregate.read",
            "governance.overview.aggregate.read",
            "governance.power.aggregate.read",
            "governance.housing.aggregate.read",
            "governance.population.aggregate.read",
            "governance.object.profile.read",
            "knowledge.search",
        ],
        [
            "administrative_area",
            "event",
            "enterprise",
            "governance_overview",
            "governance_power",
            "housing",
            "population",
            "governance_objects",
            "knowledge",
        ],
    )
