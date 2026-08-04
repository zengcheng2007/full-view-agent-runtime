"""Application port and fail-closed validation for trusted analysis plans."""

from __future__ import annotations

from typing import Protocol

from pydantic import ValidationError

from full_view_agent.application.analysis_plan_integrity import (
    recompute_analysis_plan_id,
)
from full_view_agent.application.fingerprints import canonical_fingerprint
from full_view_agent.domain.analysis_plan import AnalysisPlan


class AnalysisPlanStoreRejected(Exception):
    """A plan cannot safely be written to or read from the authority store."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"analysis plan store rejected [{code}]: {message}")


class AnalysisPlanRepository(Protocol):
    """Server-side authority for content-addressed ``AnalysisPlan`` objects."""

    async def save(
        self,
        *,
        tenant_id: str,
        user_id: str,
        run_id: str,
        plan: AnalysisPlan,
    ) -> AnalysisPlan: ...

    async def get(
        self,
        *,
        tenant_id: str,
        user_id: str,
        run_id: str,
        plan_id: str,
    ) -> AnalysisPlan | None: ...

    async def get_latest_for_run(
        self,
        *,
        tenant_id: str,
        user_id: str,
        run_id: str,
    ) -> AnalysisPlan | None: ...


def analysis_plan_namespace(
    *, tenant_id: str, user_id: str, run_id: str, plan_id: str
) -> str:
    """Derive an opaque namespace from every isolation key component."""
    return canonical_fingerprint(
        domain="analysis-plan-namespace:1.0",
        value={
            "tenant_id": tenant_id,
            "user_id": user_id,
            "run_id": run_id,
            "plan_id": plan_id,
        },
    )


def validate_plan_for_save(plan: AnalysisPlan) -> AnalysisPlan:
    """Revalidate the contract and its content-derived identity before storage."""
    try:
        validated = AnalysisPlan.model_validate(
            plan.model_dump(mode="python", warnings="none")
        )
    except (AttributeError, TypeError, ValidationError) as exc:
        raise AnalysisPlanStoreRejected(
            "PLAN_CONTRACT_INVALID", "plan does not satisfy the AnalysisPlan contract"
        ) from exc
    if validated.plan_id != recompute_analysis_plan_id(validated):
        raise AnalysisPlanStoreRejected(
            "PLAN_ID_MISMATCH", "plan id does not match canonical plan content"
        )
    return validated


def validate_stored_plan(
    *,
    plan_json: str,
    expected_plan_id: str,
    stored_request_id: str,
    stored_catalog_version: str,
    stored_catalog_fingerprint: str,
) -> AnalysisPlan:
    """Decode a stored record and verify redundant authority columns."""
    try:
        plan = AnalysisPlan.model_validate_json(plan_json)
    except (TypeError, ValueError, ValidationError) as exc:
        raise AnalysisPlanStoreRejected(
            "PLAN_JSON_INVALID", "stored plan JSON is invalid"
        ) from exc
    if plan.plan_id != expected_plan_id or plan.plan_id != recompute_analysis_plan_id(plan):
        raise AnalysisPlanStoreRejected(
            "PLAN_ID_MISMATCH", "stored plan id does not match key or canonical content"
        )
    if plan.request_id != stored_request_id:
        raise AnalysisPlanStoreRejected(
            "PLAN_REQUEST_MISMATCH", "stored request id does not match plan content"
        )
    if (
        plan.catalog_version != stored_catalog_version
        or plan.catalog_fingerprint != stored_catalog_fingerprint
    ):
        raise AnalysisPlanStoreRejected(
            "PLAN_CATALOG_MISMATCH", "stored catalog metadata does not match plan content"
        )
    return plan
