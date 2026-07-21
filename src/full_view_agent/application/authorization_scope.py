from typing import Protocol, runtime_checkable

from pydantic import BaseModel

from full_view_agent.domain.models import MetricQueryScope


@runtime_checkable
class AreaScopedContract(Protocol):
    def authorization_area_scope(self) -> MetricQueryScope | None: ...


def extract_area_scope(value: BaseModel | None) -> MetricQueryScope | None:
    if value is None or not isinstance(value, AreaScopedContract):
        return None
    return value.authorization_area_scope()


def area_is_within_scope(area_code: str, scope: MetricQueryScope) -> bool:
    return area_code == scope.area_code or (
        scope.include_descendants and area_code.startswith(scope.area_code)
    )
