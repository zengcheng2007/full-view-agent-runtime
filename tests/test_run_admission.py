import pytest
from pydantic import SecretStr, ValidationError

from full_view_agent.domain.models import LegacyIdentitySnapshot, Principal
from full_view_agent.infrastructure.credential_broker import InMemoryCredentialBroker
from full_view_agent.infrastructure.legacy_identity import HashedLegacyIdentityAdapter


@pytest.mark.asyncio
async def test_run_admission_builds_and_stores_immutable_auth_context() -> None:
    from full_view_agent.application.run_admission import RunAdmissionService
    from full_view_agent.infrastructure.auth_context_store import (
        InMemoryRunAuthContextStore,
    )

    raw_token = SecretStr("admission-token-user-01")
    identity = await HashedLegacyIdentityAdapter().resolve(raw_token)
    broker = InMemoryCredentialBroker(default_ttl_seconds=300)
    auth_store = InMemoryRunAuthContextStore()
    admission = RunAdmissionService(
        credential_broker=broker,
        auth_context_store=auth_store,
    )

    auth_context = await admission.admit(
        identity=identity,
        raw_token=raw_token,
        session_id="session-01",
        run_id="run-01",
    )
    stored = await auth_store.get(
        user_id=identity.principal.user_id,
        run_id="run-01",
    )

    assert stored == auth_context
    assert auth_context.credential_ref.startswith("cred_")
    assert "governance.population.aggregate.read" in auth_context.entitlements
    assert "governance.housing.aggregate.read" in auth_context.entitlements
    assert "governance.event.aggregate.read" in auth_context.entitlements
    assert "governance.overview.aggregate.read" in auth_context.entitlements
    assert "governance.enterprise.aggregate.read" in auth_context.entitlements
    assert "governance_overview" in auth_context.data_scopes.datasets
    assert "enterprise" in auth_context.data_scopes.datasets
    assert "housing" in auth_context.data_scopes.datasets
    assert "event" in auth_context.data_scopes.datasets
    assert auth_context.data_scopes.areas[0].area_code == "330106"
    assert "admission-token-user-01" not in auth_context.model_dump_json()
    with pytest.raises(ValidationError):
        auth_context.purpose = "registered_workflow"


@pytest.mark.asyncio
async def test_run_admission_uses_explicit_p0_user_allowlist_not_numeric_role_meaning() -> None:
    from datetime import UTC, datetime, timedelta

    from full_view_agent.application.run_admission import RunAdmissionService
    from full_view_agent.infrastructure.auth_context_store import (
        InMemoryRunAuthContextStore,
    )

    identity = LegacyIdentitySnapshot(
        principal=Principal(
            tenant_id="legacy",
            user_id="legacy-user-02",
            org_id="legacy-org",
            roles=["2"],
        ),
        source="legacy_geo_gateway",
        source_session_expires_at=datetime.now(UTC) + timedelta(minutes=5),
        base_area_codes=["330106"],
    )
    admission = RunAdmissionService(
        credential_broker=InMemoryCredentialBroker(),
        auth_context_store=InMemoryRunAuthContextStore(),
        p0_allowed_user_ids={"legacy-user-02"},
    )

    auth_context = await admission.admit(
        identity=identity,
        raw_token=SecretStr("legacy-token"),
        session_id="session-allowlist",
        run_id="run-allowlist",
    )

    assert "governance.population.aggregate.read" in auth_context.entitlements
    assert "governance.housing.aggregate.read" in auth_context.entitlements
    assert "governance.event.aggregate.read" in auth_context.entitlements
    assert "housing" in auth_context.data_scopes.datasets
    assert "event" in auth_context.data_scopes.datasets
    assert auth_context.data_scopes.areas[0].area_code == "330106"


@pytest.mark.asyncio
async def test_run_admission_revokes_issued_credential_when_auth_context_store_fails() -> None:
    from full_view_agent.application.run_admission import RunAdmissionService

    class FailingAuthContextStore:
        async def put(self, _auth_context):
            raise RuntimeError("auth context persistence failed")

    raw_token = SecretStr("admission-token-revocation")
    identity = await HashedLegacyIdentityAdapter().resolve(raw_token)
    broker = InMemoryCredentialBroker()
    admission = RunAdmissionService(
        credential_broker=broker,
        auth_context_store=FailingAuthContextStore(),
    )

    with pytest.raises(RuntimeError, match="auth context persistence failed"):
        await admission.admit(
            identity=identity,
            raw_token=raw_token,
            session_id="session-revocation",
            run_id="run-revocation",
        )

    assert broker._credentials == {}
