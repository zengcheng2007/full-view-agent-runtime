"""S0 语义内核候选（离线候选，不接入生产 Tool Registry）。

四层结构：
- catalog：只声明真实 HTTP 已验证能力的版本化语义目录；
- query_spec：框架中立的受控 QuerySpec（模型侧业务语义）；
- validator：语义与授权反例校验（结构化违规码）；
- compiler：编译到已验证能力标识的框架中立计划 + 结果 Schema 校验；
- execution_guard：执行前按实际主题 Tool 复用生产 Policy 复核。
"""

from full_view_agent.semantic.authorization import SubjectAuthorization
from full_view_agent.semantic.catalog import (
    SEMANTIC_CATALOG_VERSION,
    CapabilityBinding,
    FilterDefinition,
    FilterSummary,
    GroupByRule,
    GroupBySummary,
    MetricDefinition,
    ModelCapabilityView,
    ResultShape,
    SemanticCatalog,
    SubjectCapabilityView,
    SubjectDefinition,
)
from full_view_agent.semantic.compiler import (
    EvidenceExpectation,
    ExpectedResultShape,
    MetricDefinitionRef,
    PlanStep,
    SemanticCompiler,
    SemanticPlan,
)
from full_view_agent.semantic.errors import (
    ResultSchemaMismatch,
    SemanticKernelError,
    SemanticQueryRejected,
    UnknownSubjectError,
)
from full_view_agent.semantic.execution_guard import ExecutionGuard, ExecutionRecheck
from full_view_agent.semantic.query_spec import (
    SEMANTIC_SPEC_VERSION,
    SemanticFilter,
    SemanticOrder,
    SemanticQuerySpec,
    SemanticTimeRange,
)
from full_view_agent.semantic.validator import (
    SemanticValidator,
    ValidationReport,
    Violation,
    ViolationCode,
)

__all__ = [
    "SEMANTIC_CATALOG_VERSION",
    "SEMANTIC_SPEC_VERSION",
    "CapabilityBinding",
    "EvidenceExpectation",
    "ExecutionGuard",
    "ExecutionRecheck",
    "ExpectedResultShape",
    "FilterDefinition",
    "FilterSummary",
    "GroupByRule",
    "GroupBySummary",
    "MetricDefinition",
    "MetricDefinitionRef",
    "ModelCapabilityView",
    "PlanStep",
    "ResultSchemaMismatch",
    "ResultShape",
    "SemanticCatalog",
    "SemanticCompiler",
    "SemanticFilter",
    "SemanticKernelError",
    "SemanticOrder",
    "SemanticPlan",
    "SemanticQueryRejected",
    "SemanticQuerySpec",
    "SemanticTimeRange",
    "SemanticValidator",
    "SubjectAuthorization",
    "SubjectCapabilityView",
    "SubjectDefinition",
    "UnknownSubjectError",
    "ValidationReport",
    "Violation",
    "ViolationCode",
]
