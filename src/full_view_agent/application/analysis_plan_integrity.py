"""AnalysisPlan 指纹的唯一实现。

该 canonical SHA 是内容地址/幂等键，不是签名或授权凭据。
"""

from collections.abc import Sequence

from full_view_agent.application.fingerprints import canonical_fingerprint
from full_view_agent.domain.analysis_plan import (
    AnalysisGoal,
    AnalysisOmission,
    AnalysisPlan,
    AnalysisScopeRef,
    AnalysisStep,
    PlanBudget,
)


def compute_analysis_plan_id(
    *,
    schema_version: str,
    catalog_version: str,
    catalog_fingerprint: str,
    request_id: str,
    goals: Sequence[AnalysisGoal],
    scope_ref: AnalysisScopeRef,
    steps: Sequence[AnalysisStep],
    omissions: Sequence[AnalysisOmission],
    constraints: PlanBudget,
) -> str:
    """从计划的全部可执行语义生成 canonical ID。"""
    return canonical_fingerprint(
        domain="analysis-plan:1.0",
        value={
            "schema_version": schema_version,
            "catalog_version": catalog_version,
            "catalog_fingerprint": catalog_fingerprint,
            "request_id": request_id,
            "goals": list(goals),
            "scope_ref": scope_ref.model_dump(mode="json"),
            "steps": [step.model_dump(mode="json") for step in steps],
            "omissions": [
                omission.model_dump(mode="json") for omission in omissions
            ],
            "constraints": constraints.model_dump(mode="json"),
        },
    )


def recompute_analysis_plan_id(plan: AnalysisPlan) -> str:
    """重算已构造计划的 ID，供执行边界校验完整性。"""
    return compute_analysis_plan_id(
        schema_version=plan.schema_version,
        catalog_version=plan.catalog_version,
        catalog_fingerprint=plan.catalog_fingerprint,
        request_id=plan.request_id,
        goals=plan.goals,
        scope_ref=plan.scope_ref,
        steps=plan.steps,
        omissions=plan.omissions,
        constraints=plan.constraints,
    )
