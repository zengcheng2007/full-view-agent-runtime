"""Server-side application to identity-adapter binding.

Callers must first load the application from the trusted registry or bind it to
a fixed route.  This component intentionally does not accept a client supplied
``app_id`` and therefore cannot be used to switch applications by header.
"""

from collections.abc import Mapping
from types import MappingProxyType

from pydantic import ConfigDict, SecretStr

from full_view_agent.application.errors import ApplicationError
from full_view_agent.application.ports import LegacyIdentityPort
from full_view_agent.domain.application import AgentApplicationDefinition
from full_view_agent.domain.contract_model import ContractModel
from full_view_agent.domain.models import LegacyIdentitySnapshot


class ApplicationIdentityAdapterUnavailable(ApplicationError):
    code = "application_identity_adapter_unavailable"


class TrustedApplicationIdentityContext(ContractModel):
    """Identity resolved for one server-selected application."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    app_id: str
    agent_id: str
    identity_adapter_id: str
    identity: LegacyIdentitySnapshot


class ApplicationIdentityAdapterRegistry:
    """Resolve application identities only through explicitly bound adapters."""

    def __init__(self, adapters: Mapping[str, LegacyIdentityPort]) -> None:
        self._adapters = MappingProxyType(dict(adapters))

    async def resolve_for_application(
        self,
        *,
        application: AgentApplicationDefinition,
        raw_token: SecretStr,
    ) -> TrustedApplicationIdentityContext:
        if application.status != "active":
            raise ApplicationIdentityAdapterUnavailable(
                f"application is {application.status}"
            )
        adapter = self._adapters.get(application.identity_adapter_id)
        if adapter is None:
            raise ApplicationIdentityAdapterUnavailable(
                "application identity adapter is not registered"
            )
        identity = await adapter.resolve(raw_token)
        return TrustedApplicationIdentityContext(
            app_id=application.app_id,
            agent_id=application.default_agent_id,
            identity_adapter_id=application.identity_adapter_id,
            identity=identity,
        )
