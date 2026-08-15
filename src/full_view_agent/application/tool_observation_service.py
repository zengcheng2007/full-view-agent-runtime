"""Shared persistence boundary for successful Tool observations."""

from dataclasses import dataclass
from datetime import timedelta
from typing import Literal, Protocol

from full_view_agent.application.fingerprints import canonical_fingerprint
from full_view_agent.application.harness import ToolAction
from full_view_agent.application.ports import AgentStore, EventPublisher
from full_view_agent.application.tool_registry import ToolRegistry
from full_view_agent.domain.models import (
    AgentRun,
    AreaCandidatesResult,
    DataResult,
    EnterpriseIndustryDistributionTable,
    EnterpriseMetricTable,
    EnterpriseScaleDistributionTable,
    EnterpriseTypeDistributionTable,
    EventCategoryTable,
    EventFinishRateTable,
    EventTrendTable,
    Evidence,
    EvidenceDisplay,
    FrontendCommand,
    FrontendCommandPreconditions,
    GovernanceOverviewTable,
    GovernancePowerMetricTable,
    HousingAreaGroupTable,
    HousingLeaseTypeTable,
    HousingRoomUseTable,
    HousingStockOverviewTable,
    MapHighlightAreaPayload,
    MapRenderChoroplethPayload,
    PanelShowTablePayload,
    PopulationAggregateTable,
    PopulationMetricTable,
    PopulationRankingTable,
    ResultDisplayField,
    ResultDownload,
    ResultPresentation,
    ResultVisualization,
    TableDataResult,
    ToolResult,
)


@dataclass(frozen=True)
class PersistedToolObservation:
    """Durable references produced from one successful Tool result."""

    data_result: DataResult
    evidence: Evidence


class ToolObservationPort(Protocol):
    """Application port shared by native and analysis execution paths."""

    async def persist(
        self,
        *,
        user_id: str,
        run: AgentRun,
        action: ToolAction,
        tool_result: ToolResult,
    ) -> PersistedToolObservation: ...


def result_reference_label(data_result: DataResult, *, fallback: str) -> str:
    """Return a concise server-owned business label for a result reference."""

    presentation = getattr(data_result, "presentation", None)
    if presentation is not None and len(presentation.summary) <= 200:
        return presentation.summary
    return fallback


def durable_tool_result_id(*, run_id: str, tool_call_id: str) -> str:
    """Return the stable Result identity for a crash-replayed Tool call."""

    return canonical_fingerprint(
        domain="tool-observation-result:1.0",
        value={"run_id": run_id, "tool_call_id": tool_call_id},
    )


class ToolObservationService:
    """Persist one observation before exposing events or UI commands.

    AgentStore remains the authority for ownership, active-run checks and
    duplicate identities. No event or frontend command is emitted until both
    the Result and its Evidence have been durably accepted.
    """

    def __init__(
        self,
        *,
        store: AgentStore,
        events: EventPublisher,
        registry: ToolRegistry,
        evidence_source_system: str,
    ) -> None:
        self._store = store
        self._events = events
        self._registry = registry
        self._evidence_source_system = evidence_source_system

    def for_registry(self, registry: ToolRegistry) -> "ToolObservationService":
        """Bind evidence construction to the same immutable Run registry."""

        return ToolObservationService(
            store=self._store,
            events=self._events,
            registry=registry,
            evidence_source_system=self._evidence_source_system,
        )

    async def persist(
        self,
        *,
        user_id: str,
        run: AgentRun,
        action: ToolAction,
        tool_result: ToolResult,
    ) -> PersistedToolObservation:
        data_result = tool_result.data_result
        if data_result is None:
            raise RuntimeError("successful tool produced no data result")
        # Adapter-generated result ids are intentionally not trusted as the
        # observation identity: a crash may re-execute the same tool call and
        # produce a fresh random id.  Bind the durable result to the run-scoped
        # tool call so replay converges before any row or event is written.
        result_id = durable_tool_result_id(
            run_id=run.run_id,
            tool_call_id=tool_result.tool_call_id,
        )
        data_result = data_result.model_copy(update={"result_id": result_id})
        evidence_id = canonical_fingerprint(
            domain="tool-observation-evidence:1.0",
            value={
                "run_id": run.run_id,
                "tool_call_id": tool_result.tool_call_id,
                "result_id": data_result.result_id,
                "result_fingerprint": data_result.result_fingerprint,
            },
        )
        data_result = data_result.model_copy(update={"evidence_ids": [evidence_id]})
        data_result = _with_presentation(data_result, action=action)
        evidence = self._build_evidence(
            run=run,
            action=action,
            tool_result=tool_result,
            data_result=data_result,
            evidence_id=evidence_id,
        )
        commands = self._build_frontend_commands(
            run=run,
            action=action,
            tool_result=tool_result,
            data_result=data_result,
        )
        data_result, evidence, commands = await self._store.save_tool_observation(
            user_id=user_id,
            run_id=run.run_id,
            result=data_result,
            evidence=evidence,
            commands=commands,
        )
        await self._publish(
            run,
            "result.available",
            {"result_id": data_result.result_id},
            idempotency_key=f"{tool_result.tool_call_id}:result.available",
        )
        await self._publish(
            run,
            "evidence.available",
            {"evidence_id": evidence_id, "result_id": data_result.result_id},
            idempotency_key=f"{tool_result.tool_call_id}:evidence.available",
        )
        for command in commands:
            await self._publish(
                run,
                "frontend.command.requested",
                {"command": command.model_dump(mode="json")},
                idempotency_key=(
                    f"{tool_result.tool_call_id}:frontend.command:{command.type}"
                ),
            )
        return PersistedToolObservation(data_result=data_result, evidence=evidence)

    def _build_evidence(
        self,
        *,
        run: AgentRun,
        action: ToolAction,
        tool_result: ToolResult,
        data_result: DataResult,
        evidence_id: str,
    ) -> Evidence:
        policy_fingerprint = (
            tool_result.policy.policy_fingerprint
            if tool_result.policy is not None
            else canonical_fingerprint(
                domain="evidence-policy:unavailable",
                value={"run_id": run.run_id, "tool_call_id": tool_result.tool_call_id},
            )
        )
        query_fingerprint = (
            tool_result.policy.request_fingerprint
            if tool_result.policy is not None
            else canonical_fingerprint(
                domain="evidence-query:unavailable",
                value={"tool_id": tool_result.tool_id, "result_id": data_result.result_id},
            )
        )
        manifest = self._registry.get_manifest(tool_result.tool_id)
        descriptor = self._registry.get_model_descriptor(tool_result.tool_id)
        lineage = tool_result.semantic_lineage
        return Evidence.model_validate(
            {
                "evidence_id": evidence_id,
                "result_id": data_result.result_id,
                "result_fingerprint": data_result.result_fingerprint,
                "source_system": self._evidence_source_system,
                "dataset_id": manifest.dataset_id,
                "dataset_snapshot_version": None,
                "semantic_registry_version": (
                    lineage.catalog_version if lineage is not None else None
                ),
                "semantic_contract_fingerprint": (
                    lineage.semantic_contract_fingerprint
                    if lineage is not None
                    else None
                ),
                "semantic_shape_id": (
                    lineage.semantic_shape_id if lineage is not None else None
                ),
                "semantic_operator": (
                    lineage.semantic_operator if lineage is not None else None
                ),
                "semantic_completeness": (
                    lineage.semantic_completeness if lineage is not None else None
                ),
                "semantic_tie_policy": (
                    lineage.semantic_tie_policy if lineage is not None else None
                ),
                "metric_definitions": (
                    [
                        definition.model_dump(mode="json")
                        for definition in lineage.metric_definitions
                    ]
                    if lineage is not None
                    else []
                ),
                "effective_area_codes": (
                    action_area_codes(action)
                    or ([lineage.area_code] if lineage is not None else [])
                ),
                "time_range": None,
                "as_of": None,
                "retrieved_at": data_result.created_at,
                "query_fingerprint": query_fingerprint,
                "policy_fingerprint": policy_fingerprint,
                "tool": {
                    "tool_id": tool_result.tool_id,
                    "tool_version": tool_result.tool_version,
                },
                "freshness": {
                    "status": "unknown",
                    "expected_update_cycle": None,
                },
                "display": EvidenceDisplay(
                    source_label="全量信息视图业务数据",
                    dataset_label=_dataset_label(manifest.dataset_id),
                    tool_label=descriptor.name,
                    area_summary=_area_summary(
                        action_area_codes(action)
                        or ([lineage.area_code] if lineage is not None else [])
                    ),
                    freshness_label="数据新鲜度待上游提供",
                    method_note=(
                        "按自然月汇总；上游未返回的缺失月份按 0 补齐。"
                        if isinstance(data_result, TableDataResult)
                        and isinstance(data_result.data, EventTrendTable)
                        else None
                    ),
                ).model_dump(mode="json"),
            }
        )

    def _build_frontend_commands(
        self,
        *,
        run: AgentRun,
        action: ToolAction,
        tool_result: ToolResult,
        data_result: DataResult,
    ) -> tuple[FrontendCommand, ...]:
        client = run.client_capabilities
        if not isinstance(data_result, TableDataResult) or client is None:
            return ()
        if "1.1" not in client.frontend_command_schema_versions:
            return ()
        now = data_result.created_at
        area_codes = action_area_codes(action)
        canonical_tool_id = (
            tool_result.semantic_lineage.canonical_tool_id
            if tool_result.semantic_lineage is not None
            else action.tool_id
        )
        # 命令目标以 run 的发起客户端为唯一权威：能力声明只决定是否发命令，
        # 不参与目标绑定（持久层会以 origin_client_instance_id 复核）。
        target_client_instance_id = run.origin_client_instance_id
        commands: list[FrontendCommand] = []
        if "panel.show_table" in client.supported_commands:
            commands.append(
                FrontendCommand(
                    command_id=canonical_fingerprint(
                        domain="tool-observation-command:1.0",
                        value={
                            "run_id": run.run_id,
                            "tool_call_id": tool_result.tool_call_id,
                            "type": "panel.show_table",
                        },
                    ),
                    run_id=run.run_id,
                    target_client_instance_id=target_client_instance_id,
                    type="panel.show_table",
                    issued_at=now,
                    expires_at=now + timedelta(minutes=5),
                    preconditions=FrontendCommandPreconditions(
                        session_id=run.session_id,
                        area_code=area_codes[0] if area_codes else None,
                        required_client_capability="panel.show_table@1.0",
                    ),
                    payload=PanelShowTablePayload(result_id=data_result.result_id),
                )
            )
        choropleth_metric = _choropleth_metric(
            canonical_tool_id=canonical_tool_id,
            data_result=data_result,
        )
        if (
            choropleth_metric is not None
            and "map.render_choropleth" in client.supported_commands
        ):
            metric_field, legend_title = choropleth_metric
            commands.append(
                FrontendCommand(
                    command_id=canonical_fingerprint(
                        domain="tool-observation-command:1.0",
                        value={
                            "run_id": run.run_id,
                            "tool_call_id": tool_result.tool_call_id,
                            "type": "map.render_choropleth",
                        },
                    ),
                    run_id=run.run_id,
                    target_client_instance_id=target_client_instance_id,
                    type="map.render_choropleth",
                    target="map_panel",
                    issued_at=now,
                    expires_at=now + timedelta(minutes=5),
                    preconditions=FrontendCommandPreconditions(
                        session_id=run.session_id,
                        area_code=area_codes[0] if area_codes else None,
                        required_client_capability="map.render_choropleth@1.0",
                    ),
                    payload=MapRenderChoroplethPayload(
                        result_id=data_result.result_id,
                        metric_field=metric_field,
                        legend_title=legend_title,
                    ),
                )
            )
        if (
            canonical_tool_id == "governance.query_population_metrics"
            and area_codes
            and "map.highlight_area" in client.supported_commands
        ):
            commands.append(
                FrontendCommand(
                    command_id=canonical_fingerprint(
                        domain="tool-observation-command:1.0",
                        value={
                            "run_id": run.run_id,
                            "tool_call_id": tool_result.tool_call_id,
                            "type": "map.highlight_area",
                        },
                    ),
                    run_id=run.run_id,
                    target_client_instance_id=target_client_instance_id,
                    type="map.highlight_area",
                    target="map_panel",
                    issued_at=now,
                    expires_at=now + timedelta(minutes=5),
                    preconditions=FrontendCommandPreconditions(
                        session_id=run.session_id,
                        area_code=area_codes[0],
                        required_client_capability="map.highlight_area@1.0",
                    ),
                    payload=MapHighlightAreaPayload(area_code=area_codes[0]),
                )
            )
        return tuple(commands)

    async def _publish(
        self,
        run: AgentRun,
        event_type: str,
        data: dict[str, object],
        idempotency_key: str | None = None,
    ) -> None:
        await self._events.publish(
            event_type=event_type,
            session_id=run.session_id,
            run_id=run.run_id,
            data=data,
            idempotency_key=idempotency_key,
        )


def action_area_codes(action: ToolAction) -> list[str]:
    """Extract the declared area scope from canonical or semantic actions."""

    for key in ("query", "spec"):
        container = action.arguments.get(key)
        if not isinstance(container, dict):
            continue
        scope = container.get("scope")
        if not isinstance(scope, dict):
            continue
        area_code = scope.get("area_code")
        if isinstance(area_code, str) and area_code:
            return [area_code]
    return []


_AREA_LEVEL_LABELS = {
    "province": "省",
    "city": "市",
    "district": "区县",
    "street": "街道",
    "community": "社区",
    "grid": "网格",
}

_DATASET_LABELS = {
    "administrative_area": "行政区划数据",
    "population": "人口聚合数据",
    "housing": "房屋聚合数据",
    "event": "治理事件数据",
    "enterprise": "企业聚合数据",
    "governance_objects": "治理对象数据",
    "governance_overview": "区域治理总览数据",
    "governance_power": "治理力量汇总数据",
}


def _with_presentation(data_result: DataResult, *, action: ToolAction) -> DataResult:
    if isinstance(data_result, AreaCandidatesResult):
        candidates = data_result.data.candidates
        if data_result.data.ambiguous:
            names = "、".join(candidate.area_name for candidate in candidates[:3])
            summary = f"找到 {len(candidates)} 个候选区划：{names}。"
            status_label = "待确认"
        elif candidates:
            candidate = candidates[0]
            level = _AREA_LEVEL_LABELS[candidate.level]
            summary = (
                f"已定位到{candidate.area_name}"
                f"（{level}，{candidate.area_code}）。"
            )
            status_label = "已解析"
        else:
            summary = "未找到可查询的授权区划。"
            status_label = "未解析"
        presentation = ResultPresentation(
            title="区划解析结果",
            summary=summary,
            status_label=status_label,
        )
        return data_result.model_copy(update={"presentation": presentation})
    if not isinstance(data_result, TableDataResult):
        return data_result
    presentation = _table_presentation(data_result, action=action)
    return data_result.model_copy(update={"presentation": presentation})


def _table_presentation(
    result: TableDataResult, *, action: ToolAction
) -> ResultPresentation:
    download = ResultDownload(
        formats=["csv"],
        path=f"/agent-api/v1/results/{result.result_id}/download?format=csv",
    )
    table_view = ResultVisualization(kind="table", title="数据表格")
    if isinstance(result.data, PopulationAggregateTable):
        row = result.data.rows[0]
        operation_labels = {
            "sum": "合计",
            "avg": "平均值",
            "min": "最小值",
            "max": "最大值",
        }
        operation_label = operation_labels[row.operator]
        return ResultPresentation(
            title=f"人口{operation_label}",
            summary=(
                f"基于 {row.area_count} 个完整区划计算，"
                f"人口{operation_label}为 {row.value:g} 人。"
            ),
            status_label="查询完成",
            fields=[
                ResultDisplayField(field="operator", label="聚合运算", role="dimension"),
                ResultDisplayField(field="metric", label="指标", role="dimension"),
                ResultDisplayField(
                    field="value", label=f"人口{operation_label}", role="metric", unit="人"
                ),
                ResultDisplayField(
                    field="area_count", label="参与区划数", role="metric", unit="个"
                ),
                ResultDisplayField(
                    field="completeness", label="完整性", role="dimension"
                ),
            ],
            visualizations=[
                table_view,
                ResultVisualization(
                    kind="metric", title=f"人口{operation_label}", value_field="value"
                ),
            ],
            download=download,
        )
    if isinstance(result.data, (PopulationMetricTable, PopulationRankingTable)):
        total = sum(row.person_count for row in result.data.rows)
        solitary = _population_category(action) == "solitary_elderly"
        metric_label = "独居老人数量" if solitary else "人口数量"
        return ResultPresentation(
            title="独居老人分布" if solitary else "人口分布",
            summary=f"共 {result.row_count} 个区划，{metric_label}合计 {total} 人。",
            status_label="查询完成",
            fields=[
                *(
                    [ResultDisplayField(field="rank", label="排名", role="dimension")]
                    if isinstance(result.data, PopulationRankingTable)
                    else []
                ),
                ResultDisplayField(field="area_code", label="区划编码", role="identifier"),
                ResultDisplayField(field="area_name", label="区划名称", role="dimension"),
                ResultDisplayField(
                    field="person_count", label=metric_label, role="metric", unit="人"
                ),
            ],
            visualizations=[
                table_view,
                ResultVisualization(
                    kind="bar",
                    title=metric_label,
                    category_field="area_name",
                    value_field="person_count",
                ),
                ResultVisualization(
                    kind="choropleth",
                    title=metric_label,
                    label_field="area_name",
                    area_code_field="area_code",
                    value_field="person_count",
                ),
            ],
            download=download,
        )
    if isinstance(result.data, HousingAreaGroupTable):
        total = sum(row.dwelling_count for row in result.data.rows)
        return ResultPresentation(
            title="出租房分布",
            summary=f"共 {result.row_count} 个区划，出租房合计 {total} 套。",
            status_label="查询完成",
            fields=[
                ResultDisplayField(field="area_code", label="区划编码", role="identifier"),
                ResultDisplayField(field="area_name", label="区划名称", role="dimension"),
                ResultDisplayField(
                    field="dwelling_count",
                    label="出租房数量",
                    role="metric",
                    unit="套",
                ),
            ],
            visualizations=[
                table_view,
                ResultVisualization(
                    kind="bar",
                    title="出租房数量",
                    category_field="area_name",
                    value_field="dwelling_count",
                ),
                ResultVisualization(
                    kind="choropleth",
                    title="出租房数量",
                    label_field="area_name",
                    area_code_field="area_code",
                    value_field="dwelling_count",
                ),
            ],
            download=download,
        )
    if isinstance(result.data, EnterpriseMetricTable):
        total = sum(row.enterprise_count for row in result.data.rows)
        summary = f"共 {result.row_count} 个区划，企业合计 {total} 家。"
        if result.data.rows:
            peak = sorted(
                result.data.rows,
                key=lambda row: (-row.enterprise_count, row.area_code),
            )[0]
            summary = (
                f"共 {result.row_count} 个区划，企业合计 {total} 家；"
                f"企业最多的区划为{peak.area_name}（{peak.enterprise_count} 家）。"
            )
        return ResultPresentation(
            title="企业区划分布",
            summary=summary,
            status_label="查询完成",
            fields=[
                ResultDisplayField(field="area_code", label="区划编码", role="identifier"),
                ResultDisplayField(field="area_name", label="区划名称", role="dimension"),
                ResultDisplayField(
                    field="enterprise_count",
                    label="企业数量",
                    role="metric",
                    unit="家",
                ),
            ],
            visualizations=[
                table_view,
                ResultVisualization(
                    kind="bar",
                    title="企业数量",
                    category_field="area_name",
                    value_field="enterprise_count",
                ),
                ResultVisualization(
                    kind="choropleth",
                    title="企业数量",
                    label_field="area_name",
                    area_code_field="area_code",
                    value_field="enterprise_count",
                ),
            ],
            download=download,
        )
    if isinstance(result.data, EnterpriseTypeDistributionTable):
        total = sum(row.enterprise_count for row in result.data.rows)
        return ResultPresentation(
            title="企业类型分布",
            summary=(
                "旧接口最多返回 8 类；"
                f"当前返回 {result.row_count} 类，已返回企业数量合计 {total} 家。"
            ),
            status_label="查询完成",
            fields=[
                ResultDisplayField(
                    field="enterprise_type", label="企业类型", role="dimension"
                ),
                ResultDisplayField(
                    field="enterprise_count",
                    label="企业数量",
                    role="metric",
                    unit="家",
                ),
            ],
            visualizations=[
                table_view,
                ResultVisualization(
                    kind="metric",
                    title="已返回企业数量合计",
                    value_field="enterprise_count",
                ),
                ResultVisualization(
                    kind="bar",
                    title="企业类型分布",
                    category_field="enterprise_type",
                    value_field="enterprise_count",
                ),
            ],
            download=download,
        )
    if isinstance(result.data, EnterpriseScaleDistributionTable):
        total = sum(row.enterprise_count for row in result.data.rows)
        return ResultPresentation(
            title="企业规模分布",
            summary=(
                f"按从业人数统计 5 档规模，已纳入规模统计的企业合计 {total} 家；"
                "从业人数为空的企业不在上述合计内。"
            ),
            status_label="查询完成",
            fields=[
                ResultDisplayField(
                    field="enterprise_scale", label="企业规模", role="dimension"
                ),
                ResultDisplayField(
                    field="enterprise_count",
                    label="企业数量",
                    role="metric",
                    unit="家",
                ),
            ],
            visualizations=[
                table_view,
                ResultVisualization(
                    kind="metric",
                    title="已纳入规模统计的企业合计",
                    value_field="enterprise_count",
                ),
                ResultVisualization(
                    kind="bar",
                    title="企业规模分布",
                    category_field="enterprise_scale",
                    value_field="enterprise_count",
                ),
            ],
            download=download,
        )
    if isinstance(result.data, EnterpriseIndustryDistributionTable):
        total = sum(row.enterprise_count for row in result.data.rows)
        return ResultPresentation(
            title="企业行业分布",
            summary=(
                "旧接口最多返回 8 个行业；"
                f"当前返回 {result.row_count} 个行业，已返回企业数量合计 {total} 家。"
            ),
            status_label="查询完成",
            fields=[
                ResultDisplayField(
                    field="industry_name", label="行业名称", role="dimension"
                ),
                ResultDisplayField(
                    field="enterprise_count",
                    label="企业数量",
                    role="metric",
                    unit="家",
                ),
            ],
            visualizations=[
                table_view,
                ResultVisualization(
                    kind="metric",
                    title="已返回企业数量合计",
                    value_field="enterprise_count",
                ),
                ResultVisualization(
                    kind="bar",
                    title="企业行业分布",
                    category_field="industry_name",
                    value_field="enterprise_count",
                ),
            ],
            download=download,
        )
    if isinstance(result.data, HousingLeaseTypeTable):
        total = sum(row.dwelling_count for row in result.data.rows)
        return ResultPresentation(
            title="出租房类型统计",
            summary=f"共 {result.row_count} 种出租类型，出租房合计 {total} 套。",
            status_label="查询完成",
            fields=[
                ResultDisplayField(
                    field="lease_type", label="出租类型", role="dimension"
                ),
                ResultDisplayField(
                    field="dwelling_count",
                    label="出租房数量",
                    role="metric",
                    unit="套",
                ),
            ],
            visualizations=[
                table_view,
                ResultVisualization(
                    kind="bar",
                    title="各类型出租房数量",
                    category_field="lease_type",
                    value_field="dwelling_count",
                ),
            ],
            download=download,
        )
    if isinstance(result.data, HousingRoomUseTable):
        total = sum(row.dwelling_count for row in result.data.rows)
        return ResultPresentation(
            title="户室用途统计",
            summary=f"共 {result.row_count} 种户室用途，户室合计 {total} 套。",
            status_label="查询完成",
            fields=[
                ResultDisplayField(
                    field="room_use", label="户室用途", role="dimension"
                ),
                ResultDisplayField(
                    field="dwelling_count",
                    label="户室数量",
                    role="metric",
                    unit="套",
                ),
            ],
            visualizations=[
                table_view,
                ResultVisualization(
                    kind="metric",
                    title="户室总量",
                    value_field="dwelling_count",
                ),
                ResultVisualization(
                    kind="bar",
                    title="各用途户室数量",
                    category_field="room_use",
                    value_field="dwelling_count",
                ),
            ],
            download=download,
        )
    if isinstance(result.data, HousingStockOverviewTable):
        if len(result.data.rows) != 1:
            raise ValueError("housing stock overview must contain exactly one row")
        row = result.data.rows[0]
        return ResultPresentation(
            title="房屋存量总览",
            summary=(
                f"楼幢共 {row.building_count} 栋，户室共 {row.room_count} 间。"
            ),
            status_label="查询完成",
            fields=[
                ResultDisplayField(
                    field="building_count",
                    label="楼幢总数",
                    role="metric",
                    unit="栋",
                ),
                ResultDisplayField(
                    field="room_count",
                    label="户室总数",
                    role="metric",
                    unit="间",
                ),
            ],
            visualizations=[
                table_view,
                ResultVisualization(
                    kind="metric",
                    title="楼幢总数",
                    value_field="building_count",
                ),
                ResultVisualization(
                    kind="metric",
                    title="户室总数",
                    value_field="room_count",
                ),
            ],
            download=download,
        )
    if isinstance(result.data, EventTrendTable):
        total = sum(row.event_count for row in result.data.rows)
        return ResultPresentation(
            title="事件总数月度趋势",
            summary=(
                f"共 {result.row_count} 个月份，事件总数合计 {total} 件；"
                "上游未返回的缺失月份按 0 补齐。"
            ),
            status_label="查询完成",
            fields=[
                ResultDisplayField(field="month", label="月份", role="dimension"),
                ResultDisplayField(
                    field="event_count",
                    label="事件总数",
                    role="metric",
                    unit="件",
                ),
            ],
            visualizations=[
                table_view,
                ResultVisualization(
                    kind="line",
                    title="事件总数月度趋势",
                    x_field="month",
                    y_field="event_count",
                ),
            ],
            download=download,
        )
    if isinstance(result.data, EventCategoryTable):
        total = sum(row.event_count for row in result.data.rows)
        return ResultPresentation(
            title="网格事件一级分类",
            summary=(
                "按现有主题块统计口径返回网格事件一级分类；"
                "排除已删除/中止记录及 occur_source 为 10、11、12、90、95"
                f" 的记录。当前返回 {result.row_count} 类、合计 {total} 件。"
            ),
            status_label="查询完成",
            fields=[
                ResultDisplayField(
                    field="category_code", label="分类编码", role="identifier"
                ),
                ResultDisplayField(
                    field="category_name", label="一级分类", role="dimension"
                ),
                ResultDisplayField(
                    field="event_count", label="事件数量", role="metric", unit="件"
                ),
            ],
            visualizations=[
                table_view,
                ResultVisualization(
                    kind="bar",
                    title="网格事件一级分类",
                    category_field="category_name",
                    value_field="event_count",
                ),
            ],
            download=download,
        )
    if isinstance(result.data, EventFinishRateTable):
        return ResultPresentation(
            title="治理事件办结率",
            summary=f"已生成 {result.row_count} 个治理层级的办结率统计。",
            status_label="查询完成",
            fields=[
                ResultDisplayField(
                    field="level",
                    label="治理层级",
                    role="dimension",
                    value_labels={
                        "grid": "网格",
                        "community": "社区",
                        "street": "街道",
                    },
                ),
                ResultDisplayField(
                    field="finish_rate",
                    label="事件办结率",
                    role="metric",
                    unit="%",
                ),
            ],
            visualizations=[
                table_view,
                ResultVisualization(
                    kind="bar",
                    title="各层级事件办结率",
                    category_field="level",
                    value_field="finish_rate",
                ),
            ],
            download=download,
        )
    if isinstance(result.data, GovernanceOverviewTable):
        related_total = sum(row.related_count for row in result.data.rows)
        subject_total = sum(row.total_count for row in result.data.rows)
        coverage_rate = (
            round(related_total / subject_total * 100, 1)
            if subject_total > 0
            else 0.0
        )
        return ResultPresentation(
            title="区域治理总览",
            summary=(
                f"共 {result.row_count} 类治理要素，关联总数 {related_total} 个、"
                f"要素总数 {subject_total} 个，综合覆盖率 {coverage_rate:g}%。"
            ),
            status_label="查询完成",
            fields=[
                ResultDisplayField(
                    field="subject_label", label="治理要素", role="dimension"
                ),
                ResultDisplayField(
                    field="related_count",
                    label="治理关联数",
                    role="metric",
                    unit="个",
                ),
                ResultDisplayField(
                    field="total_count", label="要素总数", role="metric", unit="个"
                ),
                ResultDisplayField(
                    field="coverage_rate",
                    label="治理覆盖率",
                    role="metric",
                    unit="%",
                ),
            ],
            visualizations=[
                table_view,
                ResultVisualization(
                    kind="metric",
                    title="治理覆盖率",
                    category_field="subject_label",
                    value_field="coverage_rate",
                ),
                ResultVisualization(
                    kind="bar",
                    title="治理关联数",
                    category_field="subject_label",
                    value_field="related_count",
                ),
                ResultVisualization(
                    kind="bar",
                    title="要素总数",
                    category_field="subject_label",
                    value_field="total_count",
                ),
            ],
            download=download,
        )
    if isinstance(result.data, GovernancePowerMetricTable):
        return ResultPresentation(
            title="治理力量汇总",
            summary=f"共 {result.row_count} 类治理力量汇总指标。",
            status_label="查询完成",
            fields=[
                ResultDisplayField(
                    field="type_name", label="治理力量类型", role="dimension"
                ),
                ResultDisplayField(
                    field="count", label="数量", role="metric", unit="个"
                ),
            ],
            visualizations=[
                table_view,
                ResultVisualization(
                    kind="metric", title="治理力量指标", value_field="count"
                ),
                ResultVisualization(
                    kind="bar",
                    title="治理力量分布",
                    category_field="type_name",
                    value_field="count",
                ),
            ],
            download=download,
        )
    return ResultPresentation(
        title="查询结果",
        summary=f"已生成 {result.row_count} 条结果。",
        status_label="查询完成",
        visualizations=[table_view],
        download=download,
    )


def _dataset_label(dataset_id: str) -> str:
    return _DATASET_LABELS.get(dataset_id, "业务数据")


def _area_summary(area_codes: list[str]) -> str:
    if not area_codes:
        return "本次结果未声明区划范围"
    if len(area_codes) == 1:
        return f"区划编码 {area_codes[0]}"
    return "区划编码 " + "、".join(area_codes)


def _choropleth_metric(
    *, canonical_tool_id: str, data_result: TableDataResult
) -> tuple[
    Literal["person_count", "dwelling_count", "enterprise_count"], str
] | None:
    if canonical_tool_id == "governance.query_population_metrics" and isinstance(
        data_result.data, (PopulationMetricTable, PopulationRankingTable)
    ):
        return "person_count", "人口数量"
    if canonical_tool_id == "governance.query_housing_metrics" and isinstance(
        data_result.data, HousingAreaGroupTable
    ):
        return "dwelling_count", "出租房数量（套）"
    if canonical_tool_id == "governance.query_enterprise_metrics" and isinstance(
        data_result.data, EnterpriseMetricTable
    ):
        return "enterprise_count", "企业数量（家）"
    return None


def _population_category(action: ToolAction) -> str:
    for key in ("query", "spec"):
        container = action.arguments.get(key)
        if not isinstance(container, dict):
            continue
        filters = container.get("filters")
        if not isinstance(filters, list):
            continue
        for item in filters:
            if not isinstance(item, dict):
                continue
            if (
                item.get("field") == "person_category"
                and item.get("operator") == "eq"
                and item.get("value") == "solitary_elderly"
            ):
                return "solitary_elderly"
    return "general"
