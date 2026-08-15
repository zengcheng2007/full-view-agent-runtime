from copy import deepcopy
from threading import RLock

from full_view_agent.application.errors import ResourceNotFound
from full_view_agent.domain.capability import ToolSemanticContract
from full_view_agent.domain.knowledge import KnowledgeSearchInput
from full_view_agent.domain.models import (
    GetObjectProfileInput,
    InternalToolManifest,
    ModelToolDescriptor,
    QueryEnterpriseMetricsInput,
    QueryEventMetricsInput,
    QueryGovernanceOverviewInput,
    QueryGovernancePowerMetricsInput,
    QueryHousingMetricsInput,
    QueryPopulationMetricsInput,
    ResolveAreaInput,
)

_INPUT_MODELS = {
    "knowledge.search": KnowledgeSearchInput,
    "governance.resolve_area": ResolveAreaInput,
    "governance.query_population_metrics": QueryPopulationMetricsInput,
    "governance.query_housing_metrics": QueryHousingMetricsInput,
    "governance.query_event_metrics": QueryEventMetricsInput,
    "governance.query_enterprise_metrics": QueryEnterpriseMetricsInput,
    "governance.get_governance_overview": QueryGovernanceOverviewInput,
    "governance.query_governance_power_metrics": QueryGovernancePowerMetricsInput,
    "governance.get_object_profile": GetObjectProfileInput,
}

PRODUCTION_HTTP_TOOL_IDS = (
    "governance.get_governance_overview",
    "governance.query_governance_power_metrics",
    "governance.query_enterprise_metrics",
    "governance.query_event_metrics",
    "governance.query_housing_metrics",
    "governance.query_population_metrics",
    "governance.resolve_area",
)


class ToolRegistry:
    def __init__(
        self,
        *,
        manifests: list[InternalToolManifest],
        descriptors: list[ModelToolDescriptor],
        housing_next_area_enabled: bool = True,
        event_category_enabled: bool = False,
        dynamic_input_schemas: dict[str, dict[str, object]] | None = None,
    ) -> None:
        self._lock = RLock()
        self._baseline_manifests = {
            manifest.tool_id: manifest for manifest in manifests
        }
        self._baseline_descriptors = {
            descriptor.tool_id: descriptor for descriptor in descriptors
        }
        for tool_id, manifest in self._baseline_manifests.items():
            contract = manifest.semantic_contract
            descriptor = self._baseline_descriptors.get(tool_id)
            if (
                contract is None
                or descriptor is None
                or "Semantic contract Tool@" in descriptor.description
            ):
                continue
            self._baseline_descriptors[tool_id] = descriptor.model_copy(
                update={
                    "description": (
                        f"{descriptor.description} Semantic contract Tool@"
                        f"{manifest.tool_version}: subject={contract.subject}; "
                        f"operators={', '.join(contract.operators)}; "
                        f"completeness={contract.completeness.mode}."
                    )
                }
            )
        self._baseline_dynamic_input_schemas = dict(dynamic_input_schemas or {})
        self._manifests = dict(self._baseline_manifests)
        self._descriptors = dict(self._baseline_descriptors)
        self._housing_next_area_enabled = housing_next_area_enabled
        self._event_category_enabled = event_category_enabled
        self._dynamic_input_schemas = dict(self._baseline_dynamic_input_schemas)

    def bind_lock(self, lock: RLock) -> None:
        """Join a composite runtime generation lock during composition."""

        with self._lock:
            self._lock = lock

    @classmethod
    def default(
        cls,
        *,
        housing_next_area_enabled: bool = True,
        event_category_enabled: bool = False,
    ) -> "ToolRegistry":
        return cls(
            manifests=[
                _manifest(
                    tool_id="knowledge.search",
                    risk_level="low",
                    dataset_id="knowledge",
                    classifications=["internal"],
                    permission="knowledge.search",
                    input_schema_ref="schema://tools/knowledge-search-input/1.0.0",
                    result_kind="table",
                    data_schema_ref="schema://data/table-data-result/1.0.0",
                    cache_enabled=False,
                    ttl_seconds=0,
                    action="knowledge.search",
                    denial_scope="dataset_area_fields",
                    adapter_ref="adapter://agent-runtime/knowledge-search/1.0",
                ),
                _manifest(
                    tool_id="governance.query_governance_power_metrics",
                    risk_level="low",
                    dataset_id="governance_power",
                    classifications=["internal", "aggregated"],
                    permission="governance.power.aggregate.read",
                    input_schema_ref="schema://tools/query-governance-power-metrics-input/1.0.0",
                    result_kind="table",
                    data_schema_ref="schema://data/governance-power-metric-table/1.0.0",
                    cache_enabled=True,
                    ttl_seconds=60,
                    action="governance.power.aggregate.read",
                    denial_scope="dataset_area_fields",
                    adapter_ref="adapter://geo-qxst/governance-power-metrics/1.0",
                ),
                _manifest(
                    tool_id="governance.get_governance_overview",
                    risk_level="low",
                    dataset_id="governance_overview",
                    classifications=["internal", "aggregated"],
                    permission="governance.overview.aggregate.read",
                    input_schema_ref=(
                        "schema://tools/query-governance-overview-input/1.0.0"
                    ),
                    result_kind="table",
                    data_schema_ref=(
                        "schema://data/governance-overview-table/1.0.0"
                    ),
                    cache_enabled=True,
                    ttl_seconds=60,
                    action="governance.overview.aggregate.read",
                    denial_scope="dataset_area_fields",
                    adapter_ref="adapter://geo-qxst/governance-overview/1.0",
                ),
                _manifest(
                    tool_id="governance.query_enterprise_metrics",
                    risk_level="low",
                    dataset_id="enterprise",
                    classifications=["internal", "aggregated"],
                    permission="governance.enterprise.aggregate.read",
                    input_schema_ref=(
                        "schema://tools/query-enterprise-metrics-input/1.0.0"
                    ),
                    result_kind="table",
                    data_schema_ref=(
                        "schema://data/enterprise-metric-table/1.0.0"
                    ),
                    cache_enabled=True,
                    ttl_seconds=60,
                    action="governance.enterprise.aggregate.read",
                    denial_scope="dataset_area_fields",
                    adapter_ref="adapter://geo-qxst/enterprise-metrics/1.0",
                    additional_data_schema_refs=(
                        "schema://data/enterprise-type-distribution-table/1.0.0",
                        "schema://data/enterprise-scale-distribution-table/1.0.0",
                        "schema://data/enterprise-industry-distribution-table/1.0.0",
                    ),
                ),
                _manifest(
                    tool_id="governance.resolve_area",
                    risk_level="low",
                    dataset_id="administrative_area",
                    classifications=["public", "internal"],
                    permission="governance.area.read",
                    input_schema_ref="schema://tools/resolve-area-input/1.0.0",
                    result_kind="area_candidates",
                    data_schema_ref="schema://data/area-candidates/1.0.0",
                    cache_enabled=True,
                    ttl_seconds=300,
                    action="governance.area.read",
                    denial_scope="dataset_area_fields",
                    adapter_ref="adapter://geo-qxst/resolve-area/1.0",
                ),
                _manifest(
                    tool_id="governance.query_population_metrics",
                    risk_level="low",
                    dataset_id="population",
                    classifications=["internal", "aggregated"],
                    permission="governance.population.aggregate.read",
                    input_schema_ref=(
                        "schema://tools/query-population-metrics-input/1.0.0"
                    ),
                    result_kind="table",
                    data_schema_ref=(
                        "schema://data/population-metric-table/1.0.0"
                    ),
                    additional_data_schema_refs=(
                        "schema://data/population-ranking-table/1.0.0",
                        "schema://data/population-aggregate-table/1.0.0",
                    ),
                    cache_enabled=True,
                    ttl_seconds=60,
                    action="governance.population.aggregate.read",
                    denial_scope="dataset_area_fields",
                    adapter_ref="adapter://geo-qxst/population-metrics/1.0",
                ),
                _manifest(
                    tool_id="governance.query_housing_metrics",
                    risk_level="low",
                    dataset_id="housing",
                    classifications=["internal", "aggregated"],
                    permission="governance.housing.aggregate.read",
                    input_schema_ref=(
                        "schema://tools/query-housing-metrics-input/1.0.0"
                    ),
                    result_kind="table",
                    data_schema_ref=(
                        "schema://data/housing-lease-type-table/1.0.0"
                    ),
                    additional_data_schema_refs=(
                        "schema://data/housing-room-use-table/1.0.0",
                        "schema://data/housing-area-group-table/1.0.0",
                        "schema://data/housing-stock-overview/1.0.0",
                    )
                    if housing_next_area_enabled
                    else (
                        "schema://data/housing-room-use-table/1.0.0",
                        "schema://data/housing-stock-overview/1.0.0",
                    ),
                    cache_enabled=True,
                    ttl_seconds=60,
                    action="governance.housing.aggregate.read",
                    denial_scope="dataset_area_fields",
                    adapter_ref="adapter://geo-qxst/housing-metrics/1.0",
                ),
                _manifest(
                    tool_id="governance.query_event_metrics",
                    risk_level="low",
                    dataset_id="event",
                    classifications=["internal", "aggregated"],
                    permission="governance.event.aggregate.read",
                    input_schema_ref=(
                        "schema://tools/query-event-metrics-input/1.0.0"
                    ),
                    result_kind="table",
                    data_schema_ref=(
                        "schema://data/event-finish-rate-table/1.0.0"
                    ),
                    additional_data_schema_refs=(
                        "schema://data/event-trend-table/1.0.0",
                    )
                    + (
                        ("schema://data/event-category-table/1.0.0",)
                        if event_category_enabled
                        else ()
                    ),
                    cache_enabled=True,
                    ttl_seconds=60,
                    action="governance.event.aggregate.read",
                    denial_scope="dataset_area_fields",
                    adapter_ref="adapter://geo-qxst/event-metrics/1.0",
                ),
                _manifest(
                    tool_id="governance.get_object_profile",
                    risk_level="medium",
                    dataset_id="governance_objects",
                    classifications=["internal", "sensitive"],
                    permission="governance.object.profile.read",
                    input_schema_ref="schema://tools/get-object-profile-input/1.0.0",
                    result_kind="object_profile",
                    data_schema_ref="schema://data/object-profile/1.0.0",
                    cache_enabled=False,
                    ttl_seconds=0,
                    action="governance.object.profile.read",
                    denial_scope="object_fields",
                    adapter_ref="adapter://geo-qxst/object-profile/1.0",
                ),
            ],
            descriptors=[
                _descriptor(
                    tool_id="knowledge.search",
                    name="检索当前应用知识库",
                    description=(
                        "检索当前租户、应用及用户权限内已发布且索引就绪的知识库。"
                        "返回文档、知识库版本、片段、页码或段落定位，回答必须保留引用。"
                    ),
                    schema_ref="schema://tools/knowledge-search-input/1.0.0",
                ),
                _descriptor(
                    tool_id="governance.query_governance_power_metrics",
                    name="查询治理力量汇总",
                    description=(
                        "查询授权区域的户数、网格长、网格指导员、专职和兼职网格员、"
                        "其他网格力量及微网格汇总，不返回姓名、电话或个人明细。"
                    ),
                    schema_ref="schema://tools/query-governance-power-metrics-input/1.0.0",
                ),
                _descriptor(
                    tool_id="governance.get_governance_overview",
                    name="查询区域治理总览",
                    description=(
                        "查询授权区域的人、房、企、事、物治理关联数、要素总数和治理覆盖率。"
                        "使用全量信息视图当前四平台治理口径，不返回对象明细。"
                    ),
                    schema_ref=(
                        "schema://tools/query-governance-overview-input/1.0.0"
                    ),
                ),
                _descriptor(
                    tool_id="governance.query_enterprise_metrics",
                    name="查询企业区划分布",
                    description=(
                        "查询授权区域内企业聚合数量，不返回企业明细。"
                        "group_by=[next_area] 时按直接下级区划返回，"
                        "结果可用于表格、柱状图和区划分色图；"
                        "group_by=[enterprise_type] 时按旧接口返回最多 8 类企业类型，"
                        "企业类型须经 enterprise_type 字典映射为中文，"
                        "结果仅用于表格和柱状图；"
                        "group_by=[enterprise_scale] 时按从业人数返回五档企业规模，"
                        "旧接口的 10-50人、50-100人实际边界分别为"
                        "11-50人、51-100人，且从业人数为空的企业不计入；"
                        "group_by=[industry_name] 时按旧接口返回最多 8 类行业名称，"
                        "行业名称直接来自上游响应，无须字典映射，"
                        "结果仅用于表格和柱状图。"
                    ),
                    schema_ref=(
                        "schema://tools/query-enterprise-metrics-input/1.0.0"
                    ),
                ),
                _descriptor(
                    tool_id="governance.resolve_area",
                    name="解析区划",
                    description="将区划名称或当前区域表达解析为授权范围内的标准区划候选。",
                    schema_ref="schema://tools/resolve-area-input/1.0.0",
                ),
                _descriptor(
                    tool_id="governance.query_population_metrics",
                    name="查询人口聚合指标",
                    description=(
                        "查询当前授权区域的一般人口或独居老人聚合指标，不返回个人明细。"
                        "一般人口不传 filters；独居老人传 [{field: person_category, "
                        "operator: eq, value: solitary_elderly}]。不支持年龄或性别统计；"
                        "group_by 必须是区域的直接下一级，"
                        "区县传 [street]、街道传 [community]、社区传 [grid]。"
                    ),
                    schema_ref=(
                        "schema://tools/query-population-metrics-input/1.0.0"
                    ),
                ),
                _descriptor(
                    tool_id="governance.query_housing_metrics",
                    name="查询房屋聚合指标",
                    description=(
                        "查询授权区域的房屋聚合统计，不返回房屋明细。"
                        "metrics=['building_count','room_count'] 且不分组时，"
                        "返回区域楼幢总数和户室总数；"
                        "group_by=['room_use'] 时返回户室用途分类数量；"
                        + (
                            "不传 group_by 时返回区域自身按租赁类型的汇总；"
                            "group_by=['next_area'] 时返回直接下级区划"
                            "（全市按区县、区县按街道、街道按社区、社区按网格）"
                            "的出租房数量分布。"
                            if housing_next_area_enabled
                            else "返回区域自身按租赁类型的汇总。"
                        )
                        + "不支持其他分组、筛选、排序。"
                    ),
                    schema_ref=(
                        "schema://tools/query-housing-metrics-input/1.0.0"
                    ),
                ),
                _descriptor(
                    tool_id="governance.query_event_metrics",
                    name="查询治理事件指标",
                    description=(
                        "查询授权区域的三层办结率快照，或在显式日期"
                        "范围内查询按月汇总的事件总数趋势。趋势缺失"
                        "月份按 0 补齐，不表示上报或处置趋势。"
                        + (
                            "也支持按现有主题块口径统计网格事件一级分类，"
                            "不代表全量事件。"
                            if event_category_enabled
                            else ""
                        )
                    ),
                    schema_ref=(
                        "schema://tools/query-event-metrics-input/1.0.0"
                    ),
                ),
                _descriptor(
                    tool_id="governance.get_object_profile",
                    name="查询治理对象画像",
                    description=(
                        "查询授权区域内单个楼栋的基础画像和位置。"
                        "调用时必须提供声明区域 scope；当前真实适配器仅支持 building。"
                    ),
                    schema_ref="schema://tools/get-object-profile-input/1.0.0",
                ),
            ],
            housing_next_area_enabled=housing_next_area_enabled,
            event_category_enabled=event_category_enabled,
        )

    @classmethod
    def production_http(
        cls,
        *,
        housing_next_area_enabled: bool = False,
        event_category_enabled: bool = False,
    ) -> "ToolRegistry":
        return cls.default(
            housing_next_area_enabled=housing_next_area_enabled,
            event_category_enabled=event_category_enabled,
        ).subset(set(PRODUCTION_HTTP_TOOL_IDS))

    @property
    def housing_next_area_enabled(self) -> bool:
        return self._housing_next_area_enabled

    @property
    def event_category_enabled(self) -> bool:
        return self._event_category_enabled

    def get_manifest(self, tool_id: str) -> InternalToolManifest:
        with self._lock:
            manifest = self._manifests.get(tool_id)
        if manifest is None:
            raise ResourceNotFound("tool not found")
        return manifest

    def get_model_descriptor(self, tool_id: str) -> ModelToolDescriptor:
        with self._lock:
            descriptor = self._descriptors.get(tool_id)
        if descriptor is None:
            raise ResourceNotFound("tool not found")
        return descriptor

    def get_semantic_contract(self, tool_id: str) -> ToolSemanticContract:
        manifest = self.get_manifest(tool_id)
        if manifest.semantic_contract is None:
            raise ResourceNotFound("tool semantic contract not found")
        return manifest.semantic_contract

    def list_tool_ids(self) -> list[str]:
        with self._lock:
            return sorted(self._manifests)

    def get_input_schema(self, tool_id: str) -> dict[str, object]:
        self.get_manifest(tool_id)
        # Check if this is a dynamic tool with a custom input schema
        with self._lock:
            dynamic_schema = self._dynamic_input_schemas.get(tool_id)
        if dynamic_schema is not None:
            return deepcopy(dynamic_schema)
        # Otherwise use the static input model
        schema = _INPUT_MODELS[tool_id].model_json_schema(mode="validation")
        if (
            tool_id == "governance.query_housing_metrics"
            and not self._housing_next_area_enabled
        ):
            schema = deepcopy(schema)
            query_schema = schema.get("$defs", {}).get("HousingMetricQuerySpec", {})
            group_by_schema = query_schema.get("properties", {}).get("group_by", {})
            items = group_by_schema.get("items")
            if isinstance(items, dict):
                items.pop("const", None)
                items["enum"] = ["room_use"]
        if (
            tool_id == "governance.query_event_metrics"
            and not self._event_category_enabled
        ):
            schema = deepcopy(schema)
            query_schema = schema.get("$defs", {}).get("EventMetricQuerySpec", {})
            group_by_schema = query_schema.get("properties", {}).get("group_by", {})
            items = group_by_schema.get("items")
            if isinstance(items, dict) and isinstance(items.get("enum"), list):
                items["enum"] = [
                    value for value in items["enum"] if value != "event_category"
                ]
        return schema

    def subset(self, tool_ids: set[str]) -> "ToolRegistry":
        with self._lock:
            unknown = tool_ids.difference(self._manifests)
            if unknown:
                raise ResourceNotFound("tool not found")
            subset_dynamic_schemas = {
                tool_id: schema
                for tool_id, schema in self._dynamic_input_schemas.items()
                if tool_id in tool_ids
            }
            return ToolRegistry(
                manifests=[self._manifests[tool_id] for tool_id in sorted(tool_ids)],
                descriptors=[
                    self._descriptors[tool_id] for tool_id in sorted(tool_ids)
                ],
                housing_next_area_enabled=self._housing_next_area_enabled,
                event_category_enabled=self._event_category_enabled,
                dynamic_input_schemas=subset_dynamic_schemas,
            )

    def snapshot(self) -> "ToolRegistry":
        """Return an immutable-by-convention copy of the current generation."""

        with self._lock:
            return ToolRegistry(
                manifests=list(self._manifests.values()),
                descriptors=list(self._descriptors.values()),
                housing_next_area_enabled=self._housing_next_area_enabled,
                event_category_enabled=self._event_category_enabled,
                dynamic_input_schemas=deepcopy(self._dynamic_input_schemas),
            )

    def replace_dynamic(
        self,
        *,
        manifests: list[InternalToolManifest],
        descriptors: list[ModelToolDescriptor],
        dynamic_input_schemas: dict[str, dict[str, object]] | None = None,
    ) -> None:
        """Atomically replace the published dynamic layer on this live object.

        Existing holders keep the same registry reference.  Per-Run callers
        must use :meth:`snapshot` (or ``merge_dynamic``, which returns a new
        registry) to pin a generation.
        """

        manifest_map = dict(self._baseline_manifests)
        descriptor_map = dict(self._baseline_descriptors)
        for manifest in manifests:
            manifest_map[manifest.tool_id] = manifest
        for descriptor in descriptors:
            descriptor_map[descriptor.tool_id] = descriptor
        if set(manifest_map) != set(descriptor_map):
            raise ValueError("Tool manifests and descriptors must have identical IDs")
        schema_map = dict(self._baseline_dynamic_input_schemas)
        schema_map.update(dynamic_input_schemas or {})
        unknown_schema_ids = set(schema_map).difference(manifest_map)
        if unknown_schema_ids:
            raise ValueError("dynamic input schema references an unavailable Tool")
        with self._lock:
            self._manifests = manifest_map
            self._descriptors = descriptor_map
            self._dynamic_input_schemas = schema_map

    def replace_with(self, registry: "ToolRegistry") -> None:
        """Atomically expose another validated registry generation."""

        candidate = registry.snapshot()
        with candidate._lock:
            manifests = dict(candidate._manifests)
            descriptors = dict(candidate._descriptors)
            schemas = deepcopy(candidate._dynamic_input_schemas)
        if set(manifests) != set(descriptors):
            raise ValueError("Tool manifests and descriptors must have identical IDs")
        with self._lock:
            self._manifests = manifests
            self._descriptors = descriptors
            self._dynamic_input_schemas = schemas

    def merge_dynamic(
        self,
        *,
        manifests: list[InternalToolManifest],
        descriptors: list[ModelToolDescriptor],
        dynamic_input_schemas: dict[str, dict[str, object]] | None = None,
    ) -> "ToolRegistry":
        """Create a new registry that merges static and dynamic tools.

        Dynamic tools are added to the registry alongside static tools. If a
        dynamic tool has the same tool_id as a static tool, the dynamic tool
        takes precedence (allows overriding static tools). The adapter routing
        in ``CapabilityService.execute`` is the authoritative layer that
        ensures built-in tool IDs still execute through the vetted
        GovernanceAdapter regardless of manifest overrides — see the built-in
        priority rule enforced there.

        Returns a new ToolRegistry; the original is not modified.
        """
        # Merge manifests (dynamic takes precedence on conflict).
        with self._lock:
            merged_manifests = dict(self._manifests)
            for manifest in manifests:
                merged_manifests[manifest.tool_id] = manifest
            merged_descriptors = dict(self._descriptors)
            for descriptor in descriptors:
                merged_descriptors[descriptor.tool_id] = descriptor
            merged_dynamic_schemas = dict(self._dynamic_input_schemas)
            if dynamic_input_schemas:
                merged_dynamic_schemas.update(dynamic_input_schemas)

        return ToolRegistry(
            manifests=list(merged_manifests.values()),
            descriptors=list(merged_descriptors.values()),
            housing_next_area_enabled=self._housing_next_area_enabled,
            event_category_enabled=self._event_category_enabled,
            dynamic_input_schemas=merged_dynamic_schemas,
        )


def _manifest(
    *,
    tool_id: str,
    risk_level: str,
    dataset_id: str,
    classifications: list[str],
    permission: str,
    input_schema_ref: str,
    result_kind: str,
    data_schema_ref: str,
    cache_enabled: bool,
    ttl_seconds: int,
    action: str,
    denial_scope: str,
    adapter_ref: str,
    additional_data_schema_refs: tuple[str, ...] = (),
    semantic_contract: dict[str, object] | None = None,
) -> InternalToolManifest:
    return InternalToolManifest.model_validate(
        {
            "tool_id": tool_id,
            "tool_version": "1.0.0",
            "owner": "full-information-domain-team",
            "risk_level": risk_level,
            "dataset_id": dataset_id,
            "data_classifications": classifications,
            "required_permissions": [permission],
            "input_schema_ref": input_schema_ref,
            "result_schemas": [
                {"kind": result_kind, "data_schema_ref": data_schema_ref},
                *[
                    {"kind": result_kind, "data_schema_ref": extra_ref}
                    for extra_ref in additional_data_schema_refs
                ],
            ],
            "limits": {
                "timeout_ms": 8000,
                "max_attempts": 2,
                "max_result_rows": 1000,
                "max_group_buckets": 200,
            },
            "cache_policy": {
                "enabled": cache_enabled,
                "ttl_seconds": ttl_seconds,
            },
            "policy": {
                "action": action,
                "pre_check": True,
                "post_filter": True,
                "denial_scope": denial_scope,
            },
            "adapter_ref": adapter_ref,
            "semantic_contract": semantic_contract,
        }
    )


def _descriptor(
    *,
    tool_id: str,
    name: str,
    description: str,
    schema_ref: str,
) -> ModelToolDescriptor:
    return ModelToolDescriptor.model_validate(
        {
            "tool_id": tool_id,
            "tool_version": "1.0.0",
            "name": name,
            "description": description,
            "input_schema": {"$ref": schema_ref},
        }
    )
