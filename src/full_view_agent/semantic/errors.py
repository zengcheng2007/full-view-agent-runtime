"""S0 语义内核候选的受控错误类型。"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from full_view_agent.semantic.validator import Violation


class SemanticKernelError(Exception):
    """语义内核候选层的受控错误基类。"""


class UnknownSubjectError(SemanticKernelError):
    """请求了 Catalog 未声明的业务主题。"""


class SemanticQueryRejected(SemanticKernelError):
    """QuerySpec 未通过 Validator，编译被拒绝。"""

    def __init__(self, violations: tuple[Violation, ...]) -> None:
        self.violations = violations
        codes = ", ".join(violation.code.value for violation in violations)
        super().__init__(f"semantic query rejected: {codes}")


class ResultSchemaMismatch(SemanticKernelError):
    """执行结果与计划声明的结果 Schema 不一致。"""
