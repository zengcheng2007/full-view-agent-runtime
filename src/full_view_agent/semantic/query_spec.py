"""S0 语义内核候选：框架中立的受控 QuerySpec。

模型侧只能表达业务语义：subject、metrics、scope、group_by、filters、
order_by、limit、time_range、output。不得表达物理表、列、URL、SQL/DSL、
adapter 名或数据源实现；字段/操作符命名在 Schema 层即受结构约束，
驼峰、点号、物理表名式注入会被直接拒绝。

当前没有任何真实数据源验证过 order_by 与 time_range，因此它们可以被
表达但必须由 Validator 拒绝（见 validator.py）。
"""

import re
from typing import Literal

from pydantic import Field, field_validator

from full_view_agent.domain.models import ContractModel, MetricQueryScope

# 受控语义字段命名：小写字母开头，仅含小写字母、数字和下划线。
# 结构性拦截物理命名注入（areaCode、tableName、dm_empty_nest_old 以外的
# 驼峰/点号/大小写变体）；语义注册校验由 Validator 负责。
SEMANTIC_NAME_PATTERN = r"^[a-z][a-z0-9_]{0,63}$"

# 区划编码结构：仅 ASCII 数字，长度为系统支持的层级
# （市4/区县6/街道9/社区12/网格15）。语义层边界先行拦截非数字填充、
# 注入式后缀与非法长度；主题具体支持哪些层级仍由 Validator 限制。
# 不修改全局 MetricQueryScope 的既有公开契约。
SEMANTIC_SCOPE_LEVELS: tuple[int, ...] = (4, 6, 9, 12, 15)
_AREA_CODE_DIGITS_ONLY = re.compile(r"[0-9]+\Z")

SEMANTIC_SPEC_VERSION = "s0.1"


class SemanticFilter(ContractModel):
    field: str = Field(min_length=1, pattern=SEMANTIC_NAME_PATTERN)
    operator: str = Field(min_length=1, pattern=SEMANTIC_NAME_PATTERN)
    value: str | int | float | bool | list[str | int | float] | None = None


class SemanticOrder(ContractModel):
    field: str = Field(min_length=1, pattern=SEMANTIC_NAME_PATTERN)
    direction: Literal["asc", "desc"] = "asc"


class SemanticTimeRange(ContractModel):
    start: str = Field(min_length=1, max_length=64)
    end: str = Field(min_length=1, max_length=64)


class SemanticQuerySpec(ContractModel):
    schema_version: Literal["s0.1"] = SEMANTIC_SPEC_VERSION
    subject: str = Field(min_length=1, max_length=64)
    operator: Literal[
        "list", "sum", "avg", "min", "max", "top", "bottom", "rank"
    ] = "list"
    metrics: list[str] = Field(min_length=1, max_length=5)
    scope: MetricQueryScope
    group_by: list[str] = Field(default_factory=list, max_length=2)
    filters: list[SemanticFilter] = Field(default_factory=list, max_length=10)
    order_by: list[SemanticOrder] = Field(default_factory=list, max_length=4)
    limit: int = Field(default=200, ge=1, le=1000)
    time_range: SemanticTimeRange | None = None
    output: Literal["table", "choropleth", "metric_card"] = "table"

    @field_validator("scope")
    @classmethod
    def _scope_area_code_must_be_structural(cls, scope: MetricQueryScope) -> MetricQueryScope:
        area_code = scope.area_code
        if not _AREA_CODE_DIGITS_ONLY.match(area_code):
            raise ValueError(
                "scope.area_code 必须为纯数字区划编码（ASCII 0-9），"
                f"实际为 {area_code!r}。"
            )
        if len(area_code) not in SEMANTIC_SCOPE_LEVELS:
            raise ValueError(
                "scope.area_code 长度必须为系统支持的区划层级"
                f" {SEMANTIC_SCOPE_LEVELS}（市4/区县6/街道9/社区12/网格15），"
                f"实际长度 {len(area_code)}。"
            )
        return scope
