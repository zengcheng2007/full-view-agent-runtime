"""权限越权矩阵测试：两用户（A 有权限，B 无权限）三维度 fail-closed 验证。

测试维度：
1. 区划越权：B 用户无某区划权限时，查询该区划必须被 deny
2. 敏感字段越权：B 用户无 contact 权限时，请求 contact 字段集必须被 deny
3. 数据集越权：B 用户无某数据集授权时，查询必须被 deny

每个维度均验证：
- A 用户可以正常执行（allow）
- B 用户被 fail-closed 拒绝（status=denied）
- 拒绝原因码正确
"""

from __future__ import annotations

import pytest

from full_view_agent.application.capability_service import CapabilityService
from full_view_agent.application.policy import MinimalPolicyAdapter
from full_view_agent.application.tool_registry import ToolRegistry
from full_view_agent.domain import models

from .test_capability_service import StaticAuthContextRefresher
from .test_policy import population_auth_context


class _AllowAdapter:
    """A permissive adapter that returns a minimal valid DataResult.

    Used to verify that user A's allowed requests actually reach the adapter,
    while user B's requests are denied before reaching it.
    """

    def __init__(self) -> None:
        self.calls = 0

    async def execute(self, *, manifest: models.InternalToolManifest, **_kwargs):
        self.calls += 1
        return models.TableDataResult(
            result_id=f"res-allow-{manifest.tool_id}",
            data_schema_ref=f"schema://data/{manifest.tool_id}/1.0.0",
            result_fingerprint=f"sha256:allow-{manifest.tool_id}",
            data=models.DynamicTableData(rows=[]),
            row_count=0,
            truncated=False,
        )


class _DenyGateAdapter:
    """Adapter that raises if called — used to assert B user never reaches it."""

    def __init__(self) -> None:
        self.calls = 0

    async def execute(self, **_kwargs):
        self.calls += 1
        raise AssertionError("denied capability must not call its adapter")


def _make_auth_context(
    *,
    user_id: str,
    areas: list[dict[str, object]],
    datasets: list[str],
    entitlements: list[str],
    field_policy_set: str = "governance_analyst_v1",
) -> models.AuthContext:
    base = population_auth_context()
    return base.model_copy(
        update={
            "auth_context_id": f"authctx-{user_id}",
            "auth_context_fingerprint": f"sha256:auth-context-{user_id}",
            "principal": base.principal.model_copy(update={"user_id": user_id}),
            "entitlements": entitlements,
            "data_scopes": models.AuthDataScopes(
                areas=[
                    models.AuthorizedAreaScope.model_validate(area) for area in areas
                ],
                datasets=datasets,
                field_policy_set=field_policy_set,
            ),
            "session_id": f"session-{user_id}",
            "run_id": f"run-{user_id}",
            "credential_ref": f"cred-{user_id}",
        }
    )


def _build_capability(
    auth_context: models.AuthContext,
    adapter: object | None = None,
) -> CapabilityService:
    return CapabilityService(
        registry=ToolRegistry.default(),
        policy=MinimalPolicyAdapter(),
        adapter=adapter if adapter is not None else _AllowAdapter(),
        auth_context_refresher=StaticAuthContextRefresher(auth_context),
    )


# ---------- 维度 1：区划越权 ----------


@pytest.mark.asyncio
async def test_area_escalation_user_a_allowed_user_b_denied() -> None:
    """A 用户有 330106 区划权限，B 用户无；B 查询 330106 必须 fail-closed。"""
    user_a = _make_auth_context(
        user_id="user-a-area",
        areas=[{"area_code": "330106", "include_descendants": True}],
        datasets=["population"],
        entitlements=["governance.population.aggregate.read"],
    )
    user_b = _make_auth_context(
        user_id="user-b-area",
        areas=[{"area_code": "330108", "include_descendants": True}],
        datasets=["population"],
        entitlements=["governance.population.aggregate.read"],
    )

    arguments = models.QueryPopulationMetricsInput.model_validate(
        {
            "query": {
                "metrics": ["person_count"],
                "scope": {"area_code": "330106"},
            }
        }
    )

    # A 用户：区划在授权范围内 → allow
    cap_a = _build_capability(user_a)
    result_a = await cap_a.execute(
        tool_call_id="tcl-area-a",
        tool_id="governance.query_population_metrics",
        raw_arguments=arguments.model_dump(),
        auth_context=user_a,
    )
    assert result_a.policy.decision == "allow"

    # B 用户：区划不在授权范围内 → deny + AREA_OUT_OF_SCOPE
    cap_b = _build_capability(user_b, adapter=_DenyGateAdapter())
    result_b = await cap_b.execute(
        tool_call_id="tcl-area-b",
        tool_id="governance.query_population_metrics",
        raw_arguments=arguments.model_dump(),
        auth_context=user_b,
    )
    assert result_b.status == "denied"
    assert result_b.policy.decision == "deny"
    assert "AREA_OUT_OF_SCOPE" in result_b.warnings


# ---------- 维度 2：敏感字段越权 ----------


@pytest.mark.asyncio
async def test_sensitive_field_escalation_user_a_allowed_user_b_denied() -> None:
    """A 用户有 contact 权限，B 用户无；B 请求 contact 字段集必须 fail-closed。

    分两个子场景：
    - B 混合请求 [summary, contact]：decision=mask，contact 字段被裁剪
    - B 仅请求 [contact]：decision=deny，整个请求被拒绝
    """
    base_entitlements = ["governance.object.profile.read"]
    user_a = _make_auth_context(
        user_id="user-a-field",
        areas=[{"area_code": "330106", "include_descendants": True}],
        datasets=["governance_objects"],
        entitlements=base_entitlements + ["governance.object.contact.read"],
    )
    user_b = _make_auth_context(
        user_id="user-b-field",
        areas=[{"area_code": "330106", "include_descendants": True}],
        datasets=["governance_objects"],
        entitlements=base_entitlements,  # 无 contact 权限
    )

    # --- 子场景 1：混合请求 → mask（summary 保留，contact 裁剪）---
    mixed_args = {
        "object_ref": {"object_type": "person", "object_id": "person-01"},
        "scope": {"area_code": "330106"},
        "field_sets": ["summary", "contact"],
    }

    cap_a = _build_capability(user_a)
    result_a = await cap_a.execute(
        tool_call_id="tcl-field-a-mixed",
        tool_id="governance.get_object_profile",
        raw_arguments=mixed_args,
        auth_context=user_a,
    )
    assert result_a.policy.decision in {"allow", "mask"}
    assert "FIELD_NOT_AUTHORIZED" not in (result_a.warnings or [])
    assert "FIELD_RESTRICTED" not in (result_a.warnings or [])

    cap_b_mixed = _build_capability(user_b, adapter=_AllowAdapter())
    result_b_mixed = await cap_b_mixed.execute(
        tool_call_id="tcl-field-b-mixed",
        tool_id="governance.get_object_profile",
        raw_arguments=mixed_args,
        auth_context=user_b,
    )
    # fail-closed：mask 策略，敏感字段被裁剪，请求仍可返回非敏感部分
    assert result_b_mixed.policy.decision == "mask"
    assert "FIELD_RESTRICTED" in result_b_mixed.warnings
    # masked_fields 必须包含 "contact"，不允许的字段已被裁剪
    assert "contact" in result_b_mixed.policy.masked_fields

    # --- 子场景 2：仅请求敏感字段 → deny（整个请求被拒绝）---
    sensitive_only_args = {
        "object_ref": {"object_type": "person", "object_id": "person-01"},
        "scope": {"area_code": "330106"},
        "field_sets": ["contact"],
    }

    cap_b_deny = _build_capability(user_b, adapter=_DenyGateAdapter())
    result_b_deny = await cap_b_deny.execute(
        tool_call_id="tcl-field-b-deny",
        tool_id="governance.get_object_profile",
        raw_arguments=sensitive_only_args,
        auth_context=user_b,
    )
    assert result_b_deny.status == "denied"
    assert result_b_deny.policy.decision == "deny"
    assert "FIELD_NOT_AUTHORIZED" in result_b_deny.warnings


# ---------- 维度 3：数据集越权 ----------


@pytest.mark.asyncio
async def test_dataset_escalation_user_a_allowed_user_b_denied() -> None:
    """A 用户有 housing 数据集授权，B 用户无；B 查询 housing 必须 fail-closed。"""
    user_a = _make_auth_context(
        user_id="user-a-dataset",
        areas=[{"area_code": "330106", "include_descendants": True}],
        datasets=["housing"],
        entitlements=["governance.housing.aggregate.read"],
    )
    user_b = _make_auth_context(
        user_id="user-b-dataset",
        areas=[{"area_code": "330106", "include_descendants": True}],
        datasets=["population"],  # 无 housing 授权
        entitlements=["governance.housing.aggregate.read"],
    )

    arguments = models.QueryHousingMetricsInput.model_validate(
        {"query": {"scope": {"area_code": "330106"}}}
    )

    # A 用户：有 housing 数据集授权 → allow
    cap_a = _build_capability(user_a)
    result_a = await cap_a.execute(
        tool_call_id="tcl-dataset-a",
        tool_id="governance.query_housing_metrics",
        raw_arguments=arguments.model_dump(),
        auth_context=user_a,
    )
    assert result_a.policy.decision == "allow"

    # B 用户：无 housing 数据集授权 → deny + DATASET_NOT_AUTHORIZED
    cap_b = _build_capability(user_b, adapter=_DenyGateAdapter())
    result_b = await cap_b.execute(
        tool_call_id="tcl-dataset-b",
        tool_id="governance.query_housing_metrics",
        raw_arguments=arguments.model_dump(),
        auth_context=user_b,
    )
    assert result_b.status == "denied"
    assert result_b.policy.decision == "deny"
    assert "DATASET_NOT_AUTHORIZED" in result_b.warnings


# ---------- 组合验证：三维度同时越权 ----------


@pytest.mark.asyncio
async def test_combined_escalation_all_dimensions_denied() -> None:
    """B 用户同时在区划、字段、数据集三个维度越权，必须全部 fail-closed。"""
    user_b = _make_auth_context(
        user_id="user-b-combined",
        areas=[{"area_code": "330108", "include_descendants": True}],  # 区划不对
        datasets=["population"],  # 数据集不对（需要 governance_objects）
        entitlements=["governance.object.profile.read"],  # 无 contact 权限
    )

    arguments = {
        "object_ref": {"object_type": "person", "object_id": "person-01"},
        "scope": {"area_code": "330106"},  # B 无此区划权限
        "field_sets": ["summary", "contact"],  # B 无 contact 权限
    }

    cap_b = _build_capability(user_b, adapter=_DenyGateAdapter())
    result_b = await cap_b.execute(
        tool_call_id="tcl-combined-b",
        tool_id="governance.get_object_profile",
        raw_arguments=arguments,
        auth_context=user_b,
    )

    # fail-closed：至少一个维度被 deny（策略按优先级拒绝）
    assert result_b.status == "denied"
    assert result_b.policy.decision == "deny"
    # 拒绝原因码应包含至少一个越权标识
    assert set(result_b.warnings or []) & {
        "AREA_OUT_OF_SCOPE",
        "DATASET_NOT_AUTHORIZED",
        "FIELD_NOT_AUTHORIZED",
    }
