from full_view_agent.application.errors import (
    AuthenticationFailed,
    CredentialUnavailable,
    ReauthenticationRequired,
)
from full_view_agent.application.ports import CredentialBroker, LegacyIdentityPort
from full_view_agent.application.run_admission import RunAdmissionService
from full_view_agent.domain.models import AuthContext


class RunAuthContextRefresher:
    def __init__(
        self,
        *,
        credential_broker: CredentialBroker,
        identity_port: LegacyIdentityPort,
        admission: RunAdmissionService,
    ) -> None:
        self._credential_broker = credential_broker
        self._identity_port = identity_port
        self._admission = admission

    async def refresh(self, auth_context: AuthContext) -> AuthContext:
        try:
            raw_token = await self._credential_broker.resolve(
                credential_ref=auth_context.credential_ref,
                subject_user_id=auth_context.principal.user_id,
                app_id=auth_context.application.app_id,
                run_id=auth_context.run_id,
            )
            identity = await self._identity_port.resolve(raw_token)
        except (AuthenticationFailed, CredentialUnavailable) as exc:
            raise ReauthenticationRequired("登录凭据已失效，请重新认证") from exc
        if identity.principal.user_id != auth_context.principal.user_id:
            raise AuthenticationFailed("refreshed identity does not match the active run")
        refreshed = await self._admission.admit(
            identity=identity,
            raw_token=raw_token,
            session_id=auth_context.session_id,
            run_id=auth_context.run_id,
        )
        await self._credential_broker.revoke(
            credential_ref=auth_context.credential_ref,
        )
        return refreshed
