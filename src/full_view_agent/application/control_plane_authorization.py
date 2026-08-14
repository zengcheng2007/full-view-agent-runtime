from collections.abc import Mapping, Set
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType

from full_view_agent.application.errors import AuthorizationDenied
from full_view_agent.domain.models import LegacyIdentitySnapshot


class ControlPlanePermission(StrEnum):
    CAPABILITY_READ = "capability.read"
    CAPABILITY_MANAGE = "capability.manage"
    CAPABILITY_APPROVE = "capability.approve"
    CAPABILITY_PUBLISH = "capability.publish"
    APPLICATION_MANAGE = "application.manage"
    APPLICATION_BIND = "application.bind"
    MODEL_MANAGE = "model.manage"


ALL_CONTROL_PLANE_PERMISSIONS = frozenset(ControlPlanePermission)


@dataclass(frozen=True)
class ControlPlanePrincipal:
    user_id: str
    tenant_id: str
    permissions: frozenset[ControlPlanePermission]
    source_roles: tuple[str, ...]


class ControlPlaneAuthorizer:
    """Maps trusted identity roles to explicit control-plane permissions.

    Role interpretation is isolated here so routes and services authorize a
    stable permission instead of repeatedly inferring administrator meaning.
    """

    def __init__(
        self,
        *,
        role_permissions: Mapping[str, Set[ControlPlanePermission]],
    ) -> None:
        self._role_permissions = MappingProxyType(
            {
                role: frozenset(permissions)
                for role, permissions in role_permissions.items()
            }
        )

    @classmethod
    def compatibility_default(cls) -> "ControlPlaneAuthorizer":
        return cls(
            role_permissions={
                role: ALL_CONTROL_PLANE_PERMISSIONS
                for role in ("admin", "super_admin", "role_1", "1")
            }
        )

    def resolve(self, identity: LegacyIdentitySnapshot) -> ControlPlanePrincipal:
        roles = tuple(identity.principal.roles)
        permissions = frozenset(
            permission
            for role in roles
            for permission in self._role_permissions.get(role, ())
        )
        return ControlPlanePrincipal(
            user_id=identity.principal.user_id,
            tenant_id=identity.principal.tenant_id,
            permissions=permissions,
            source_roles=roles,
        )

    def require(
        self,
        identity: LegacyIdentitySnapshot,
        permission: ControlPlanePermission,
    ) -> ControlPlanePrincipal:
        principal = self.resolve(identity)
        if permission not in principal.permissions:
            raise AuthorizationDenied(
                f"control-plane permission required: {permission.value}"
            )
        return principal
