"""P2 意图编译服务：受控意图 → 可信 AnalysisRequest → 可信 Plan。

模型只提交 ``AnalysisIntentV1``；所有可信决策都在服务端完成：

- ``named_area`` 的 ``area_query`` 通过注入的服务端区划解析器解析；
  ``current_area`` 的可信编码由服务端上下文以独立入参 ``current_area_code``
  另传，绝不取自意图载荷；
- 区划歧义/未知/越权一律 fail closed；区域授权判定复用现有
  ``SubjectAuthorization`` 与区域前缀规则，不引入平行权限体系；
- goals 必须由当前 Catalog + entitlement 动态允许（未知/禁用/越权目标
  拒绝）；准入后主题的执行细节沿用现有 AnalysisPlanner omission 语义，
  但零可执行步骤的编译结果 fail closed，绝不扩大为 overview；
- 服务端生成 ``request_id``（由幂等指纹派生）并使用默认预算，编译为现有
  ``AnalysisRequest``，复用 ``AnalysisPlanningService`` 形成可信 plan。

幂等性：``request_id`` 在区划解析之后派生，指纹绑定解析后的可信编译
上下文——tenant/user/run、canonical goals、可信 scope_ref（解析结果
而非原始查询文本）、规范化授权视图（去重 + 确定性排序，不含凭据引用、
时间戳）、当前 catalog_version + execution_fingerprint 与服务端默认
预算策略。同一可信编译上下文派生出同一 ``request_id``；上下文任何一项
变化（区划解析漂移、当前区划变更、授权内容变化、Catalog/默认预算变化）
都会产生新的 ``request_id``，避免同一唯一键下出现不同 plan 内容；配合
确定性规划器与内容寻址仓库，重复请求不会形成两个 plan；授权变化后指纹
随之变化，不会回放旧授权下的计划。

接入边界（下阶段接线点）：
- 本切片不修改对外 HTTP、模型 Planner 与 Graph；下阶段将分析执行入口从
  "模型直接提交 AnalysisRequest"切换为"模型提交 ``AnalysisIntentV1``、
  由本服务编译"。
- ``AnalysisAreaResolver`` 只负责名称 → 候选解析，授权判定在本服务；
  生产可桥接现有治理区划数据源（如 resolve_area 上游）。
- ``AnalysisIntentClarificationRequired`` 携带授权内候选，由下阶段入口
  转换为 ``PendingInputRequest(kind="clarification")``。
"""

import re
from typing import Protocol, runtime_checkable

from pydantic import ConfigDict, Field, ValidationError

from full_view_agent.application.analysis_service import AnalysisPlanningService
from full_view_agent.application.fingerprints import canonical_fingerprint
from full_view_agent.domain.analysis_intent import (
    AnalysisIntentV1,
    CurrentAreaScopeIntent,
)
from full_view_agent.domain.analysis_plan import AnalysisPlan, AnalysisRequest, AreaScopeRef
from full_view_agent.domain.analysis_shared import AnalysisGoal
from full_view_agent.domain.contract_model import ContractModel
from full_view_agent.domain.models import AuthContext, MetricQueryScope
from full_view_agent.semantic.authorization import SubjectAuthorization
from full_view_agent.semantic.catalog import area_is_authorized

_STRUCTURAL_AREA_CODE = re.compile(r"[0-9]+\Z")

# 单次区划查询的候选数量上界：超过即视为解析源异常，fail closed。
MAX_AREA_CANDIDATES = 32


class AnalysisIntentError(Exception):
    """意图编译层的受控错误基类。"""


class AnalysisIntentRejected(AnalysisIntentError):
    """意图编译被拒绝（fail closed），携带稳定 code。"""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"analysis intent rejected [{code}]: {message}")


class AnalysisIntentClarificationRequired(AnalysisIntentError):
    """区划查询存在歧义，需要用户在授权内候选中澄清。"""

    def __init__(self, code: str, message: str, *, candidates: tuple["ResolvedArea", ...]) -> None:
        self.code = code
        self.candidates = candidates
        super().__init__(f"analysis intent needs clarification [{code}]: {message}")


class ResolvedArea(ContractModel):
    """区划解析器返回的可信候选：服务端事实，不接受模型输入。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    area_code: str = Field(min_length=1, max_length=32)
    area_name: str = Field(min_length=1, max_length=100)


@runtime_checkable
class AnalysisAreaResolver(Protocol):
    """服务端区划解析端口：名称 → 候选区划。

    只负责解析，不做授权判定；返回空元组表示未知查询。候选中编码结构
    非法的记录由编译服务 fail closed。
    """

    async def resolve_area_query(self, *, query: str) -> tuple[ResolvedArea, ...]: ...


class AnalysisIntentCompilationService:
    """把受控意图编译为服务端可信计划；模型侧能力面保持为零。"""

    def __init__(
        self,
        *,
        planning: AnalysisPlanningService,
        area_resolver: AnalysisAreaResolver,
    ) -> None:
        self._planning = planning
        self._area_resolver = area_resolver

    async def compile_intent(
        self,
        intent: AnalysisIntentV1,
        *,
        auth_context: AuthContext,
        current_area_code: str | None = None,
    ) -> AnalysisPlan:
        """编译意图为可信计划；所有失败路径均不落库。

        ``current_area_code`` 只允许由服务端上下文传入；``intent`` 载荷中
        不存在任何区划编码字段。
        """
        authorization = SubjectAuthorization.from_auth_context(auth_context)
        scope_ref = await self._resolve_scope(
            intent, authorization=authorization, current_area_code=current_area_code
        )
        self._admit_goals(intent.goals, authorization=authorization)
        request = AnalysisRequest(
            # request_id 必须在 scope 解析之后派生：绑定解析后的可信编译
            # 上下文而非原始意图文本。
            request_id=self._derive_request_id(
                intent,
                scope_ref=scope_ref,
                auth_context=auth_context,
                authorization=authorization,
            ),
            goals=intent.goals,
            scope_ref=scope_ref,
        )
        # 先预规划再落库：零可执行步骤的意图 fail closed，不产生空计划。
        preview = self._planning.planner.plan(request, authorization=authorization)
        if not preview.steps:
            omissions = ", ".join(
                f"{omission.subject}:{omission.reason_code}"
                for omission in preview.omissions
            )
            raise AnalysisIntentRejected(
                "NO_EXECUTABLE_GOALS",
                "intent compiles to no executable steps "
                f"under the current authorization (omissions: {omissions or 'none'})",
            )
        return await self._planning.create_plan(request=request, auth_context=auth_context)

    async def _resolve_scope(
        self,
        intent: AnalysisIntentV1,
        *,
        authorization: SubjectAuthorization,
        current_area_code: str | None,
    ) -> AreaScopeRef:
        scope = intent.scope
        if isinstance(scope, CurrentAreaScopeIntent):
            if current_area_code is None:
                raise AnalysisIntentRejected(
                    "CURRENT_AREA_UNAVAILABLE",
                    "current-area intent requires a server-side context hint",
                )
            # hint 类型错误必须 typed fail closed，不允许 TypeError 逃逸。
            if not isinstance(current_area_code, str):
                raise AnalysisIntentRejected(
                    "CURRENT_AREA_INVALID",
                    "current-area hint must be a structural area code string",
                )
            if _STRUCTURAL_AREA_CODE.match(current_area_code) is None:
                raise AnalysisIntentRejected(
                    "CURRENT_AREA_INVALID",
                    f"current-area hint {current_area_code!r} is not a structural area code",
                )
            # hint 来自上下文而非授权体系，结构校验后仍必须单独做授权判定，
            # 不能默认上下文区划一定在授权范围内。
            scope_ref = self._trusted_area_scope(
                current_area_code, invalid_code="CURRENT_AREA_INVALID"
            )
            if not area_is_authorized(scope_ref.scope, authorization):
                raise AnalysisIntentRejected(
                    "AREA_NOT_AUTHORIZED",
                    f"current-area hint {current_area_code!r} is not within "
                    "the current authorization",
                )
            return scope_ref
        candidates = await self._area_resolver.resolve_area_query(query=scope.area_query)
        if not candidates:
            raise AnalysisIntentRejected(
                "AREA_QUERY_UNKNOWN",
                f"area query {scope.area_query!r} matched no known area",
            )
        if len(candidates) > MAX_AREA_CANDIDATES:
            raise AnalysisIntentRejected(
                "AREA_CANDIDATES_OVERFLOW",
                f"area query {scope.area_query!r} produced {len(candidates)} "
                f"candidates; limit is {MAX_AREA_CANDIDATES}",
            )
        # 先完整结构校验：任一候选结构非法即整体 fail closed，不静默丢弃。
        validated: list[ResolvedArea] = []
        for candidate in candidates:
            revalidated = self._validated_candidate(candidate)
            if revalidated is None:
                raise AnalysisIntentRejected(
                    "AREA_CODE_INVALID",
                    f"area query {scope.area_query!r} produced a structurally "
                    "invalid candidate",
                )
            validated.append(revalidated)
        # 按 area_code 去重（重复条目不等于歧义）；同编码不同名称属于
        # 解析源数据冲突，fail closed。
        unique: dict[str, ResolvedArea] = {}
        for candidate in validated:
            existing = unique.get(candidate.area_code)
            if existing is None:
                unique[candidate.area_code] = candidate
            elif existing.area_name != candidate.area_name:
                raise AnalysisIntentRejected(
                    "AREA_CANDIDATE_CONFLICT",
                    f"area code {candidate.area_code!r} resolved to conflicting "
                    f"names {existing.area_name!r} / {candidate.area_name!r}",
                )
        # 确定性排序：歧义候选与授权过滤顺序不依赖解析器返回顺序。
        ordered = sorted(unique.values(), key=lambda item: item.area_code)
        authorized = [
            candidate
            for candidate in ordered
            if area_is_authorized(
                MetricQueryScope(area_code=candidate.area_code), authorization
            )
        ]
        if not authorized:
            raise AnalysisIntentRejected(
                "AREA_NOT_AUTHORIZED",
                f"no candidate for area query {scope.area_query!r} is within "
                "the current authorization",
            )
        if len(authorized) > 1:
            raise AnalysisIntentClarificationRequired(
                "AREA_QUERY_AMBIGUOUS",
                f"area query {scope.area_query!r} matched {len(authorized)} "
                "authorized areas",
                candidates=tuple(authorized),
            )
        return self._trusted_area_scope(authorized[0].area_code, invalid_code="AREA_CODE_INVALID")

    @classmethod
    def _is_structural_scope(cls, area_code: str) -> bool:
        try:
            AreaScopeRef(scope=MetricQueryScope(area_code=area_code))
        except ValidationError:
            return False
        return True

    @classmethod
    def _validated_candidate(cls, candidate: ResolvedArea) -> ResolvedArea | None:
        """完整结构校验：字段约束 + 编码结构全部通过才返回可信候选。

        重新走一遍模型校验，绕过校验构造（如 ``model_construct``）的
        候选同样会被拒绝；任何一项不通过返回 None，由调用方 fail closed。
        """
        try:
            validated = ResolvedArea.model_validate(candidate.model_dump())
        except ValidationError:
            return None
        if not cls._is_structural_scope(validated.area_code):
            return None
        return validated

    def _admit_goals(
        self, goals: tuple[AnalysisGoal, ...], *, authorization: SubjectAuthorization
    ) -> None:
        """目标准入：必须由当前 Catalog + entitlement 动态允许。

        overview 由 Catalog 派生主题，执行细节沿用 planner omission 语义；
        显式主题必须在当前 Catalog 声明、存在已验证绑定且授权持有其
        entitlement，否则整体拒绝——绝不替换或扩大为 overview。
        """
        catalog = self._planning.planner.catalog
        for goal in goals:
            if goal == "overview":
                continue
            subject = catalog.subject(goal)
            if subject is None:
                raise AnalysisIntentRejected(
                    "GOAL_NOT_DECLARED",
                    f"goal {goal} is not declared by the current catalog",
                )
            if goal not in catalog.bindable_subject_ids():
                raise AnalysisIntentRejected(
                    "GOAL_DISABLED",
                    f"goal {goal} has no verified capability binding",
                )
            if subject.required_entitlement not in authorization.entitlements:
                raise AnalysisIntentRejected(
                    "GOAL_NOT_ENTITLED",
                    f"authorization lacks entitlement "
                    f"{subject.required_entitlement} for goal {goal}",
                )

    @staticmethod
    def _trusted_area_scope(area_code: str, *, invalid_code: str) -> AreaScopeRef:
        try:
            return AreaScopeRef(scope=MetricQueryScope(area_code=area_code))
        except ValidationError as exc:
            raise AnalysisIntentRejected(
                invalid_code,
                f"area code {area_code!r} is not a structural analysis scope",
            ) from exc

    def _derive_request_id(
        self,
        intent: AnalysisIntentV1,
        *,
        scope_ref: AreaScopeRef,
        auth_context: AuthContext,
        authorization: SubjectAuthorization,
    ) -> str:
        """幂等指纹 → request_id：绑定解析后的可信编译上下文。

        身份由可信事实构成——canonical goals、解析后的 scope_ref、规范
        化授权视图、tenant/user/run、当前 Catalog 版本与执行指纹、服务端
        默认预算策略；原始 ``area_query`` 文本不是 scope 身份，解析结果
        （或上下文区划）漂移必然派生出新的 request_id。
        """
        planner = self._planning.planner
        key = canonical_fingerprint(
            domain="analysis-intent-compilation:2.0",
            value={
                "tenant_id": auth_context.principal.tenant_id,
                "user_id": auth_context.principal.user_id,
                "run_id": auth_context.run_id,
                "intent": {
                    "schema_version": intent.schema_version,
                    "kind": intent.kind,
                    "goals": list(intent.goals),
                },
                "scope_ref": scope_ref.model_dump(mode="json"),
                "authorization": _normalized_authorization_view(
                    auth_context, authorization
                ),
                "catalog_version": planner.catalog.catalog_version,
                "catalog_fingerprint": planner.catalog.execution_fingerprint,
                "default_budget": planner.default_budget.model_dump(mode="json"),
            },
        )
        return f"areq_{key.removeprefix('sha256:')}"


def _normalized_authorization_view(
    auth_context: AuthContext, authorization: SubjectAuthorization
) -> dict[str, object]:
    """稳定授权视图：集合去重 + 确定性排序，条目顺序/重复不影响指纹。

    只含稳定授权事实：不含凭据引用、auth-context ID 与时间戳。
    """
    area_entries = {
        (area.area_code, area.include_descendants)
        for area in authorization.area_scopes
    }
    return {
        "entitlements": sorted(set(authorization.entitlements)),
        "datasets": sorted(set(authorization.datasets)),
        "areas": [
            {"area_code": area_code, "include_descendants": include_descendants}
            for area_code, include_descendants in sorted(area_entries)
        ],
        "field_policy_set": authorization.field_policy_set,
        "purpose": auth_context.purpose,
        "policy_version": auth_context.policy_version,
    }
