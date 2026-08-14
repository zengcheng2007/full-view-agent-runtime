import pytest

from full_view_agent.application.control_plane_authorization import (
    ALL_CONTROL_PLANE_PERMISSIONS,
    ControlPlaneAuthorizer,
    ControlPlanePermission,
)
from full_view_agent.application.errors import AuthorizationDenied
from full_view_agent.domain.models import LegacyIdentitySnapshot, Principal


def _identity(*roles: str) -> LegacyIdentitySnapshot:
    from datetime import UTC, datetime, timedelta

    return LegacyIdentitySnapshot(
        principal=Principal(
            tenant_id="platform",
            user_id="control-user",
            org_id="control-org",
            roles=list(roles),
        ),
        source="control-test",
        source_session_expires_at=datetime.now(UTC) + timedelta(minutes=5),
        base_area_codes=[],
    )


def test_legacy_admin_role_is_mapped_once_to_explicit_control_permissions() -> None:
    principal = ControlPlaneAuthorizer.compatibility_default().resolve(
        _identity("admin")
    )

    assert principal.permissions == ALL_CONTROL_PLANE_PERMISSIONS
    assert principal.source_roles == ("admin",)


def test_business_role_has_no_implicit_control_plane_read_access() -> None:
    authorizer = ControlPlaneAuthorizer.compatibility_default()

    with pytest.raises(AuthorizationDenied, match="control-plane permission"):
        authorizer.require(
            _identity("governance_analyst"),
            ControlPlanePermission.CAPABILITY_READ,
        )


def test_custom_role_mapping_can_grant_read_without_management() -> None:
    authorizer = ControlPlaneAuthorizer(
        role_permissions={
            "capability_auditor": {ControlPlanePermission.CAPABILITY_READ}
        }
    )
    identity = _identity("capability_auditor")

    authorizer.require(identity, ControlPlanePermission.CAPABILITY_READ)
    with pytest.raises(AuthorizationDenied):
        authorizer.require(identity, ControlPlanePermission.CAPABILITY_MANAGE)


def test_reviewer_permission_is_separate_from_capability_management() -> None:
    authorizer = ControlPlaneAuthorizer(
        role_permissions={
            "capability_reviewer": {ControlPlanePermission.CAPABILITY_APPROVE},
            "capability_editor": {ControlPlanePermission.CAPABILITY_MANAGE},
        }
    )

    authorizer.require(
        _identity("capability_reviewer"),
        ControlPlanePermission.CAPABILITY_APPROVE,
    )
    with pytest.raises(AuthorizationDenied):
        authorizer.require(
            _identity("capability_editor"),
            ControlPlanePermission.CAPABILITY_APPROVE,
        )


def test_openapi_documents_bearer_or_legacy_geotoken_for_control_plane() -> None:
    from full_view_agent.api.app import RuntimeContainer, create_app

    openapi = create_app(RuntimeContainer()).openapi()
    schemes = openapi["components"]["securitySchemes"]
    operation = openapi["paths"]["/capability-api/v1/applications"]["get"]

    assert schemes["BearerAuth"] == {
        "type": "http",
        "scheme": "bearer",
    }
    assert {tuple(item) for item in operation["security"]} == {
        ("GeoToken",),
        ("BearerAuth",),
    }
    assert "ControlBearer" not in schemes
    for path, path_item in openapi["paths"].items():
        if not path.startswith("/capability-api/v1/"):
            continue
        for method in ("get", "post", "patch", "put", "delete"):
            if method not in path_item:
                continue
            assert {tuple(item) for item in path_item[method]["security"]} == {
                ("GeoToken",),
                ("BearerAuth",),
            }


@pytest.mark.parametrize(
    "field,value",
    [
        ("timeout_seconds", 4),
        ("timeout_seconds", 601),
        ("max_output_tokens", 99),
        ("max_output_tokens", 128001),
        ("max_retries", -1),
        ("max_retries", 6),
    ],
)
def test_model_config_api_rejects_values_outside_runtime_limits(
    field: str, value: int
) -> None:
    from pydantic import ValidationError

    from full_view_agent.api.capability_routes import ModelConfigCreateBody

    payload = {
        "name": "test",
        "api_base_url": "https://model.example/v1",
        "api_key": "secret",
        "model_name": "model",
        field: value,
    }
    with pytest.raises(ValidationError):
        ModelConfigCreateBody.model_validate(payload)
