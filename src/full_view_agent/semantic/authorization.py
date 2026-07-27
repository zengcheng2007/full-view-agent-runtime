"""S0 语义内核候选：从生产 AuthContext 派生的主题授权视图。

Validator 用它做主题级权限校验（主题授权、数据集、字段策略集、区域），
Catalog 用它派生权限过滤后的模型能力视图。权限判定的最终事实源仍是
运行时的 AuthContext 与生产 Policy；本视图只做只读投影，不放宽任何条件。
"""

from full_view_agent.domain.models import (
    AuthContext,
    AuthorizedAreaScope,
    ContractModel,
)


class SubjectAuthorization(ContractModel):
    entitlements: tuple[str, ...] = ()
    datasets: tuple[str, ...] = ()
    area_scopes: tuple[AuthorizedAreaScope, ...] = ()
    field_policy_set: str = ""

    @classmethod
    def from_auth_context(cls, auth_context: AuthContext) -> "SubjectAuthorization":
        return cls(
            entitlements=tuple(auth_context.entitlements),
            datasets=tuple(auth_context.data_scopes.datasets),
            area_scopes=tuple(auth_context.data_scopes.areas),
            field_policy_set=auth_context.data_scopes.field_policy_set,
        )
