"""S1-A：语义入口的生产接线组合。

把语义内核（Catalog/Resolver/Compiler/Guard）与既有生产能力栈
（CapabilityService/Policy/Adapter）组合为一份共享接线，供
``orchestrator_factory``、API 组合根与 Eval Runner 复用，保证
Native/LangGraph 与离线评测走同一套语义链路，不产生第二份实现。

接线不改变既有 Tool 的任何行为：语义执行器只是 Harness 执行器的
透明包装，非 semantic_query 调用原样直通。
"""

from dataclasses import dataclass

from full_view_agent.application.capability_service import (
    AuthContextRefresher,
    CapabilityService,
    DenialLedger,
    ToolAdapter,
)
from full_view_agent.application.harness import (
    AgentHarness,
    DeterministicCompletionValidator,
)
from full_view_agent.application.policy import MinimalPolicyAdapter
from full_view_agent.application.semantic_executor import (
    SemanticToolCallFingerprinter,
    SemanticToolExecutor,
)
from full_view_agent.application.tool_registry import ToolRegistry
from full_view_agent.semantic.action_resolver import SemanticActionResolver
from full_view_agent.semantic.catalog import SemanticCatalog
from full_view_agent.semantic.presenter import SemanticToolPresenter


@dataclass(frozen=True)
class SemanticCapabilityStack:
    """一次组合的语义入口全链路；所有字段共享同一 Catalog 实例。"""

    catalog: SemanticCatalog
    resolver: SemanticActionResolver
    capability: CapabilityService
    executor: SemanticToolExecutor
    fingerprinter: SemanticToolCallFingerprinter
    presenter: SemanticToolPresenter

    def build_harness(self) -> AgentHarness:
        """生产 Harness：语义执行器 + 规范动作循环指纹。"""
        return AgentHarness(
            tool_executor=self.executor,
            validator=DeterministicCompletionValidator(),
            call_fingerprinter=self.fingerprinter,
        )


def build_semantic_capability_stack(
    *,
    registry: ToolRegistry,
    adapter: ToolAdapter,
    policy: MinimalPolicyAdapter | None = None,
    catalog: SemanticCatalog | None = None,
    auth_context_refresher: AuthContextRefresher | None = None,
    denial_ledger: DenialLedger | None = None,
) -> SemanticCapabilityStack:
    effective_policy = policy or MinimalPolicyAdapter()
    effective_catalog = catalog or SemanticCatalog.default()
    capability = CapabilityService(
        registry=registry,
        policy=effective_policy,
        adapter=adapter,
        auth_context_refresher=auth_context_refresher,
        denial_ledger=denial_ledger,
    )
    resolver = SemanticActionResolver(
        catalog=effective_catalog,
        registry=registry,
        policy=effective_policy,
    )
    return SemanticCapabilityStack(
        catalog=effective_catalog,
        resolver=resolver,
        capability=capability,
        executor=SemanticToolExecutor(inner=capability, resolver=resolver),
        fingerprinter=SemanticToolCallFingerprinter(resolver=resolver),
        presenter=SemanticToolPresenter(catalog=effective_catalog),
    )
