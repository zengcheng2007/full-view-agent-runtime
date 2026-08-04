"""从可信 AnalysisPlan 与服务端执行结果组装引用式研判报告。

组装器不接收或生成模型文案；计划只能经服务端可信仓储加载，子 Result
引用必须能在既有 Result 生命周期存储中读取且内容与执行证据一致。
"""

from typing import NoReturn

from pydantic import ValidationError

from full_view_agent.application.analysis_plan_repository import (
    AnalysisPlanRepository,
)
from full_view_agent.application.analysis_planner import AnalysisPlanner
from full_view_agent.application.analysis_semantic_spec import (
    AnalysisSemanticSpecError,
    AnalysisSemanticSpecFactory,
)
from full_view_agent.application.errors import ResourceNotFound
from full_view_agent.application.fingerprints import canonical_fingerprint
from full_view_agent.application.ports import AgentStore
from full_view_agent.application.trusted_analysis_plan import (
    TrustedAnalysisPlanLoader,
)
from full_view_agent.domain.analysis_execution import (
    AnalysisExecutionResult,
    AnalysisExecutionStatus,
    AnalysisStepExecution,
)
from full_view_agent.domain.analysis_plan import (
    AnalysisOmission,
    AnalysisPlan,
    AnalysisStep,
)
from full_view_agent.domain.analysis_report import (
    AnalysisChildResultRef,
    AnalysisReportDataResult,
    AnalysisReportLimitation,
    AnalysisReportSection,
)
from full_view_agent.domain.models import (
    AuthContext,
    DataResult,
    TableDataResult,
    ToolResult,
)
from full_view_agent.semantic.action_resolver import (
    ResolvedSemanticAction,
    SemanticActionResolver,
)
from full_view_agent.semantic.catalog import SemanticCatalog
from full_view_agent.semantic.compiler import SemanticCompiler


class AnalysisReportAssemblyError(Exception):
    """输入执行证据不自洽时的稳定、fail-closed 错误。"""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"analysis report assembly rejected [{code}]: {message}")


# 报告 Result 的受控 schema 引用；不暴露物理存储或 adapter。
ANALYSIS_REPORT_DATA_SCHEMA_REF = "schema://data/analysis-report/1.0.0"


class AnalysisReportAssembler:
    """纯确定性组装器；不接收或生成模型文案。

    ``result_store`` 必须注入既有 Result 生命周期存储（生产为
    AgentStore 实现）；组装器不新建旁路仓储。
    """

    def __init__(
        self,
        *,
        catalog: SemanticCatalog,
        planner: AnalysisPlanner,
        plan_repository: AnalysisPlanRepository,
        resolver: SemanticActionResolver,
        result_store: AgentStore,
    ) -> None:
        if resolver.catalog is not catalog:
            raise ValueError(
                "analysis report assembler and resolver must share the catalog"
            )
        self._catalog = catalog
        self._resolver = resolver
        self._result_store = result_store
        self._compiler = SemanticCompiler(catalog)
        self._semantic_spec_factory = AnalysisSemanticSpecFactory(catalog)
        self._plan_loader = TrustedAnalysisPlanLoader(
            catalog=catalog,
            planner=planner,
            repository=plan_repository,
        )

    async def assemble(
        self,
        *,
        plan_id: str,
        request_id: str,
        auth_context: AuthContext,
        execution: AnalysisExecutionResult,
    ) -> AnalysisReportDataResult:
        plan = await self._plan_loader.load(
            plan_id=plan_id,
            request_id=request_id,
            auth_context=auth_context,
        )
        execution = self._revalidate_execution(execution)
        self._validate_identity(plan=plan, execution=execution)
        self._validate_plan_subjects(plan)
        self._validate_execution_shape(plan=plan, execution=execution)

        expected_status, expected_reason = self._overall_status(
            execution.steps,
            has_omissions=bool(plan.omissions),
        )
        if execution.status != expected_status:
            self._reject(
                "EXECUTION_STATUS_MISMATCH",
                "execution status does not match deterministic step outcomes",
            )
        if execution.reason_code != expected_reason:
            self._reject(
                "EXECUTION_REASON_MISMATCH",
                "execution reason does not match deterministic overall status",
            )

        sections: list[AnalysisReportSection] = []
        limitations: list[AnalysisReportLimitation] = []
        result_ids: set[str] = set()
        evidence_ids: list[str] = []
        for planned, executed in zip(plan.steps, execution.steps, strict=True):
            result_ref = await self._validated_result_ref(
                plan=plan,
                planned=planned,
                executed=executed,
                auth_context=auth_context,
            )
            if result_ref is not None:
                if result_ref.result_id in result_ids:
                    self._reject(
                        "DUPLICATE_CHILD_RESULT",
                        "the same child result cannot satisfy multiple analysis steps",
                    )
                result_ids.add(result_ref.result_id)
                data_result = (
                    executed.tool_result.data_result
                    if executed.tool_result is not None
                    else None
                )
                if data_result is not None:
                    evidence_ids.extend(data_result.evidence_ids)
            sections.append(
                AnalysisReportSection(
                    step_id=planned.step_id,
                    subject=planned.subject,
                    status=executed.status,
                    reason_code=executed.reason_code,
                    result_ref=result_ref,
                )
            )
            if executed.status != "success":
                limitations.append(
                    AnalysisReportLimitation(
                        step_id=planned.step_id,
                        subject=planned.subject,
                        status=executed.status,
                        reason_code=executed.reason_code,
                    )
                )

        result_id, result_fingerprint = self._report_identity(
            plan_id=plan.plan_id,
            request_id=plan.request_id,
            status=expected_status,
            reason_code=expected_reason,
            sections=sections,
            omissions=plan.omissions,
            limitations=limitations,
            evidence_ids=evidence_ids,
        )
        return AnalysisReportDataResult(
            result_id=result_id,
            data_schema_ref=ANALYSIS_REPORT_DATA_SCHEMA_REF,
            result_fingerprint=result_fingerprint,
            evidence_ids=evidence_ids,
            plan_id=plan.plan_id,
            request_id=plan.request_id,
            status=expected_status,
            reason_code=expected_reason,
            sections=tuple(sections),
            omissions=plan.omissions,
            limitations=tuple(limitations),
        )

    async def assemble_and_save(
        self,
        *,
        plan_id: str,
        request_id: str,
        auth_context: AuthContext,
        execution: AnalysisExecutionResult,
    ) -> AnalysisReportDataResult:
        """组装报告并写入既有 Result 生命周期，重读核验一致后返回。"""
        report = await self.assemble(
            plan_id=plan_id,
            request_id=request_id,
            auth_context=auth_context,
            execution=execution,
        )
        try:
            await self._result_store.save_result(
                user_id=auth_context.principal.user_id,
                run_id=auth_context.run_id,
                result=report,
            )
        except Exception as exc:
            raise AnalysisReportAssemblyError(
                "REPORT_NOT_PERSISTED",
                "analysis report could not be saved into the result lifecycle",
            ) from exc
        try:
            stored = await self._result_store.get_result(
                user_id=auth_context.principal.user_id,
                result_id=report.result_id,
            )
        except ResourceNotFound as exc:
            raise AnalysisReportAssemblyError(
                "REPORT_NOT_PERSISTED",
                "saved analysis report is not readable from the result store",
            ) from exc
        if not isinstance(stored, AnalysisReportDataResult) or stored != report:
            self._reject(
                "REPORT_STORE_MISMATCH",
                "re-read analysis report differs from the saved content",
            )
        return stored

    @staticmethod
    def _report_identity(
        *,
        plan_id: str,
        request_id: str,
        status: AnalysisExecutionStatus,
        reason_code: str,
        sections: list[AnalysisReportSection],
        omissions: tuple[AnalysisOmission, ...],
        limitations: list[AnalysisReportLimitation],
        evidence_ids: list[str],
    ) -> tuple[str, str]:
        """按确定性内容计算报告 result_id 与指纹；时间戳不参与身份。"""
        content: dict[str, object] = {
            "schema_version": "1.0",
            "plan_id": plan_id,
            "request_id": request_id,
            "status": status,
            "reason_code": reason_code,
            "sections": [section.model_dump(mode="json") for section in sections],
            "omissions": [omission.model_dump(mode="json") for omission in omissions],
            "limitations": [
                limitation.model_dump(mode="json") for limitation in limitations
            ],
            "extensions": {"derived_metric_refs": [], "evidence_graph_refs": []},
            "evidence_ids": list(evidence_ids),
        }
        result_id = canonical_fingerprint(
            domain="analysis-report-result-id:1.0", value=content
        )
        result_fingerprint = canonical_fingerprint(
            domain="analysis-report:1.0", value=content
        )
        return result_id, result_fingerprint

    @classmethod
    def _revalidate_execution(
        cls, execution: AnalysisExecutionResult
    ) -> AnalysisExecutionResult:
        try:
            return AnalysisExecutionResult.model_validate(
                execution.model_dump(mode="python", warnings="none")
            )
        except (TypeError, ValidationError, ValueError) as exc:
            raise AnalysisReportAssemblyError(
                "EXECUTION_CONTRACT_INVALID", "analysis execution contract is invalid"
            ) from exc

    @classmethod
    def _validate_identity(
        cls,
        *,
        plan: AnalysisPlan,
        execution: AnalysisExecutionResult,
    ) -> None:
        if execution.plan_id != plan.plan_id:
            cls._reject(
                "EXECUTION_PLAN_MISMATCH", "execution is not bound to this plan"
            )
        if execution.request_id != plan.request_id:
            cls._reject(
                "EXECUTION_REQUEST_MISMATCH", "execution is not bound to this request"
            )
        if execution.omissions != plan.omissions:
            cls._reject(
                "EXECUTION_OMISSIONS_MISMATCH",
                "execution omissions differ from the trusted plan",
            )

    @classmethod
    def _validate_plan_subjects(cls, plan: AnalysisPlan) -> None:
        subjects = [step.subject for step in plan.steps]
        if len(subjects) != len(set(subjects)):
            cls._reject(
                "DUPLICATE_PLAN_SUBJECT",
                "one report cannot contain duplicate subject steps",
            )

    @classmethod
    def _validate_execution_shape(
        cls,
        *,
        plan: AnalysisPlan,
        execution: AnalysisExecutionResult,
    ) -> None:
        if len(execution.steps) != len(plan.steps):
            cls._reject(
                "STEP_COUNT_MISMATCH",
                "execution step count differs from the trusted plan",
            )
        execution_ids = [step.step_id for step in execution.steps]
        if len(execution_ids) != len(set(execution_ids)):
            cls._reject("DUPLICATE_EXECUTION_STEP", "execution has duplicate steps")
        for planned, executed in zip(plan.steps, execution.steps, strict=True):
            if (
                planned.step_id != executed.step_id
                or planned.subject != executed.subject
            ):
                cls._reject(
                    "STEP_ORDER_MISMATCH",
                    "execution steps do not follow the trusted plan order",
                )

    async def _validated_result_ref(
        self,
        *,
        plan: AnalysisPlan,
        planned: AnalysisStep,
        executed: AnalysisStepExecution,
        auth_context: AuthContext,
    ) -> AnalysisChildResultRef | None:
        if executed.status in {"success", "partial"}:
            raw_tool_result = executed.tool_result
            if raw_tool_result is None or raw_tool_result.data_result is None:
                self._reject(
                    "CHILD_RESULT_REQUIRED",
                    "successful and partial steps require a child data result",
                )
            tool_result = self._revalidate_tool_result(raw_tool_result)
            if tool_result.status != executed.status:
                self._reject(
                    "CHILD_STATUS_MISMATCH",
                    "child ToolResult status differs from its analysis step",
                )
            try:
                spec = self._semantic_spec_factory.build(plan, planned)
            except AnalysisSemanticSpecError as exc:
                raise AnalysisReportAssemblyError(
                    "EXPECTED_SEMANTIC_PLAN_INVALID",
                    "trusted plan step cannot be rebuilt as a semantic query",
                ) from exc
            resolution = self._resolver.compile_action(
                {
                    "catalog_version": plan.catalog_version,
                    "catalog_fingerprint": plan.catalog_fingerprint,
                    "spec": spec.model_dump(mode="json"),
                },
                auth_context=auth_context,
            )
            if not isinstance(resolution, ResolvedSemanticAction):
                self._reject(
                    "EXPECTED_SEMANTIC_PLAN_INVALID",
                    "trusted plan step no longer compiles under current authorization",
                )
            canonical_step = resolution.plan.steps[0]
            if (
                tool_result.tool_id != canonical_step.capability_id
                or tool_result.tool_version != canonical_step.capability_version
                or tool_result.tool_id != planned.capability_id
                or tool_result.tool_version != planned.capability_version
            ):
                self._reject(
                    "CHILD_TOOL_MISMATCH",
                    "child ToolResult does not match the planned canonical capability",
                )
            expected_tool_call_id = canonical_fingerprint(
                domain="analysis-step-tool-call:1.0",
                value={"plan_id": plan.plan_id, "step_id": planned.step_id},
            )
            if tool_result.tool_call_id != expected_tool_call_id:
                self._reject(
                    "CHILD_TOOL_CALL_MISMATCH",
                    "child ToolResult is not bound to this plan step",
                )
            if tool_result.semantic_lineage != resolution.lineage:
                self._reject(
                    "CHILD_LINEAGE_MISMATCH",
                    "child semantic lineage differs from the server-rebuilt lineage",
                )
            data_result = tool_result.data_result
            if not isinstance(data_result, TableDataResult):
                self._reject(
                    "CHILD_RESULT_KIND_UNSUPPORTED",
                    "analysis subject results must use the registered table kind",
                )
            try:
                self._compiler.verify_result(resolution.plan, data_result)
            except Exception as exc:
                raise AnalysisReportAssemblyError(
                    "CHILD_RESULT_SCHEMA_MISMATCH",
                    "child result does not match the catalog result shape",
                ) from exc
            fingerprint_domain = resolution.plan.expected_result.fingerprint_domain
            if fingerprint_domain is None:
                self._reject(
                    "CHILD_FINGERPRINT_DOMAIN_MISSING",
                    "catalog result shape has no registered fingerprint domain",
                )
            expected_fingerprint = canonical_fingerprint(
                domain=fingerprint_domain,
                value=data_result.data,
            )
            if data_result.result_fingerprint != expected_fingerprint:
                self._reject(
                    "CHILD_RESULT_FINGERPRINT_MISMATCH",
                    "child result fingerprint does not match its data content",
                )
            stored = await self._load_stored_result(
                auth_context, result_id=data_result.result_id
            )
            if stored != data_result:
                self._reject(
                    "CHILD_RESULT_STORE_MISMATCH",
                    "stored child result differs from the execution evidence",
                )
            try:
                return AnalysisChildResultRef(
                    result_id=data_result.result_id,
                    result_fingerprint=data_result.result_fingerprint,
                    kind="table",
                    data_schema_ref=data_result.data_schema_ref,
                )
            except ValidationError as exc:
                raise AnalysisReportAssemblyError(
                    "CHILD_RESULT_INVALID",
                    "child result identity or fingerprint is malformed",
                ) from exc

        if executed.tool_result is not None:
            tool_result = self._revalidate_tool_result(executed.tool_result)
            if executed.status in {"timeout", "skipped"}:
                self._reject(
                    "NON_USABLE_STEP_RESULT_FORBIDDEN",
                    "timeout and skipped steps cannot carry ToolResult data",
                )
            if (
                tool_result.status != executed.status
                or tool_result.data_result is not None
            ):
                self._reject(
                    "NON_USABLE_STEP_RESULT_FORBIDDEN",
                    "failed or denied steps cannot carry a successful child result",
                )
        return None

    async def _load_stored_result(
        self,
        auth_context: AuthContext,
        *,
        result_id: str,
    ) -> DataResult:
        try:
            return await self._result_store.get_result(
                user_id=auth_context.principal.user_id,
                result_id=result_id,
            )
        except ResourceNotFound as exc:
            raise AnalysisReportAssemblyError(
                "CHILD_RESULT_NOT_STORED",
                "child result is not readable from the trusted result store",
            ) from exc

    @classmethod
    def _revalidate_tool_result(cls, tool_result: ToolResult) -> ToolResult:
        try:
            return ToolResult.model_validate(
                tool_result.model_dump(mode="python", warnings="none")
            )
        except (TypeError, ValidationError, ValueError) as exc:
            raise AnalysisReportAssemblyError(
                "CHILD_RESULT_INVALID", "child ToolResult contract is invalid"
            ) from exc

    @staticmethod
    def _overall_status(
        steps: tuple[AnalysisStepExecution, ...],
        *,
        has_omissions: bool,
    ) -> tuple[AnalysisExecutionStatus, str]:
        if (
            steps
            and all(step.status == "success" for step in steps)
            and not has_omissions
        ):
            return "completed", "ANALYSIS_COMPLETED"
        if any(step.status in {"success", "partial"} for step in steps):
            return "partial", "ANALYSIS_PARTIAL"
        return "failed", "ANALYSIS_FAILED"

    @staticmethod
    def _reject(code: str, message: str) -> NoReturn:
        raise AnalysisReportAssemblyError(code, message)
