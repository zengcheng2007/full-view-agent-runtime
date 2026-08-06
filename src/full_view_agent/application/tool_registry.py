from copy import deepcopy

from full_view_agent.application.errors import ResourceNotFound
from full_view_agent.domain.models import (
    GetObjectProfileInput,
    InternalToolManifest,
    ModelToolDescriptor,
    QueryEventMetricsInput,
    QueryHousingMetricsInput,
    QueryPopulationMetricsInput,
    ResolveAreaInput,
)

_INPUT_MODELS = {
    "governance.resolve_area": ResolveAreaInput,
    "governance.query_population_metrics": QueryPopulationMetricsInput,
    "governance.query_housing_metrics": QueryHousingMetricsInput,
    "governance.query_event_metrics": QueryEventMetricsInput,
    "governance.get_object_profile": GetObjectProfileInput,
}

PRODUCTION_HTTP_TOOL_IDS = (
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
        dynamic_input_schemas: dict[str, dict[str, object]] | None = None,
    ) -> None:
        self._manifests = {manifest.tool_id: manifest for manifest in manifests}
        self._descriptors = {
            descriptor.tool_id: descriptor for descriptor in descriptors
        }
        self._housing_next_area_enabled = housing_next_area_enabled
        self._dynamic_input_schemas = dynamic_input_schemas or {}

    @classmethod
    def default(
        cls, *, housing_next_area_enabled: bool = True
    ) -> "ToolRegistry":
        return cls(
            manifests=[
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
                        "schema://data/housing-area-group-table/1.0.0",
                    )
                    if housing_next_area_enabled
                    else (),
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
                    tool_id="governance.resolve_area",
                    name="解析区划",
                    description="将区划名称或当前区域表达解析为授权范围内的标准区划候选。",
                    schema_ref="schema://tools/resolve-area-input/1.0.0",
                ),
                _descriptor(
                    tool_id="governance.query_population_metrics",
                    name="查询独居老人指标",
                    description=(
                        "仅查询当前授权区域的独居老人聚合指标，不返回个人明细，"
                        "不支持通用人口、年龄或性别统计。filters 必须为"
                        " [{field: person_category, operator: eq, value: "
                        "solitary_elderly}]；group_by 必须是区域的直接下一级，"
                        "区县传 [street]、街道传 [community]、社区传 [grid]。"
                    ),
                    schema_ref=(
                        "schema://tools/query-population-metrics-input/1.0.0"
                    ),
                ),
                _descriptor(
                    tool_id="governance.query_housing_metrics",
                    name="查询出租房指标",
                    description=(
                        "查询授权区域的出租房聚合统计，不返回个人明细。"
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
                    name="查询网格事件办结率",
                    description=(
                        "查询指定授权区域自身的网格、社区、街道三个层级汇总办结率。"
                        "当前数据源不返回下级区划明细、事件总量或办结数，"
                        "也不支持按阈值筛选。"
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
        )

    @classmethod
    def production_http(
        cls, *, housing_next_area_enabled: bool = False
    ) -> "ToolRegistry":
        return cls.default(
            housing_next_area_enabled=housing_next_area_enabled
        ).subset(set(PRODUCTION_HTTP_TOOL_IDS))

    @property
    def housing_next_area_enabled(self) -> bool:
        return self._housing_next_area_enabled

    def get_manifest(self, tool_id: str) -> InternalToolManifest:
        manifest = self._manifests.get(tool_id)
        if manifest is None:
            raise ResourceNotFound("tool not found")
        return manifest

    def get_model_descriptor(self, tool_id: str) -> ModelToolDescriptor:
        descriptor = self._descriptors.get(tool_id)
        if descriptor is None:
            raise ResourceNotFound("tool not found")
        return descriptor

    def list_tool_ids(self) -> list[str]:
        return sorted(self._manifests)

    def get_input_schema(self, tool_id: str) -> dict[str, object]:
        self.get_manifest(tool_id)
        # Check if this is a dynamic tool with a custom input schema
        if tool_id in self._dynamic_input_schemas:
            return self._dynamic_input_schemas[tool_id]
        # Otherwise use the static input model
        schema = _INPUT_MODELS[tool_id].model_json_schema(mode="validation")
        if (
            tool_id == "governance.query_housing_metrics"
            and not self._housing_next_area_enabled
        ):
            schema = deepcopy(schema)
            query_schema = schema.get("$defs", {}).get("HousingMetricQuerySpec", {})
            query_schema.get("properties", {}).pop("group_by", None)
        return schema

    def subset(self, tool_ids: set[str]) -> "ToolRegistry":
        unknown = tool_ids.difference(self._manifests)
        if unknown:
            raise ResourceNotFound("tool not found")
        # Preserve dynamic input schemas for the subset
        subset_dynamic_schemas = {
            tool_id: schema
            for tool_id, schema in self._dynamic_input_schemas.items()
            if tool_id in tool_ids
        }
        return ToolRegistry(
            manifests=[self._manifests[tool_id] for tool_id in sorted(tool_ids)],
            descriptors=[self._descriptors[tool_id] for tool_id in sorted(tool_ids)],
            housing_next_area_enabled=self._housing_next_area_enabled,
            dynamic_input_schemas=subset_dynamic_schemas,
        )

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
        takes precedence (allows overriding static tools).

        Returns a new ToolRegistry; the original is not modified.
        """
        # Merge manifests (dynamic takes precedence on conflict)
        merged_manifests = dict(self._manifests)
        for manifest in manifests:
            merged_manifests[manifest.tool_id] = manifest

        # Merge descriptors (dynamic takes precedence on conflict)
        merged_descriptors = dict(self._descriptors)
        for descriptor in descriptors:
            merged_descriptors[descriptor.tool_id] = descriptor

        # Merge dynamic input schemas
        merged_dynamic_schemas = dict(self._dynamic_input_schemas)
        if dynamic_input_schemas:
            merged_dynamic_schemas.update(dynamic_input_schemas)

        return ToolRegistry(
            manifests=list(merged_manifests.values()),
            descriptors=list(merged_descriptors.values()),
            housing_next_area_enabled=self._housing_next_area_enabled,
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
