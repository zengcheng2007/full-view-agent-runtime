import asyncio
import re
from collections.abc import Collection, Mapping
from datetime import date
from typing import Any, Literal, Protocol, cast

import httpx
from pydantic import BaseModel, SecretStr

from full_view_agent.application.authorization_scope import area_is_within_scope
from full_view_agent.application.errors import (
    ReauthenticationRequired,
    SemanticValidationError,
    UpstreamContractError,
    UpstreamTimeout,
    UpstreamUnavailable,
)
from full_view_agent.application.fingerprints import canonical_fingerprint
from full_view_agent.application.session_run_service import new_id
from full_view_agent.domain.models import (
    AreaCandidate,
    AreaCandidatesData,
    AreaCandidatesResult,
    AuthContext,
    DataResult,
    EnterpriseIndustryDistributionRow,
    EnterpriseIndustryDistributionTable,
    EnterpriseMetricRow,
    EnterpriseMetricTable,
    EnterpriseScaleDistributionRow,
    EnterpriseScaleDistributionTable,
    EnterpriseTypeDistributionRow,
    EnterpriseTypeDistributionTable,
    EventCategoryRow,
    EventCategoryTable,
    EventFinishRateRow,
    EventFinishRateTable,
    EventTrendRow,
    EventTrendTable,
    GetObjectProfileInput,
    GovernanceOverviewRow,
    GovernanceOverviewTable,
    GovernancePowerMetricRow,
    GovernancePowerMetricTable,
    HousingAreaGroupRow,
    HousingAreaGroupTable,
    HousingLeaseTypeRow,
    HousingLeaseTypeTable,
    HousingRoomUseRow,
    HousingRoomUseTable,
    HousingStockOverviewRow,
    HousingStockOverviewTable,
    InternalToolManifest,
    ObjectProfileData,
    ObjectProfileField,
    ObjectProfileResult,
    PolicyDecision,
    PopulationAggregateRow,
    PopulationAggregateTable,
    PopulationMetricRow,
    PopulationMetricTable,
    PopulationRankingRow,
    PopulationRankingTable,
    QueryEnterpriseMetricsInput,
    QueryEventMetricsInput,
    QueryGovernanceOverviewInput,
    QueryGovernancePowerMetricsInput,
    QueryHousingMetricsInput,
    QueryPopulationMetricsInput,
    ResolveAreaInput,
    TableDataResult,
)

# Canonical Tools with a verified production HTTP dispatch branch. The Registry
# controls exposure; the capability consistency Gate requires an exact match.
PRODUCTION_HTTP_ADAPTER_TOOL_IDS = frozenset(
    {
        "governance.get_governance_overview",
        "governance.query_governance_power_metrics",
        "governance.query_enterprise_metrics",
        "governance.resolve_area",
        "governance.query_population_metrics",
        "governance.query_housing_metrics",
        "governance.query_event_metrics",
        "governance.get_object_profile",
    }
)


class InMemoryGovernanceAdapter:
    def __init__(self) -> None:
        self._areas = [
            AreaCandidate(
                area_code="3301",
                area_name="杭州市",
                level="city",
                parent_area_code=None,
                bounds=(118.35, 29.18, 120.72, 30.58),
            ),
            AreaCandidate(
                area_code="330106",
                area_name="西湖区",
                level="district",
                parent_area_code="330100",
                bounds=(120.02, 30.05, 120.20, 30.35),
            ),
            AreaCandidate(
                area_code="330106001",
                area_name="翠苑街道",
                level="street",
                parent_area_code="330106",
                bounds=(120.10, 30.27, 120.16, 30.31),
            ),
            AreaCandidate(
                area_code="330105",
                area_name="拱墅区",
                level="district",
                parent_area_code="330100",
                bounds=(120.10, 30.25, 120.25, 30.40),
            ),
        ]

    async def execute(
        self,
        *,
        manifest: InternalToolManifest,
        arguments: BaseModel,
        policy_decision: PolicyDecision,
        auth_context: AuthContext,
    ) -> DataResult:
        del auth_context
        if manifest.tool_id == "governance.resolve_area":
            if not isinstance(arguments, ResolveAreaInput):
                raise TypeError("resolve_area requires ResolveAreaInput")
            return self._resolve_area(arguments, policy_decision)
        if manifest.tool_id == "governance.query_population_metrics":
            if not isinstance(arguments, QueryPopulationMetricsInput):
                raise TypeError(
                    "query_population_metrics requires QueryPopulationMetricsInput"
                )
            return self._query_population_metrics(arguments)
        if manifest.tool_id == "governance.query_housing_metrics":
            if not isinstance(arguments, QueryHousingMetricsInput):
                raise TypeError(
                    "query_housing_metrics requires QueryHousingMetricsInput"
                )
            return self._query_housing_metrics(arguments)
        if manifest.tool_id == "governance.query_event_metrics":
            if not isinstance(arguments, QueryEventMetricsInput):
                raise TypeError(
                    "query_event_metrics requires QueryEventMetricsInput"
                )
            return self._query_event_metrics(arguments)
        if manifest.tool_id == "governance.query_enterprise_metrics":
            if not isinstance(arguments, QueryEnterpriseMetricsInput):
                raise TypeError(
                    "query_enterprise_metrics requires QueryEnterpriseMetricsInput"
                )
            return self._query_enterprise_metrics(arguments)
        if manifest.tool_id == "governance.get_governance_overview":
            if not isinstance(arguments, QueryGovernanceOverviewInput):
                raise TypeError(
                    "get_governance_overview requires QueryGovernanceOverviewInput"
                )
            return self._get_governance_overview(arguments)
        if manifest.tool_id == "governance.query_governance_power_metrics":
            if not isinstance(arguments, QueryGovernancePowerMetricsInput):
                raise TypeError(
                    "query_governance_power_metrics requires "
                    "QueryGovernancePowerMetricsInput"
                )
            return self._query_governance_power_metrics(arguments)
        if manifest.tool_id == "governance.get_object_profile":
            if not isinstance(arguments, GetObjectProfileInput):
                raise TypeError(
                    "get_object_profile requires GetObjectProfileInput"
                )
            return self._get_object_profile(arguments, policy_decision)
        raise NotImplementedError(f"adapter not implemented for {manifest.tool_id}")

    def _resolve_area(
        self,
        arguments: ResolveAreaInput,
        policy_decision: PolicyDecision,
    ) -> AreaCandidatesResult:
        allowed_area_codes = policy_decision.effective_scope.area_codes
        normalized_query = arguments.query.replace("杭州全市", "杭州市")
        candidates = [
            area
            for area in self._areas
            if normalized_query in area.area_name
            and _area_in_scope(area.area_code, allowed_area_codes)
            and (
                arguments.parent_area_code is None
                or area.parent_area_code == arguments.parent_area_code
            )
        ][: arguments.max_candidates]
        data = AreaCandidatesData(
            resolved_area_code=(candidates[0].area_code if len(candidates) == 1 else None),
            ambiguous=len(candidates) > 1,
            candidates=candidates,
        )
        return AreaCandidatesResult(
            result_id=new_id("res"),
            data_schema_ref="schema://data/area-candidates/1.0.0",
            result_fingerprint=canonical_fingerprint(
                domain="data-result:area-candidates:1.0.0",
                value=data,
            ),
            data=data,
            candidate_count=len(candidates),
        )

    @staticmethod
    def _query_population_metrics(
        arguments: QueryPopulationMetricsInput,
    ) -> TableDataResult:
        area_code = arguments.query.scope.area_code
        _validate_population_query(arguments)
        return _population_result(
            arguments=arguments,
            parsed_rows=[(f"{area_code}001", "示例街道", 128)],
            upstream_truncated=False,
            ranking=arguments.query.group_by[0] in {
                "district", "descendant_street", "descendant_community"
            },
        )

    @staticmethod
    def _query_event_metrics(
        arguments: QueryEventMetricsInput,
    ) -> TableDataResult:
        if arguments.query.group_by == ["event_category"]:
            rows = [
                EventCategoryRow(
                    category_code="01",
                    category_name="社会治理",
                    event_count=12,
                ),
                EventCategoryRow(
                    category_code="02",
                    category_name="公共安全",
                    event_count=7,
                ),
            ]
            data = EventCategoryTable(rows=rows)
            return TableDataResult(
                result_id=new_id("res"),
                data_schema_ref="schema://data/event-category-table/1.0.0",
                result_fingerprint=canonical_fingerprint(
                    domain="data-result:event-category-table:1.0.0", value=data
                ),
                data=data,
                row_count=len(rows),
            )
        if arguments.query.metrics == ["event_count"]:
            if arguments.query.time_range is None:
                raise SemanticValidationError(
                    "event trend requires an explicit time range"
                )
            rows = [
                EventTrendRow(month=month, event_count=index + 1)
                for index, month in enumerate(
                    _calendar_months(
                        arguments.query.time_range.start,
                        arguments.query.time_range.end,
                    )
                )
            ]
            data = EventTrendTable(rows=rows)
            return TableDataResult(
                result_id=new_id("res"),
                data_schema_ref="schema://data/event-trend-table/1.0.0",
                result_fingerprint=canonical_fingerprint(
                    domain="data-result:event-trend-table:1.0.0",
                    value=data,
                ),
                data=data,
                row_count=len(rows),
            )
        rows = [
            EventFinishRateRow(level="grid", finish_rate=85),
            EventFinishRateRow(level="community", finish_rate=72),
            EventFinishRateRow(level="street", finish_rate=90),
        ]
        data = EventFinishRateTable(rows=rows)
        return TableDataResult(
            result_id=new_id("res"),
            data_schema_ref="schema://data/event-finish-rate-table/1.0.0",
            result_fingerprint=canonical_fingerprint(
                domain="data-result:event-metric-table:1.0.0",
                value=data,
            ),
            data=data,
            row_count=len(rows),
        )

    @staticmethod
    def _get_governance_overview(
        arguments: QueryGovernanceOverviewInput,
    ) -> TableDataResult:
        _validate_governance_overview_query(arguments)
        rows = [
            GovernanceOverviewRow(
                subject="person", subject_label="人",
                related_count=80, total_count=100, coverage_rate=80,
            ),
            GovernanceOverviewRow(
                subject="house", subject_label="房",
                related_count=45, total_count=50, coverage_rate=90,
            ),
            GovernanceOverviewRow(
                subject="enterprise", subject_label="企",
                related_count=18, total_count=20, coverage_rate=90,
            ),
            GovernanceOverviewRow(
                subject="event", subject_label="事",
                related_count=12, total_count=15, coverage_rate=80,
            ),
            GovernanceOverviewRow(
                subject="matter", subject_label="物",
                related_count=8, total_count=10, coverage_rate=80,
            ),
        ]
        data = GovernanceOverviewTable(rows=rows)
        return TableDataResult(
            result_id=new_id("res"),
            data_schema_ref="schema://data/governance-overview-table/1.0.0",
            result_fingerprint=canonical_fingerprint(
                domain="data-result:governance-overview-table:1.0.0", value=data
            ),
            data=data,
            row_count=len(rows),
        )

    @staticmethod
    def _query_governance_power_metrics(
        arguments: QueryGovernancePowerMetricsInput,
    ) -> TableDataResult:
        _validate_area_code(
            arguments.query.scope.area_code,
            supported_lengths={4, 6, 9, 12, 15},
            capability_name="governance power",
        )
        rows = [
            GovernancePowerMetricRow(type_code="roomNum", type_name="户数", count=409_600),
            GovernancePowerMetricRow(type_code="10", type_name="网格长", count=191),
            GovernancePowerMetricRow(type_code="11", type_name="网格指导员", count=191),
            GovernancePowerMetricRow(type_code="12", type_name="专职网格员", count=860),
            GovernancePowerMetricRow(type_code="13", type_name="兼职网格员", count=420),
            GovernancePowerMetricRow(
                type_code="nGridSum", type_name="N其他网格力量", count=260
            ),
            GovernancePowerMetricRow(
                type_code="gridUnitSum", type_name="微网格", count=2_100
            ),
        ]
        data = GovernancePowerMetricTable(rows=rows)
        return TableDataResult(
            result_id=new_id("res"),
            data_schema_ref="schema://data/governance-power-metric-table/1.0.0",
            result_fingerprint=canonical_fingerprint(
                domain="data-result:governance-power-metric-table:1.0.0",
                value=data,
            ),
            data=data,
            row_count=len(rows),
        )

    @staticmethod
    def _query_enterprise_metrics(
        arguments: QueryEnterpriseMetricsInput,
    ) -> TableDataResult:
        if arguments.query.group_by == ["enterprise_scale"]:
            rows = [
                EnterpriseScaleDistributionRow(
                    enterprise_scale=label,
                    enterprise_count=count,
                )
                for label, count in (
                    ("5人以下", 11),
                    ("5-10人", 7),
                    ("11-50人", 5),
                    ("51-100人", 3),
                    ("100人以上", 2),
                )
            ]
            data = EnterpriseScaleDistributionTable(rows=rows)
            return TableDataResult(
                result_id=new_id("res"),
                data_schema_ref=(
                    "schema://data/enterprise-scale-distribution-table/1.0.0"
                ),
                result_fingerprint=canonical_fingerprint(
                    domain=(
                        "data-result:enterprise-scale-distribution-table:1.0.0"
                    ),
                    value=data,
                ),
                data=data,
                row_count=len(rows),
            )
        if arguments.query.group_by == ["enterprise_type"]:
            source_rows = [
                EnterpriseTypeDistributionRow(
                    enterprise_type="有限责任公司",
                    enterprise_count=31,
                ),
                EnterpriseTypeDistributionRow(
                    enterprise_type="股份有限公司",
                    enterprise_count=18,
                ),
            ]
            rows = source_rows[: arguments.query.limit]
            data = EnterpriseTypeDistributionTable(rows=rows)
            return TableDataResult(
                result_id=new_id("res"),
                data_schema_ref=(
                    "schema://data/enterprise-type-distribution-table/1.0.0"
                ),
                result_fingerprint=canonical_fingerprint(
                    domain=(
                        "data-result:enterprise-type-distribution-table:1.0.0"
                    ),
                    value=data,
                ),
                data=data,
                row_count=len(rows),
                truncated=len(source_rows) > arguments.query.limit,
            )
        if arguments.query.group_by == ["industry_name"]:
            source_rows = [
                EnterpriseIndustryDistributionRow(industry_name="零售业", enterprise_count=18),
                EnterpriseIndustryDistributionRow(industry_name="信息技术", enterprise_count=12),
                EnterpriseIndustryDistributionRow(industry_name="制造业", enterprise_count=8),
            ]
            rows = source_rows[: arguments.query.limit]
            data = EnterpriseIndustryDistributionTable(rows=rows)
            return TableDataResult(
                result_id=new_id("res"),
                data_schema_ref="schema://data/enterprise-industry-distribution-table/1.0.0",
                result_fingerprint=canonical_fingerprint(
                    domain="data-result:enterprise-industry-distribution-table:1.0.0",
                    value=data,
                ),
                data=data,
                row_count=len(rows),
                truncated=len(source_rows) > arguments.query.limit,
            )
        _validate_enterprise_next_area_query(arguments)
        area_code = arguments.query.scope.area_code
        child_label = _NEXT_AREA_CHILD_LABEL[len(area_code)]
        source_rows = [
            EnterpriseMetricRow(
                area_code=f"{area_code}001",
                area_name=f"示例{child_label}一",
                enterprise_count=31,
            ),
            EnterpriseMetricRow(
                area_code=f"{area_code}002",
                area_name=f"示例{child_label}二",
                enterprise_count=18,
            ),
        ]
        rows = source_rows[: arguments.query.limit]
        data = EnterpriseMetricTable(rows=rows)
        return TableDataResult(
            result_id=new_id("res"),
            data_schema_ref="schema://data/enterprise-metric-table/1.0.0",
            result_fingerprint=canonical_fingerprint(
                domain="data-result:enterprise-metric-table:1.0.0",
                value=data,
            ),
            data=data,
            row_count=len(rows),
            truncated=len(source_rows) > arguments.query.limit,
        )

    @staticmethod
    def _query_housing_metrics(
        arguments: QueryHousingMetricsInput,
    ) -> TableDataResult:
        if arguments.query.metrics == ["building_count", "room_count"]:
            data = HousingStockOverviewTable(
                rows=[
                    HousingStockOverviewRow(
                        building_count=12_800,
                        room_count=409_600,
                    )
                ]
            )
            return TableDataResult(
                result_id=new_id("res"),
                data_schema_ref="schema://data/housing-stock-overview/1.0.0",
                result_fingerprint=canonical_fingerprint(
                    domain="data-result:housing-stock-overview:1.0.0",
                    value=data,
                ),
                data=data,
                row_count=1,
            )
        if arguments.query.group_by in (["next_area"], ["descendant_street"]):
            return InMemoryGovernanceAdapter._query_housing_area_distribution(
                arguments
            )
        if arguments.query.group_by == ["room_use"]:
            source_rows = [
                HousingRoomUseRow(room_use="自住", dwelling_count=1800),
                HousingRoomUseRow(room_use="出租", dwelling_count=620),
                HousingRoomUseRow(room_use="空置", dwelling_count=95),
            ]
            rows = source_rows[: arguments.query.limit]
            data = HousingRoomUseTable(rows=rows)
            return TableDataResult(
                result_id=new_id("res"),
                data_schema_ref="schema://data/housing-room-use-table/1.0.0",
                result_fingerprint=canonical_fingerprint(
                    domain="data-result:housing-room-use-table:1.0.0",
                    value=data,
                ),
                data=data,
                row_count=len(rows),
                truncated=len(source_rows) > arguments.query.limit,
            )
        source_rows = [
            HousingLeaseTypeRow(lease_type="住宅出租", dwelling_count=3200),
            HousingLeaseTypeRow(lease_type="商铺出租", dwelling_count=850),
            HousingLeaseTypeRow(lease_type="公寓出租", dwelling_count=1200),
            HousingLeaseTypeRow(lease_type="群租房", dwelling_count=320),
            HousingLeaseTypeRow(lease_type="工业出租", dwelling_count=180),
        ]
        rows = source_rows[: arguments.query.limit]
        data = HousingLeaseTypeTable(rows=rows)
        return TableDataResult(
            result_id=new_id("res"),
            data_schema_ref="schema://data/housing-lease-type-table/1.0.0",
            result_fingerprint=canonical_fingerprint(
                domain="data-result:housing-metric-table:1.0.0",
                value=data,
            ),
            data=data,
            row_count=len(rows),
            truncated=len(source_rows) > arguments.query.limit,
        )

    @staticmethod
    def _query_housing_area_distribution(
        arguments: QueryHousingMetricsInput,
    ) -> TableDataResult:
        if arguments.query.group_by == ["descendant_street"]:
            _validate_housing_descendant_street_query(arguments)
            area_code = arguments.query.scope.area_code
            source_rows = [
                HousingAreaGroupRow(
                    area_code=f"{area_code}02001",
                    area_name="示例街道一",
                    dwelling_count=520,
                ),
                HousingAreaGroupRow(
                    area_code=f"{area_code}06001",
                    area_name="示例街道二",
                    dwelling_count=486,
                ),
            ]
        else:
            _validate_housing_next_area_query(arguments)
            area_code = arguments.query.scope.area_code
            child_label = _NEXT_AREA_CHILD_LABEL[len(area_code)]
            source_rows = [
                HousingAreaGroupRow(
                    area_code=f"{area_code}001",
                    area_name=f"示例{child_label}一",
                    dwelling_count=210,
                ),
                HousingAreaGroupRow(
                    area_code=f"{area_code}002",
                    area_name=f"示例{child_label}二",
                    dwelling_count=168,
                ),
            ]
        rows = source_rows[: arguments.query.limit]
        data = HousingAreaGroupTable(rows=rows)
        return TableDataResult(
            result_id=new_id("res"),
            data_schema_ref="schema://data/housing-area-group-table/1.0.0",
            result_fingerprint=canonical_fingerprint(
                domain="data-result:housing-area-group-table:1.0.0",
                value=data,
            ),
            data=data,
            row_count=len(rows),
            truncated=len(source_rows) > arguments.query.limit,
        )

    @staticmethod
    def _get_object_profile(
        arguments: GetObjectProfileInput,
        policy_decision: PolicyDecision,
    ) -> ObjectProfileResult:
        field_sets = set(policy_decision.effective_scope.allowed_field_sets)
        fields: list[ObjectProfileField] = []
        if "summary" in field_sets:
            fields.extend(
                [
                    ObjectProfileField(
                        field_id="display_name",
                        label="显示名称",
                        value="张某",
                        classification="internal",
                    ),
                    ObjectProfileField(
                        field_id="object_type",
                        label="对象类型",
                        value=arguments.object_ref.object_type,
                        classification="internal",
                    ),
                ]
            )
        if "demographics" in field_sets:
            fields.append(
                ObjectProfileField(
                    field_id="age",
                    label="年龄",
                    value=82,
                    classification="internal",
                )
            )
        if "contact" in field_sets:
            fields.append(
                ObjectProfileField(
                    field_id="phone",
                    label="联系电话",
                    value="13800000000",
                    classification="sensitive",
                )
            )
        data = ObjectProfileData(
            object_ref=arguments.object_ref,
            area_code="330106001",
            title="张某",
            fields=fields,
        )
        return ObjectProfileResult(
            result_id=new_id("res"),
            data_schema_ref="schema://data/object-profile/1.0.0",
            result_fingerprint=canonical_fingerprint(
                domain="data-result:object-profile:1.0.0",
                value=data,
            ),
            data=data,
        )


def _area_in_scope(area_code: str, allowed_area_codes: list[str]) -> bool:
    return any(
        area_code == allowed_area_code or area_code.startswith(allowed_area_code)
        for allowed_area_code in allowed_area_codes
    )


class HttpGovernanceAdapter:
    """Adapter boundary for the existing geo-qxst read APIs."""

    def __init__(
        self,
        *,
        base_url: str,
        credential_broker: "LegacyCredentialResolver",
        client: httpx.AsyncClient | None = None,
        housing_next_area_enabled: bool = True,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._credential_broker = credential_broker
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient()
        self._housing_next_area_enabled = housing_next_area_enabled

    @property
    def housing_next_area_enabled(self) -> bool:
        return self._housing_next_area_enabled

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def execute(
        self,
        *,
        manifest: InternalToolManifest,
        arguments: BaseModel,
        policy_decision: PolicyDecision,
        auth_context: AuthContext,
    ) -> DataResult:
        if manifest.tool_id == "governance.get_governance_overview" and isinstance(
            arguments, QueryGovernanceOverviewInput
        ):
            return await self._get_governance_overview_http(
                arguments=arguments,
                auth_context=auth_context,
                timeout_seconds=manifest.limits.timeout_ms / 1000,
                max_attempts=manifest.limits.max_attempts,
            )
        if (
            manifest.tool_id == "governance.query_governance_power_metrics"
            and isinstance(arguments, QueryGovernancePowerMetricsInput)
        ):
            return await self._query_governance_power_metrics_http(
                arguments=arguments,
                auth_context=auth_context,
                timeout_seconds=manifest.limits.timeout_ms / 1000,
                max_attempts=manifest.limits.max_attempts,
            )
        if manifest.tool_id == "governance.query_enterprise_metrics" and isinstance(
            arguments, QueryEnterpriseMetricsInput
        ):
            return await self._query_enterprise_metrics_http(
                arguments=arguments,
                auth_context=auth_context,
                timeout_seconds=manifest.limits.timeout_ms / 1000,
                max_attempts=manifest.limits.max_attempts,
            )
        if manifest.tool_id == "governance.resolve_area" and isinstance(
            arguments, ResolveAreaInput
        ):
            return await self._resolve_area_http(
                arguments=arguments,
                policy_decision=policy_decision,
                auth_context=auth_context,
                timeout_seconds=manifest.limits.timeout_ms / 1000,
                max_attempts=manifest.limits.max_attempts,
            )
        if manifest.tool_id == "governance.query_population_metrics" and isinstance(
            arguments, QueryPopulationMetricsInput
        ):
            return await self._query_population_metrics_http(
                arguments=arguments,
                auth_context=auth_context,
                timeout_seconds=manifest.limits.timeout_ms / 1000,
                max_attempts=manifest.limits.max_attempts,
            )
        if manifest.tool_id == "governance.query_housing_metrics" and isinstance(
            arguments, QueryHousingMetricsInput
        ):
            return await self._query_housing_metrics_http(
                arguments=arguments,
                auth_context=auth_context,
                timeout_seconds=manifest.limits.timeout_ms / 1000,
                max_attempts=manifest.limits.max_attempts,
            )
        if manifest.tool_id == "governance.query_event_metrics" and isinstance(
            arguments, QueryEventMetricsInput
        ):
            return await self._query_event_metrics_http(
                arguments=arguments,
                auth_context=auth_context,
                timeout_seconds=manifest.limits.timeout_ms / 1000,
                max_attempts=manifest.limits.max_attempts,
            )
        if manifest.tool_id == "governance.get_object_profile" and isinstance(
            arguments, GetObjectProfileInput
        ):
            return await self._get_object_profile_http(
                arguments=arguments,
                policy_decision=policy_decision,
                auth_context=auth_context,
                timeout_seconds=manifest.limits.timeout_ms / 1000,
                max_attempts=manifest.limits.max_attempts,
            )
        raise NotImplementedError(f"HTTP adapter not implemented for {manifest.tool_id}")

    async def _get_governance_overview_http(
        self,
        *,
        arguments: QueryGovernanceOverviewInput,
        auth_context: AuthContext,
        timeout_seconds: float,
        max_attempts: int,
    ) -> TableDataResult:
        _validate_governance_overview_query(arguments)
        area_code = arguments.query.scope.area_code
        token = await self._credential_broker.resolve(
            credential_ref=auth_context.credential_ref,
            subject_user_id=auth_context.principal.user_id,
            app_id=auth_context.application.app_id,
            run_id=auth_context.run_id,
        )
        response = await self._get(
            "/base/getBaseTotal",
            params={
                "areaName": _area_code_column(area_code),
                "areaCode": area_code,
                "dataBaseType": "2",
            },
            token=token,
            timeout_seconds=timeout_seconds,
            max_attempts=max_attempts,
        )
        raw = _unwrap_standard_result(response)
        if not isinstance(raw, dict):
            raise UpstreamContractError("legacy governance overview is not an object")
        fields: dict[
            Literal["person", "house", "enterprise", "event", "matter"],
            tuple[str, str, str],
        ] = {
            "person": ("人", "personRelationNum", "personTotal"),
            "house": ("房", "houseRelationNum", "houseTotal"),
            "enterprise": ("企", "enterpriseRelationNum", "enterpriseTotal"),
            "event": ("事", "eventRelationNum", "eventTotal"),
            "matter": ("物", "matterRelationNum", "matterTotal"),
        }
        required_fields = {
            field
            for _label, related, total in fields.values()
            for field in (related, total)
        }
        if not required_fields.issubset(raw):
            raise UpstreamContractError(
                "legacy governance overview is missing required counts"
            )
        try:
            counts = {
                field: _parse_non_negative_integer(raw[field])
                for field in required_fields
            }
            rows = [
                GovernanceOverviewRow(
                    subject=subject,
                    subject_label=label,
                    related_count=counts[related_field],
                    total_count=counts[total_field],
                    coverage_rate=(
                        round(
                            counts[related_field]
                            / counts[total_field]
                            * 100,
                            1,
                        )
                        if counts[total_field] > 0
                        else 0.0
                    ),
                )
                for subject, (label, related_field, total_field) in fields.items()
            ]
        except (TypeError, ValueError) as exc:
            raise UpstreamContractError(
                "legacy governance overview contains invalid counts"
            ) from exc
        data = GovernanceOverviewTable(rows=rows)
        return TableDataResult(
            result_id=new_id("res"),
            data_schema_ref="schema://data/governance-overview-table/1.0.0",
            result_fingerprint=canonical_fingerprint(
                domain="data-result:governance-overview-table:1.0.0",
                value=data,
            ),
            data=data,
            row_count=len(rows),
        )

    async def _query_governance_power_metrics_http(
        self,
        *,
        arguments: QueryGovernancePowerMetricsInput,
        auth_context: AuthContext,
        timeout_seconds: float,
        max_attempts: int,
    ) -> TableDataResult:
        area_code = arguments.query.scope.area_code
        if not re.fullmatch(r"(?:\d{4}|\d{6}|\d{9}|\d{12}|\d{15}|\d{17}|\d{21})", area_code):
            raise SemanticValidationError("invalid governance power area code")
        token = await self._credential_broker.resolve(
            credential_ref=auth_context.credential_ref,
            subject_user_id=auth_context.principal.user_id,
            app_id=auth_context.application.app_id,
            run_id=auth_context.run_id,
        )
        response = await self._get(
            "/api/getGovernancePower",
            params={
                "areaCodeName": _area_code_column(area_code),
                "areaCodeValue": area_code,
            },
            token=token,
            timeout_seconds=timeout_seconds,
            max_attempts=max_attempts,
        )
        raw = _unwrap_standard_result(response)
        if not isinstance(raw, list):
            raise UpstreamContractError("legacy governance power is not a list")
        labels = {
            "roomNum": "户数",
            "10": "网格长",
            "11": "网格指导员",
            "12": "专职网格员",
            "13": "兼职网格员",
            "nGridSum": "其他网格力量",
            "gridUnitSum": "微网格",
        }
        seen: set[str] = set()
        rows: list[GovernancePowerMetricRow] = []
        try:
            for item in raw:
                if not isinstance(item, dict):
                    raise ValueError("row")
                type_code = item.get("type")
                if not isinstance(type_code, str):
                    raise ValueError("type")
                if type_code in seen:
                    raise ValueError("duplicate")
                seen.add(type_code)
                if type_code == "999":
                    # The legacy service always emits this superseded "other"
                    # aggregate.  The product uses nGridSum instead, so validate
                    # but do not expose the duplicate metric.
                    _parse_non_negative_integer(item.get("count"))
                    continue
                if type_code not in labels:
                    raise ValueError("type")
                rows.append(
                    GovernancePowerMetricRow(
                        type_code=cast(
                            Literal[
                                "roomNum",
                                "10",
                                "11",
                                "12",
                                "13",
                                "nGridSum",
                                "gridUnitSum",
                            ],
                            type_code,
                        ),
                        type_name=labels[type_code],
                        count=_parse_non_negative_integer(item.get("count")),
                    )
                )
        except (TypeError, ValueError) as exc:
            raise UpstreamContractError(
                "legacy governance power contains unsafe aggregate rows"
            ) from exc
        data = GovernancePowerMetricTable(rows=rows)
        return TableDataResult(
            result_id=new_id("res"),
            data_schema_ref="schema://data/governance-power-metric-table/1.0.0",
            result_fingerprint=canonical_fingerprint(
                domain="data-result:governance-power-metric-table:1.0.0",
                value=data,
            ),
            data=data,
            row_count=len(rows),
        )

    async def _query_enterprise_metrics_http(
        self,
        *,
        arguments: QueryEnterpriseMetricsInput,
        auth_context: AuthContext,
        timeout_seconds: float,
        max_attempts: int,
    ) -> TableDataResult:
        if arguments.query.group_by == ["enterprise_scale"]:
            return await self._query_enterprise_scale_distribution_http(
                arguments=arguments,
                auth_context=auth_context,
                timeout_seconds=timeout_seconds,
                max_attempts=max_attempts,
            )
        if arguments.query.group_by == ["enterprise_type"]:
            return await self._query_enterprise_type_distribution_http(
                arguments=arguments,
                auth_context=auth_context,
                timeout_seconds=timeout_seconds,
                max_attempts=max_attempts,
            )
        if arguments.query.group_by == ["industry_name"]:
            return await self._query_enterprise_industry_distribution_http(
                arguments=arguments,
                auth_context=auth_context,
                timeout_seconds=timeout_seconds,
                max_attempts=max_attempts,
            )
        _validate_enterprise_next_area_query(arguments)
        area_code = arguments.query.scope.area_code
        token = await self._credential_broker.resolve(
            credential_ref=auth_context.credential_ref,
            subject_user_id=auth_context.principal.user_id,
            app_id=auth_context.application.app_id,
            run_id=auth_context.run_id,
        )
        response = await self._post(
            "/enterprise/getNextEnterprise",
            data={
                "areaCode": area_code,
                "areaName": _area_code_column(area_code),
            },
            token=token,
            timeout_seconds=timeout_seconds,
            max_attempts=max_attempts,
        )
        raw = _unwrap_standard_result(response)
        if not isinstance(raw, list):
            raise UpstreamContractError("legacy enterprise metrics is not a list")
        expected_length = _NEXT_AREA_CODE_LENGTH[len(area_code)]
        rows: list[EnterpriseMetricRow] = []
        for item in raw[: arguments.query.limit]:
            if not isinstance(item, dict) or not {
                "areaCode", "areaName", "total"
            }.issubset(item):
                raise UpstreamContractError(
                    "legacy enterprise metric row is missing required fields"
                )
            child_code = item["areaCode"]
            child_name = item["areaName"]
            try:
                count = _parse_non_negative_integer(item["total"])
            except (TypeError, ValueError) as exc:
                raise UpstreamContractError(
                    "legacy enterprise metric row contains invalid total"
                ) from exc
            if (
                not isinstance(child_code, str)
                or not child_code.isascii()
                or not child_code.isdigit()
                or len(child_code) != expected_length
                or not child_code.startswith(area_code)
                or not isinstance(child_name, str)
                or not child_name.strip()
                or count < 0
            ):
                raise UpstreamContractError(
                    "legacy enterprise metric row violates direct-child contract"
                )
            rows.append(
                EnterpriseMetricRow(
                    area_code=child_code,
                    area_name=child_name,
                    enterprise_count=count,
                )
            )
        data = EnterpriseMetricTable(rows=rows)
        return TableDataResult(
            result_id=new_id("res"),
            data_schema_ref="schema://data/enterprise-metric-table/1.0.0",
            result_fingerprint=canonical_fingerprint(
                domain="data-result:enterprise-metric-table:1.0.0",
                value=data,
            ),
            data=data,
            row_count=len(rows),
            truncated=len(raw) > arguments.query.limit,
        )

    async def _query_enterprise_type_distribution_http(
        self,
        *,
        arguments: QueryEnterpriseMetricsInput,
        auth_context: AuthContext,
        timeout_seconds: float,
        max_attempts: int,
    ) -> TableDataResult:
        area_code = arguments.query.scope.area_code
        _validate_area_code(
            area_code,
            supported_lengths={4, 6, 9, 12, 15},
            capability_name="enterprise type distribution",
        )
        token = await self._credential_broker.resolve(
            credential_ref=auth_context.credential_ref,
            subject_user_id=auth_context.principal.user_id,
            app_id=auth_context.application.app_id,
            run_id=auth_context.run_id,
        )
        response = await self._get(
            "/api/getEnterpriseTypeCount",
            params={
                "areaCodeName": _area_code_column(area_code),
                "areaCodeValue": area_code,
                "typeColumn": "enterprise_type",
            },
            token=token,
            timeout_seconds=timeout_seconds,
            max_attempts=max_attempts,
        )
        raw_rows = _unwrap_standard_result(response)
        if not isinstance(raw_rows, list) or len(raw_rows) > 8:
            raise UpstreamContractError(
                "legacy enterprise type response violates the top-eight list contract"
            )
        if not raw_rows:
            data = EnterpriseTypeDistributionTable(rows=[])
            return TableDataResult(
                result_id=new_id("res"),
                data_schema_ref=(
                    "schema://data/enterprise-type-distribution-table/1.0.0"
                ),
                result_fingerprint=canonical_fingerprint(
                    domain=(
                        "data-result:enterprise-type-distribution-table:1.0.0"
                    ),
                    value=data,
                ),
                data=data,
                row_count=0,
            )

        dictionary_response = await self._post(
            "/dict/getDictValue",
            data={},
            token=token,
            timeout_seconds=timeout_seconds,
            max_attempts=max_attempts,
        )
        labels = _unwrap_enterprise_type_dictionary(dictionary_response)
        source_rows: list[EnterpriseTypeDistributionRow] = []
        seen_codes: set[str] = set()
        try:
            for item in raw_rows:
                raw_code = _required_mapping_value(item, "enterprise_type")
                if not isinstance(raw_code, str) or not raw_code.strip():
                    raise ValueError("enterprise type code must be non-empty")
                code = raw_code.strip()
                if code in seen_codes:
                    raise ValueError("enterprise type code must be unique")
                seen_codes.add(code)
                source_rows.append(
                    EnterpriseTypeDistributionRow(
                        enterprise_type=labels[code],
                        enterprise_count=_parse_non_negative_integer(
                            _required_mapping_value(item, "count")
                        ),
                    )
                )
        except (KeyError, TypeError, ValueError) as exc:
            raise UpstreamContractError(
                "legacy enterprise type response is malformed"
            ) from exc
        rows = source_rows[: arguments.query.limit]
        data = EnterpriseTypeDistributionTable(rows=rows)
        return TableDataResult(
            result_id=new_id("res"),
            data_schema_ref=(
                "schema://data/enterprise-type-distribution-table/1.0.0"
            ),
            result_fingerprint=canonical_fingerprint(
                domain="data-result:enterprise-type-distribution-table:1.0.0",
                value=data,
            ),
            data=data,
            row_count=len(rows),
            truncated=len(source_rows) > arguments.query.limit,
        )

    async def _query_enterprise_industry_distribution_http(
        self,
        *,
        arguments: QueryEnterpriseMetricsInput,
        auth_context: AuthContext,
        timeout_seconds: float,
        max_attempts: int,
    ) -> TableDataResult:
        area_code = arguments.query.scope.area_code
        _validate_area_code(
            area_code,
            supported_lengths={4, 6, 9, 12, 15},
            capability_name="enterprise industry distribution",
        )
        token = await self._credential_broker.resolve(
            credential_ref=auth_context.credential_ref,
            subject_user_id=auth_context.principal.user_id,
            app_id=auth_context.application.app_id,
            run_id=auth_context.run_id,
        )
        response = await self._get(
            "/api/getEnterpriseTypeCount",
            params={
                "areaCodeName": _area_code_column(area_code),
                "areaCodeValue": area_code,
                "typeColumn": "industry_name",
            },
            token=token,
            timeout_seconds=timeout_seconds,
            max_attempts=max_attempts,
        )
        raw_rows = _unwrap_standard_result(response)
        if not isinstance(raw_rows, list) or len(raw_rows) > 8:
            raise UpstreamContractError(
                "legacy enterprise industry response violates the top-eight list contract"
            )
        source_rows: list[EnterpriseIndustryDistributionRow] = []
        seen_names: set[str] = set()
        try:
            for item in raw_rows:
                if not isinstance(item, Mapping):
                    raise ValueError("industry row is not an object")
                raw_name = _required_mapping_value(item, "industry_name")
                if not isinstance(raw_name, str) or not raw_name.strip():
                    raise ValueError("industry name must be a non-empty string")
                name = raw_name.strip()
                if name in seen_names:
                    raise ValueError("industry name must be unique")
                seen_names.add(name)
                source_rows.append(
                    EnterpriseIndustryDistributionRow(
                        industry_name=name,
                        enterprise_count=_parse_non_negative_integer(
                            _required_mapping_value(item, "count")
                        ),
                    )
                )
        except (KeyError, TypeError, ValueError) as exc:
            raise UpstreamContractError(
                "legacy enterprise industry response is malformed"
            ) from exc
        rows = source_rows[: arguments.query.limit]
        data = EnterpriseIndustryDistributionTable(rows=rows)
        return TableDataResult(
            result_id=new_id("res"),
            data_schema_ref="schema://data/enterprise-industry-distribution-table/1.0.0",
            result_fingerprint=canonical_fingerprint(
                domain="data-result:enterprise-industry-distribution-table:1.0.0",
                value=data,
            ),
            data=data,
            row_count=len(rows),
            truncated=len(source_rows) > arguments.query.limit,
        )

    async def _query_enterprise_scale_distribution_http(
        self,
        *,
        arguments: QueryEnterpriseMetricsInput,
        auth_context: AuthContext,
        timeout_seconds: float,
        max_attempts: int,
    ) -> TableDataResult:
        area_code = arguments.query.scope.area_code
        _validate_area_code(
            area_code,
            supported_lengths={4, 6, 9, 12, 15},
            capability_name="enterprise scale distribution",
        )
        token = await self._credential_broker.resolve(
            credential_ref=auth_context.credential_ref,
            subject_user_id=auth_context.principal.user_id,
            app_id=auth_context.application.app_id,
            run_id=auth_context.run_id,
        )
        response = await self._get(
            "/api/getEnterpriseScale",
            params={
                "areaCodeName": _area_code_column(area_code),
                "areaCodeValue": area_code,
            },
            token=token,
            timeout_seconds=timeout_seconds,
            max_attempts=max_attempts,
        )
        raw = _unwrap_standard_result(response)
        if not isinstance(raw, list) or len(raw) != 1:
            raise UpstreamContractError(
                "legacy enterprise scale response must contain exactly one row"
            )
        source = raw[0]
        source_labels = (
            ("5人以下", "5人以下"),
            ("5-10人", "5-10人"),
            ("10-50人", "11-50人"),
            ("50-100人", "51-100人"),
            ("100人以上", "100人以上"),
        )
        if not isinstance(source, dict) or set(source) != {
            source_key for source_key, _ in source_labels
        }:
            raise UpstreamContractError(
                "legacy enterprise scale row violates the exact five-bucket contract"
            )
        try:
            rows = [
                EnterpriseScaleDistributionRow(
                    enterprise_scale=display_label,
                    enterprise_count=_parse_non_negative_integer(source[source_key]),
                )
                for source_key, display_label in source_labels
            ]
        except (TypeError, ValueError) as exc:
            raise UpstreamContractError(
                "legacy enterprise scale row contains an invalid count"
            ) from exc
        data = EnterpriseScaleDistributionTable(rows=rows)
        return TableDataResult(
            result_id=new_id("res"),
            data_schema_ref=(
                "schema://data/enterprise-scale-distribution-table/1.0.0"
            ),
            result_fingerprint=canonical_fingerprint(
                domain="data-result:enterprise-scale-distribution-table:1.0.0",
                value=data,
            ),
            data=data,
            row_count=len(rows),
        )

    async def _resolve_area_http(
        self,
        *,
        arguments: ResolveAreaInput,
        policy_decision: PolicyDecision,
        auth_context: AuthContext,
        timeout_seconds: float,
        max_attempts: int,
    ) -> AreaCandidatesResult:
        token = await self._credential_broker.resolve(
            credential_ref=auth_context.credential_ref,
            subject_user_id=auth_context.principal.user_id,
            app_id=auth_context.application.app_id,
            run_id=auth_context.run_id,
        )
        response = await self._post(
            "/area/getAreaInfoByAreaName",
            data={"areaName": arguments.query},
            token=token,
            timeout_seconds=timeout_seconds,
            max_attempts=max_attempts,
        )
        area = _unwrap_standard_result(response)
        if area is None or area == []:
            data = AreaCandidatesData(
                resolved_area_code=None,
                ambiguous=False,
                candidates=[],
            )
            return AreaCandidatesResult(
                result_id=new_id("res"),
                data_schema_ref="schema://data/area-candidates/1.0.0",
                result_fingerprint=canonical_fingerprint(
                    domain="data-result:area-candidates:1.0.0",
                    value=data,
                ),
                data=data,
                candidate_count=0,
            )
        try:
            area_code = str(area["areacode"])
            area_name = str(area["areaname"])
        except (KeyError, TypeError) as exc:
            raise UpstreamContractError("legacy area response is malformed") from exc
        candidates = []
        if _area_in_scope(area_code, policy_decision.effective_scope.area_codes):
            candidates.append(
                AreaCandidate(
                    area_code=area_code,
                    area_name=area_name,
                    level=_area_level(area_code),
                )
            )
        data = AreaCandidatesData(
            resolved_area_code=area_code if len(candidates) == 1 else None,
            ambiguous=False,
            candidates=candidates,
        )
        return AreaCandidatesResult(
            result_id=new_id("res"),
            data_schema_ref="schema://data/area-candidates/1.0.0",
            result_fingerprint=canonical_fingerprint(
                domain="data-result:area-candidates:1.0.0",
                value=data,
            ),
            data=data,
            candidate_count=len(candidates),
        )

    async def _query_population_metrics_http(
        self,
        *,
        arguments: QueryPopulationMetricsInput,
        auth_context: AuthContext,
        timeout_seconds: float,
        max_attempts: int,
    ) -> TableDataResult:
        area_code = arguments.query.scope.area_code
        population_kind = _validate_population_query(arguments)
        token = await self._credential_broker.resolve(
            credential_ref=auth_context.credential_ref,
            subject_user_id=auth_context.principal.user_id,
            app_id=auth_context.application.app_id,
            run_id=auth_context.run_id,
        )
        endpoint = (
            "/getNextSiteData"
            if population_kind == "solitary_elderly"
            else "/area/getNextPersonByType"
        )
        request_data = {
            "areaName": _area_code_column(area_code),
            "areaCode": area_code,
        }
        if population_kind == "solitary_elderly":
            request_data["tableName"] = "dm_empty_nest_old"
        if arguments.query.group_by in (
            ["descendant_street"],
            ["descendant_community"],
        ):
            return await self._query_population_descendants_http(
                arguments=arguments,
                token=token,
                endpoint=endpoint,
                population_kind=population_kind,
                timeout_seconds=timeout_seconds,
                max_attempts=max_attempts,
            )
        response = await self._post(
            endpoint,
            data=request_data,
            token=token,
            timeout_seconds=timeout_seconds,
            max_attempts=max_attempts,
        )
        raw_rows, upstream_truncated = _unwrap_standard_result_page(response)
        try:
            parsed_rows = [
                (
                    str(item["areaCode"]),
                    str(item["areaName"]),
                    _parse_non_negative_integer(item["total"]),
                )
                for item in raw_rows
            ]
        except (KeyError, TypeError, ValueError) as exc:
            raise UpstreamContractError(
                "legacy population response is malformed"
            ) from exc
        return _population_result(
            arguments=arguments,
            parsed_rows=parsed_rows,
            upstream_truncated=upstream_truncated,
            ranking=arguments.query.group_by == ["district"],
        )

    async def _query_population_descendants_http(
        self,
        *,
        arguments: QueryPopulationMetricsInput,
        token: SecretStr,
        endpoint: str,
        population_kind: str,
        timeout_seconds: float,
        max_attempts: int,
    ) -> TableDataResult:
        """Build a complete city ranking through a bounded direct-child workflow."""

        city_code = arguments.query.scope.area_code
        target_length = (
            9 if arguments.query.group_by == ["descendant_street"] else 12
        )
        max_nodes = {6: 20, 9: 400, 12: 5000}

        async def fetch_children(parent_code: str) -> list[Mapping[str, object]]:
            request_data = {
                "areaName": _area_code_column(parent_code),
                "areaCode": parent_code,
            }
            if population_kind == "solitary_elderly":
                request_data["tableName"] = "dm_empty_nest_old"
            response = await self._post(
                endpoint,
                data=request_data,
                token=token,
                timeout_seconds=timeout_seconds,
                max_attempts=max_attempts,
            )
            raw = _unwrap_standard_result(response)
            if not isinstance(raw, list):
                raise UpstreamContractError(
                    "legacy population descendant response data is not a list"
                )
            if len(raw) > max_nodes[len(parent_code) + (2 if len(parent_code) == 4 else 3)]:
                raise UpstreamContractError(
                    "legacy population descendant fan-out exceeds the bound"
                )
            if not all(isinstance(item, Mapping) for item in raw):
                raise UpstreamContractError(
                    "legacy population descendant row is malformed"
                )
            return list(raw)

        frontier = [city_code]
        source_rows: list[PopulationRankingRow] = []
        seen: set[str] = set()
        while frontier:
            children_by_parent = await asyncio.gather(
                *(fetch_children(parent) for parent in frontier)
            )
            next_frontier: list[str] = []
            for parent, children in zip(frontier, children_by_parent, strict=True):
                expected_length = len(parent) + (2 if len(parent) == 4 else 3)
                for item in children:
                    child_code = item.get("areaCode")
                    if (
                        not isinstance(child_code, str)
                        or len(child_code) != expected_length
                        or not child_code.isascii()
                        or not child_code.isdigit()
                        or not child_code.startswith(parent)
                        or child_code in seen
                    ):
                        raise UpstreamContractError(
                            "legacy population descendant code is malformed"
                        )
                    seen.add(child_code)
                    if len(child_code) == target_length:
                        try:
                            source_rows.append(
                                PopulationRankingRow(
                                    rank=1,
                                    area_code=child_code,
                                    area_name=str(item["areaName"]),
                                    person_count=_parse_non_negative_integer(item["total"]),
                                )
                            )
                        except (KeyError, TypeError, ValueError) as exc:
                            raise UpstreamContractError(
                                "legacy population descendant row is malformed"
                            ) from exc
                    elif len(child_code) < target_length:
                        next_frontier.append(child_code)
            if len(next_frontier) > max_nodes.get(expected_length, 0):
                raise UpstreamContractError(
                    "legacy population descendant fan-out exceeds the bound"
                )
            frontier = next_frontier

        return _population_result(
            arguments=arguments,
            parsed_rows=[
                (row.area_code, row.area_name, row.person_count)
                for row in source_rows
            ],
            upstream_truncated=False,
            ranking=True,
        )

    async def _query_event_metrics_http(
        self,
        *,
        arguments: QueryEventMetricsInput,
        auth_context: AuthContext,
        timeout_seconds: float,
        max_attempts: int,
    ) -> TableDataResult:
        area_code = arguments.query.scope.area_code
        if arguments.query.group_by == ["event_category"]:
            return await self._query_event_category_http(
                arguments=arguments,
                auth_context=auth_context,
                timeout_seconds=timeout_seconds,
                max_attempts=max_attempts,
            )
        if arguments.query.metrics == ["event_count"]:
            return await self._query_event_trend_http(
                arguments=arguments,
                auth_context=auth_context,
                timeout_seconds=timeout_seconds,
                max_attempts=max_attempts,
            )
        token = await self._credential_broker.resolve(
            credential_ref=auth_context.credential_ref,
            subject_user_id=auth_context.principal.user_id,
            app_id=auth_context.application.app_id,
            run_id=auth_context.run_id,
        )
        response = await self._get(
            "/api/getEventPropertiesAndConflictsByTotal",
            params={
                "areaCodeName": _area_code_column(area_code),
                "areaCodeValue": area_code,
            },
            token=token,
            timeout_seconds=timeout_seconds,
            max_attempts=max_attempts,
        )
        raw = _unwrap_standard_result(response)
        if not isinstance(raw, dict):
            raise UpstreamContractError(
                "legacy event response is not an object"
            )
        required_rate_fields = {
            "gridFinishRate",
            "communityFinishRate",
            "streetFinishRate",
        }
        if not required_rate_fields.issubset(raw):
            raise UpstreamContractError(
                "legacy event response is missing required finish rates"
            )
        grid_rate = raw["gridFinishRate"]
        community_rate = raw["communityFinishRate"]
        street_rate = raw["streetFinishRate"]
        rows = [
            EventFinishRateRow(level="grid", finish_rate=_parse_percent(grid_rate)),
            EventFinishRateRow(
                level="community",
                finish_rate=_parse_percent(community_rate),
            ),
            EventFinishRateRow(level="street", finish_rate=_parse_percent(street_rate)),
        ]
        data = EventFinishRateTable(rows=rows)
        return TableDataResult(
            result_id=new_id("res"),
            data_schema_ref="schema://data/event-finish-rate-table/1.0.0",
            result_fingerprint=canonical_fingerprint(
                domain="data-result:event-metric-table:1.0.0",
                value=data,
            ),
            data=data,
            row_count=len(rows),
        )

    async def _query_event_trend_http(
        self,
        *,
        arguments: QueryEventMetricsInput,
        auth_context: AuthContext,
        timeout_seconds: float,
        max_attempts: int,
    ) -> TableDataResult:
        time_range = arguments.query.time_range
        if time_range is None:
            raise SemanticValidationError(
                "event trend requires an explicit time range"
            )
        area_code = arguments.query.scope.area_code
        _validate_area_code(
            area_code,
            supported_lengths={4, 6, 9, 12, 15},
            capability_name="event trend",
        )
        token = await self._credential_broker.resolve(
            credential_ref=auth_context.credential_ref,
            subject_user_id=auth_context.principal.user_id,
            app_id=auth_context.application.app_id,
            run_id=auth_context.run_id,
        )
        response = await self._post(
            "/event/getEventCountByMonth",
            data={
                "areaName": _area_code_column(area_code),
                "areaCode": area_code,
                "startDate": time_range.start.isoformat(),
                "endDate": time_range.end.isoformat(),
            },
            token=token,
            timeout_seconds=timeout_seconds,
            max_attempts=max_attempts,
        )
        raw = _unwrap_standard_result(response)
        if not isinstance(raw, list):
            raise UpstreamContractError(
                "legacy event trend response data is not a list"
            )
        requested_months = _calendar_months(time_range.start, time_range.end)
        requested_set = set(requested_months)
        counts: dict[str, int] = {}
        for item in raw:
            if not isinstance(item, Mapping):
                raise UpstreamContractError(
                    "legacy event trend row is not an object"
                )
            month = item.get("month")
            if (
                not isinstance(month, str)
                or _YEAR_MONTH.fullmatch(month) is None
                or month not in requested_set
                or month in counts
            ):
                raise UpstreamContractError(
                    "legacy event trend month is invalid, duplicated, or out of range"
                )
            try:
                counts[month] = _parse_non_negative_integer(item.get("total"))
            except ValueError as exc:
                raise UpstreamContractError(
                    "legacy event trend count is malformed"
                ) from exc
        rows = [
            EventTrendRow(month=month, event_count=counts.get(month, 0))
            for month in requested_months
        ]
        data = EventTrendTable(rows=rows)
        return TableDataResult(
            result_id=new_id("res"),
            data_schema_ref="schema://data/event-trend-table/1.0.0",
            result_fingerprint=canonical_fingerprint(
                domain="data-result:event-trend-table:1.0.0",
                value=data,
            ),
            data=data,
            row_count=len(rows),
        )

    async def _query_event_category_http(
        self,
        *,
        arguments: QueryEventMetricsInput,
        auth_context: AuthContext,
        timeout_seconds: float,
        max_attempts: int,
    ) -> TableDataResult:
        area_code = arguments.query.scope.area_code
        _validate_area_code(
            area_code,
            supported_lengths={4, 6, 9, 12, 15},
            capability_name="event category distribution",
        )
        token = await self._credential_broker.resolve(
            credential_ref=auth_context.credential_ref,
            subject_user_id=auth_context.principal.user_id,
            app_id=auth_context.application.app_id,
            run_id=auth_context.run_id,
        )
        response = await self._get(
            "/api/getEventProperties",
            params={
                "areaCodeName": _area_code_column(area_code),
                "areaCodeValue": area_code,
                "eventType": "eventtype_code1",
            },
            token=token,
            timeout_seconds=timeout_seconds,
            max_attempts=max_attempts,
        )
        raw_rows = _unwrap_standard_result(response)
        if not isinstance(raw_rows, list) or len(raw_rows) > 100:
            raise UpstreamContractError(
                "legacy event category response violates the list contract"
            )
        if not raw_rows:
            data = EventCategoryTable(rows=[])
            return TableDataResult(
                result_id=new_id("res"),
                data_schema_ref="schema://data/event-category-table/1.0.0",
                result_fingerprint=canonical_fingerprint(
                    domain="data-result:event-category-table:1.0.0", value=data
                ),
                data=data,
                row_count=0,
            )
        dictionary_response = await self._post(
            "/dict/getDictValue",
            data={},
            token=token,
            timeout_seconds=timeout_seconds,
            max_attempts=max_attempts,
        )
        labels = _unwrap_event_category_dictionary(dictionary_response)
        rows: list[EventCategoryRow] = []
        seen_codes: set[str] = set()
        try:
            for item in raw_rows:
                raw_code = _required_mapping_value(item, "key")
                if not isinstance(raw_code, str) or not raw_code.strip():
                    raise ValueError("event category code must be non-empty")
                code = raw_code.strip()
                if code in seen_codes:
                    raise ValueError("event category code must be unique")
                seen_codes.add(code)
                rows.append(
                    EventCategoryRow(
                        category_code=code,
                        category_name=labels[code],
                        event_count=_parse_non_negative_integer(
                            _required_mapping_value(item, "doc_count")
                        ),
                    )
                )
        except (KeyError, TypeError, ValueError) as exc:
            raise UpstreamContractError(
                "legacy event category response is malformed"
            ) from exc
        rows = rows[: arguments.query.limit]
        data = EventCategoryTable(rows=rows)
        return TableDataResult(
            result_id=new_id("res"),
            data_schema_ref="schema://data/event-category-table/1.0.0",
            result_fingerprint=canonical_fingerprint(
                domain="data-result:event-category-table:1.0.0", value=data
            ),
            data=data,
            row_count=len(rows),
            truncated=len(raw_rows) > arguments.query.limit,
        )

    async def _query_housing_metrics_http(
        self,
        *,
        arguments: QueryHousingMetricsInput,
        auth_context: AuthContext,
        timeout_seconds: float,
        max_attempts: int,
    ) -> TableDataResult:
        if arguments.query.metrics == ["building_count", "room_count"]:
            return await self._query_housing_stock_overview_http(
                arguments=arguments,
                auth_context=auth_context,
                timeout_seconds=timeout_seconds,
                max_attempts=max_attempts,
            )
        if arguments.query.group_by in (["next_area"], ["descendant_street"]):
            if not self._housing_next_area_enabled:
                raise SemanticValidationError(
                    "housing area aggregation is disabled in this deployment"
                )
            return await self._query_housing_area_distribution_http(
                arguments=arguments,
                auth_context=auth_context,
                timeout_seconds=timeout_seconds,
                max_attempts=max_attempts,
            )
        if arguments.query.group_by == ["room_use"]:
            return await self._query_housing_room_use_http(
                arguments=arguments,
                auth_context=auth_context,
                timeout_seconds=timeout_seconds,
                max_attempts=max_attempts,
            )
        area_code = arguments.query.scope.area_code
        token = await self._credential_broker.resolve(
            credential_ref=auth_context.credential_ref,
            subject_user_id=auth_context.principal.user_id,
            app_id=auth_context.application.app_id,
            run_id=auth_context.run_id,
        )
        response = await self._post(
            "/house/getRoomLeaseType",
            data={
                "areaName": _area_code_column(area_code),
                "areaCode": area_code,
            },
            token=token,
            timeout_seconds=timeout_seconds,
            max_attempts=max_attempts,
        )
        raw_rows, upstream_truncated = _unwrap_standard_result_page(response)
        if not isinstance(raw_rows, list):
            raise UpstreamContractError(
                "legacy housing response data is not a list"
            )
        try:
            rows = [
                HousingLeaseTypeRow(
                    lease_type=str(item["house_type"]),
                    dwelling_count=int(item["total"]),
                )
                for item in raw_rows
            ][: arguments.query.limit]
        except (KeyError, TypeError, ValueError) as exc:
            raise UpstreamContractError(
                "legacy housing response is malformed"
            ) from exc
        data = HousingLeaseTypeTable(rows=rows)
        return TableDataResult(
            result_id=new_id("res"),
            data_schema_ref="schema://data/housing-lease-type-table/1.0.0",
            result_fingerprint=canonical_fingerprint(
                domain="data-result:housing-metric-table:1.0.0",
                value=data,
            ),
            data=data,
            row_count=len(rows),
            truncated=(
                upstream_truncated or len(raw_rows) > arguments.query.limit
            ),
        )

    async def _query_housing_stock_overview_http(
        self,
        *,
        arguments: QueryHousingMetricsInput,
        auth_context: AuthContext,
        timeout_seconds: float,
        max_attempts: int,
    ) -> TableDataResult:
        area_code = arguments.query.scope.area_code
        _validate_area_code(
            area_code,
            supported_lengths={4, 6, 9, 12, 15},
            capability_name="housing stock overview",
        )
        token = await self._credential_broker.resolve(
            credential_ref=auth_context.credential_ref,
            subject_user_id=auth_context.principal.user_id,
            app_id=auth_context.application.app_id,
            run_id=auth_context.run_id,
        )
        response = await self._get(
            "/api/getBuildingAndRoomTotal",
            params={
                "areaCodeName": _area_code_column(area_code),
                "areaCodeValue": area_code,
            },
            token=token,
            timeout_seconds=timeout_seconds,
            max_attempts=max_attempts,
        )
        raw = _unwrap_standard_result(response)
        if not isinstance(raw, Mapping):
            raise UpstreamContractError(
                "legacy housing stock response data is not an object"
            )
        try:
            row = HousingStockOverviewRow(
                building_count=_parse_non_negative_integer(
                    _required_mapping_value(raw, "buildingTotal")
                ),
                room_count=_parse_non_negative_integer(
                    _required_mapping_value(raw, "roomTotal")
                ),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise UpstreamContractError(
                "legacy housing stock response is malformed"
            ) from exc
        data = HousingStockOverviewTable(rows=[row])
        return TableDataResult(
            result_id=new_id("res"),
            data_schema_ref="schema://data/housing-stock-overview/1.0.0",
            result_fingerprint=canonical_fingerprint(
                domain="data-result:housing-stock-overview:1.0.0",
                value=data,
            ),
            data=data,
            row_count=1,
        )

    async def _query_housing_room_use_http(
        self,
        *,
        arguments: QueryHousingMetricsInput,
        auth_context: AuthContext,
        timeout_seconds: float,
        max_attempts: int,
    ) -> TableDataResult:
        area_code = arguments.query.scope.area_code
        _validate_area_code(
            area_code,
            supported_lengths={4, 6, 9, 12, 15},
            capability_name="housing room-use",
        )
        token = await self._credential_broker.resolve(
            credential_ref=auth_context.credential_ref,
            subject_user_id=auth_context.principal.user_id,
            app_id=auth_context.application.app_id,
            run_id=auth_context.run_id,
        )
        room_use_response = await self._post_query(
            "/room/getRoomUseAndAlone",
            params={
                "areaName": _area_code_column(area_code),
                "areaCode": area_code,
            },
            token=token,
            timeout_seconds=timeout_seconds,
            max_attempts=max_attempts,
        )
        raw_rows = _unwrap_standard_result(room_use_response)
        if not isinstance(raw_rows, list):
            raise UpstreamContractError(
                "legacy housing room-use response data is not a list"
            )
        if not raw_rows:
            data = HousingRoomUseTable(rows=[])
            return TableDataResult(
                result_id=new_id("res"),
                data_schema_ref="schema://data/housing-room-use-table/1.0.0",
                result_fingerprint=canonical_fingerprint(
                    domain="data-result:housing-room-use-table:1.0.0",
                    value=data,
                ),
                data=data,
                row_count=0,
            )

        dictionary_response = await self._post(
            "/dict/getDictValue",
            # qxst-sj initDictUtil posts an empty form and selects room_user
            # from the returned dictionary collection.
            data={},
            token=token,
            timeout_seconds=timeout_seconds,
            max_attempts=max_attempts,
        )
        room_use_labels = _unwrap_room_use_dictionary(dictionary_response)
        try:
            source_rows = [
                HousingRoomUseRow(
                    room_use=room_use_labels[_parse_room_use_code(item)],
                    dwelling_count=_parse_non_negative_integer(
                        _required_mapping_value(item, "doc_count")
                    ),
                )
                for item in raw_rows
            ]
        except (KeyError, TypeError, ValueError) as exc:
            raise UpstreamContractError(
                "legacy housing room-use response is malformed"
            ) from exc
        rows = source_rows[: arguments.query.limit]
        data = HousingRoomUseTable(rows=rows)
        return TableDataResult(
            result_id=new_id("res"),
            data_schema_ref="schema://data/housing-room-use-table/1.0.0",
            result_fingerprint=canonical_fingerprint(
                domain="data-result:housing-room-use-table:1.0.0",
                value=data,
            ),
            data=data,
            row_count=len(rows),
            truncated=len(source_rows) > arguments.query.limit,
        )

    async def _query_housing_area_distribution_http(
        self,
        *,
        arguments: QueryHousingMetricsInput,
        auth_context: AuthContext,
        timeout_seconds: float,
        max_attempts: int,
    ) -> TableDataResult:
        if arguments.query.group_by == ["descendant_street"]:
            return await self._query_housing_descendant_streets_http(
                arguments=arguments,
                auth_context=auth_context,
                timeout_seconds=timeout_seconds,
                max_attempts=max_attempts,
            )
        _validate_housing_next_area_query(arguments)
        area_code = arguments.query.scope.area_code
        token = await self._credential_broker.resolve(
            credential_ref=auth_context.credential_ref,
            subject_user_id=auth_context.principal.user_id,
            app_id=auth_context.application.app_id,
            run_id=auth_context.run_id,
        )
        response = await self._post(
            "/getNextSiteData",
            data={
                "areaName": _area_code_column(area_code),
                "areaCode": area_code,
                "tableName": "base_room_lease",
            },
            token=token,
            timeout_seconds=timeout_seconds,
            max_attempts=max_attempts,
        )
        raw_rows = _unwrap_standard_result(response)
        if not isinstance(raw_rows, list):
            raise UpstreamContractError(
                "legacy housing area response data is not a list"
            )
        source_rows = raw_rows
        try:
            rows = sorted(
                [
                HousingAreaGroupRow(
                    area_code=str(item["areaCode"]),
                    area_name=str(item["areaName"]),
                    dwelling_count=int(item["total"]),
                )
                for item in source_rows
                ],
                key=lambda row: (-row.dwelling_count, row.area_code),
            )[: arguments.query.limit]
        except (KeyError, TypeError, ValueError) as exc:
            raise UpstreamContractError(
                "legacy housing area response is malformed"
            ) from exc
        data = HousingAreaGroupTable(rows=rows)
        return TableDataResult(
            result_id=new_id("res"),
            data_schema_ref="schema://data/housing-area-group-table/1.0.0",
            result_fingerprint=canonical_fingerprint(
                domain="data-result:housing-area-group-table:1.0.0",
                value=data,
            ),
            data=data,
            row_count=len(rows),
            truncated=len(source_rows) > arguments.query.limit,
        )

    async def _query_housing_descendant_streets_http(
        self,
        *,
        arguments: QueryHousingMetricsInput,
        auth_context: AuthContext,
        timeout_seconds: float,
        max_attempts: int,
    ) -> TableDataResult:
        """Return a complete city-wide street ranking in one bounded Tool call.

        The legacy endpoint only exposes direct children.  A global city claim
        therefore requires one city request followed by one request per
        district.  The fan-out is bounded and fail-closed: partial district
        coverage can never be presented as the city-wide maximum.
        """

        _validate_housing_descendant_street_query(arguments)
        city_code = arguments.query.scope.area_code
        token = await self._credential_broker.resolve(
            credential_ref=auth_context.credential_ref,
            subject_user_id=auth_context.principal.user_id,
            app_id=auth_context.application.app_id,
            run_id=auth_context.run_id,
        )

        async def fetch_children(area_code: str) -> list[object]:
            response = await self._post(
                "/getNextSiteData",
                data={
                    "areaName": _area_code_column(area_code),
                    "areaCode": area_code,
                    "tableName": "base_room_lease",
                },
                token=token,
                timeout_seconds=timeout_seconds,
                max_attempts=max_attempts,
            )
            raw = _unwrap_standard_result(response)
            if not isinstance(raw, list):
                raise UpstreamContractError(
                    "legacy housing descendant response data is not a list"
                )
            return raw

        district_rows = await fetch_children(city_code)
        if not district_rows or len(district_rows) > 20:
            raise UpstreamContractError(
                "legacy housing district fan-out is empty or exceeds the bound"
            )
        district_codes: list[str] = []
        for item in district_rows:
            if not isinstance(item, Mapping):
                raise UpstreamContractError("legacy housing district row is malformed")
            district_code = item.get("areaCode")
            if (
                not isinstance(district_code, str)
                or len(district_code) != 6
                or not district_code.isascii()
                or not district_code.isdigit()
                or not district_code.startswith(city_code)
                or district_code in district_codes
            ):
                raise UpstreamContractError("legacy housing district code is malformed")
            district_codes.append(district_code)

        district_children = await asyncio.gather(
            *(fetch_children(code) for code in district_codes)
        )
        source_rows: list[HousingAreaGroupRow] = []
        seen_codes: set[str] = set()
        try:
            for district_code, child_rows in zip(
                district_codes, district_children, strict=True
            ):
                for item in child_rows:
                    if not isinstance(item, Mapping):
                        raise UpstreamContractError(
                            "legacy housing street row is malformed"
                        )
                    street_code = item.get("areaCode")
                    if (
                        not isinstance(street_code, str)
                        or len(street_code) != 9
                        or not street_code.isascii()
                        or not street_code.isdigit()
                        or not street_code.startswith(district_code)
                        or street_code in seen_codes
                    ):
                        raise UpstreamContractError(
                            "legacy housing street code is malformed"
                        )
                    seen_codes.add(street_code)
                    source_rows.append(
                        HousingAreaGroupRow(
                            area_code=street_code,
                            area_name=str(item["areaName"]),
                            dwelling_count=_parse_non_negative_integer(item["total"]),
                        )
                    )
        except (KeyError, TypeError, ValueError) as exc:
            raise UpstreamContractError(
                "legacy housing street row is malformed"
            ) from exc

        ordered = sorted(
            source_rows,
            key=lambda row: (-row.dwelling_count, row.area_code),
        )
        rows = ordered[: arguments.query.limit]
        data = HousingAreaGroupTable(rows=rows)
        return TableDataResult(
            result_id=new_id("res"),
            data_schema_ref="schema://data/housing-area-group-table/1.0.0",
            result_fingerprint=canonical_fingerprint(
                domain="data-result:housing-area-group-table:1.0.0",
                value=data,
            ),
            data=data,
            row_count=len(rows),
            truncated=len(ordered) > arguments.query.limit,
        )

    async def _get_object_profile_http(
        self,
        *,
        arguments: GetObjectProfileInput,
        policy_decision: PolicyDecision,
        auth_context: AuthContext,
        timeout_seconds: float,
        max_attempts: int,
    ) -> ObjectProfileResult:
        object_ref = arguments.object_ref
        if object_ref.object_type != "building":
            raise SemanticValidationError(
                "legacy object profile adapter currently supports buildings only"
            )
        token = await self._credential_broker.resolve(
            credential_ref=auth_context.credential_ref,
            subject_user_id=auth_context.principal.user_id,
            app_id=auth_context.application.app_id,
            run_id=auth_context.run_id,
        )
        field_sets = set(policy_decision.effective_scope.allowed_field_sets)
        fields: list[ObjectProfileField] = []

        response = await self._post(
            "/house/getHouseDetails",
            data={"houseId": object_ref.object_id},
            token=token,
            timeout_seconds=timeout_seconds,
            max_attempts=max_attempts,
        )
        raw_rows = _unwrap_standard_result(response)
        if not isinstance(raw_rows, list) or not raw_rows:
            raise UpstreamContractError("legacy building profile response is malformed")
        first_row = raw_rows[0]
        if not isinstance(first_row, dict) or not isinstance(
            first_row.get("_source"), dict
        ):
            raise UpstreamContractError("legacy building profile response is malformed")
        raw = first_row["_source"]
        returned_object_code = str(raw.get("code", raw.get("building_code", "")))
        if not returned_object_code or not area_is_within_scope(
            returned_object_code,
            arguments.scope,
        ):
            raise UpstreamContractError(
                "legacy building profile is outside the declared area scope"
            )
        building_title = str(
            raw.get(
                "building_path",
                raw.get("buildingname", raw.get("building_name", object_ref.object_id)),
            )
        )
        if "summary" in field_sets:
            fields.extend(
                [
                    ObjectProfileField(
                        field_id="building_name",
                        label="楼栋名称",
                        value=building_title,
                        classification="internal",
                    ),
                    ObjectProfileField(
                        field_id="object_code",
                        label="统一地址编码",
                        value=returned_object_code,
                        classification="internal",
                    ),
                    ObjectProfileField(
                        field_id="area_code",
                        label="区划编码",
                        value=arguments.scope.area_code,
                        classification="internal",
                    ),
                ]
            )
        if "location" in field_sets:
            fields.extend(
                [
                    ObjectProfileField(
                        field_id="longitude",
                        label="经度",
                        value=raw.get("longitude", raw.get("lon")),
                        classification="internal",
                    ),
                    ObjectProfileField(
                        field_id="latitude",
                        label="纬度",
                        value=raw.get("latitude", raw.get("lat")),
                        classification="internal",
                    ),
                ]
            )

        data = ObjectProfileData(
            object_ref=object_ref,
            area_code=arguments.scope.area_code,
            title=building_title,
            fields=fields,
        )
        return ObjectProfileResult(
            result_id=new_id("res"),
            data_schema_ref="schema://data/object-profile/1.0.0",
            result_fingerprint=canonical_fingerprint(
                domain="data-result:object-profile:1.0.0",
                value=data,
            ),
            data=data,
        )

    async def _get(
        self,
        path: str,
        *,
        params: dict[str, str],
        token: SecretStr,
        timeout_seconds: float,
        max_attempts: int,
    ) -> httpx.Response:
        attempts = max(1, max_attempts)
        for attempt in range(attempts):
            try:
                response = await self._client.get(
                    f"{self._base_url}{path}",
                    params=params,
                    headers={"geoToken": token.get_secret_value()},
                    timeout=timeout_seconds,
                )
                if _is_token_failure_response(response):
                    raise ReauthenticationRequired("登录凭据已失效，请重新认证")
                response.raise_for_status()
                return response
            except ReauthenticationRequired:
                raise
            except httpx.TimeoutException as exc:
                if attempt + 1 == attempts:
                    raise UpstreamTimeout(
                        "legacy geo-qxst request timed out"
                    ) from exc
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code < 500 or attempt + 1 == attempts:
                    raise UpstreamUnavailable(
                        "legacy geo-qxst is unavailable"
                    ) from exc
            except httpx.RequestError as exc:
                if attempt + 1 == attempts:
                    raise UpstreamUnavailable(
                        "legacy geo-qxst is unavailable"
                    ) from exc
        raise AssertionError("unreachable retry state")

    async def _post(
        self,
        path: str,
        *,
        data: dict[str, str],
        token: SecretStr,
        timeout_seconds: float,
        max_attempts: int,
    ) -> httpx.Response:
        attempts = max(1, max_attempts)
        for attempt in range(attempts):
            try:
                response = await self._client.post(
                    f"{self._base_url}{path}",
                    data=data,
                    headers={"geoToken": token.get_secret_value()},
                    timeout=timeout_seconds,
                )
                if _is_token_failure_response(response):
                    raise ReauthenticationRequired("登录凭据已失效，请重新认证")
                response.raise_for_status()
                return response
            except ReauthenticationRequired:
                raise
            except httpx.TimeoutException as exc:
                if attempt + 1 == attempts:
                    raise UpstreamTimeout(
                        "legacy geo-qxst request timed out"
                    ) from exc
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code < 500 or attempt + 1 == attempts:
                    raise UpstreamUnavailable(
                        "legacy geo-qxst is unavailable"
                    ) from exc
            except httpx.RequestError as exc:
                if attempt + 1 == attempts:
                    raise UpstreamUnavailable(
                        "legacy geo-qxst is unavailable"
                    ) from exc
        raise AssertionError("unreachable retry state")

    async def _post_query(
        self,
        path: str,
        *,
        params: dict[str, str],
        token: SecretStr,
        timeout_seconds: float,
        max_attempts: int,
    ) -> httpx.Response:
        attempts = max(1, max_attempts)
        for attempt in range(attempts):
            try:
                response = await self._client.post(
                    f"{self._base_url}{path}",
                    params=params,
                    headers={"geoToken": token.get_secret_value()},
                    timeout=timeout_seconds,
                )
                if _is_token_failure_response(response):
                    raise ReauthenticationRequired("登录凭据已失效，请重新认证")
                response.raise_for_status()
                return response
            except ReauthenticationRequired:
                raise
            except httpx.TimeoutException as exc:
                if attempt + 1 == attempts:
                    raise UpstreamTimeout(
                        "legacy geo-qxst request timed out"
                    ) from exc
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code < 500 or attempt + 1 == attempts:
                    raise UpstreamUnavailable(
                        "legacy geo-qxst is unavailable"
                    ) from exc
            except httpx.RequestError as exc:
                if attempt + 1 == attempts:
                    raise UpstreamUnavailable(
                        "legacy geo-qxst is unavailable"
                    ) from exc
        raise AssertionError("unreachable retry state")


class LegacyCredentialResolver(Protocol):
    async def resolve(
        self,
        *,
        credential_ref: str,
        subject_user_id: str,
        app_id: str,
        run_id: str,
    ) -> SecretStr: ...


def _area_level(
    area_code: str,
) -> Literal["province", "city", "district", "street", "community", "grid"]:
    if len(area_code) == 4:
        return "city"
    if len(area_code) == 6:
        return "district"
    if len(area_code) == 9:
        return "street"
    if len(area_code) == 12:
        return "community"
    if len(area_code) == 15:
        return "grid"
    raise SemanticValidationError("unsupported legacy area code level")


def _area_code_column(area_code: str) -> str:
    columns = {
        4: "city_code",
        6: "county_code",
        9: "town_code",
        12: "community_code",
        15: "grid_code",
        17: "courtyard_code",
        21: "unifiedaddressid",
    }
    try:
        return columns[len(area_code)]
    except KeyError as exc:
        raise SemanticValidationError(
            "legacy adapter area code has an unsupported level"
        ) from exc


# 出租房 next_area 分组与人口独居老人同一通用下级聚合端点。
# 支持范围：市→区县、区县→街道、街道→社区、社区→网格
# （市级经 city_code 列聚合，qxst-sj commonUtil.paramsLoader case 4）。
_NEXT_AREA_CHILD_LABEL = {4: "区县", 6: "街道", 9: "社区", 12: "网格"}
_NEXT_AREA_CODE_LENGTH = {4: 6, 6: 9, 9: 12, 12: 15}
_GOVERNANCE_OVERVIEW_AREA_CODE_LENGTHS = frozenset({4, 6, 9, 12, 15, 17, 21})
_YEAR_MONTH = re.compile(r"[0-9]{4}-(0[1-9]|1[0-2])\Z")


def _calendar_months(start: date, end: date) -> list[str]:
    months: list[str] = []
    year = start.year
    month = start.month
    while (year, month) <= (end.year, end.month):
        months.append(f"{year:04d}-{month:02d}")
        if month == 12:
            year += 1
            month = 1
        else:
            month += 1
    return months


def _validate_area_code(
    area_code: str,
    *,
    supported_lengths: Collection[int],
    capability_name: str,
) -> None:
    if (
        not area_code.isascii()
        or not area_code.isdigit()
        or len(area_code) not in supported_lengths
    ):
        raise SemanticValidationError(
            f"legacy {capability_name} adapter area code must contain only "
            "ASCII digits at a supported administrative level"
        )


def _validate_governance_overview_query(
    arguments: QueryGovernanceOverviewInput,
) -> None:
    _validate_area_code(
        arguments.query.scope.area_code,
        supported_lengths=_GOVERNANCE_OVERVIEW_AREA_CODE_LENGTHS,
        capability_name="governance overview",
    )


def _validate_enterprise_next_area_query(
    arguments: QueryEnterpriseMetricsInput,
) -> None:
    if arguments.query.group_by != ["next_area"]:
        raise SemanticValidationError(
            "legacy enterprise adapter supports only group_by=['next_area']"
        )
    _validate_area_code(
        arguments.query.scope.area_code,
        supported_lengths=_NEXT_AREA_CODE_LENGTH,
        capability_name="enterprise direct-child",
    )


def _parse_non_negative_integer(value: object) -> int:
    if isinstance(value, bool):
        raise ValueError("boolean is not an integer count")
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str) and value.isascii() and value.isdigit():
        parsed = int(value)
    else:
        raise ValueError("count must be an integer or an ASCII digit string")
    if parsed < 0:
        raise ValueError("count must be non-negative")
    return parsed


def _required_mapping_value(value: object, field: str) -> object:
    if not isinstance(value, Mapping) or field not in value:
        raise ValueError(f"missing required field {field}")
    return value[field]


def _parse_room_use_code(value: object) -> str:
    raw_code = _required_mapping_value(value, "key")
    if not isinstance(raw_code, str) or not raw_code.strip():
        raise ValueError("room-use code must be a non-empty string")
    return raw_code.strip()


def _unwrap_room_use_dictionary(response: httpx.Response) -> dict[str, str]:
    raw_dictionary = _unwrap_standard_result(response)
    if not isinstance(raw_dictionary, list):
        raise UpstreamContractError("legacy dictionary response data is not a list")
    matches: list[object] = []
    for item in raw_dictionary:
        if not isinstance(item, Mapping):
            raise UpstreamContractError("legacy dictionary response is malformed")
        if "room_user" in item:
            matches.append(item["room_user"])
    if len(matches) != 1 or not isinstance(matches[0], list):
        raise UpstreamContractError(
            "legacy room-use dictionary is missing or duplicated"
        )
    labels: dict[str, str] = {}
    for entry in matches[0]:
        if not isinstance(entry, Mapping):
            raise UpstreamContractError("legacy room-use dictionary is malformed")
        code = entry.get("dicValue")
        label = entry.get("dicName")
        if (
            not isinstance(code, str)
            or not code.strip()
            or not isinstance(label, str)
            or not label.strip()
            or code.strip() in labels
        ):
            raise UpstreamContractError("legacy room-use dictionary is malformed")
        labels[code.strip()] = label.strip()
    if not labels:
        raise UpstreamContractError("legacy room-use dictionary is empty")
    return labels


def _unwrap_enterprise_type_dictionary(response: httpx.Response) -> dict[str, str]:
    raw_dictionary = _unwrap_standard_result(response)
    if not isinstance(raw_dictionary, list):
        raise UpstreamContractError("legacy dictionary response data is not a list")
    matches: list[object] = []
    for item in raw_dictionary:
        if not isinstance(item, Mapping):
            raise UpstreamContractError("legacy dictionary response is malformed")
        if "enterprise_type" in item:
            matches.append(item["enterprise_type"])
    if len(matches) != 1 or not isinstance(matches[0], list):
        raise UpstreamContractError(
            "legacy enterprise-type dictionary is missing or duplicated"
        )
    labels: dict[str, str] = {}
    seen_labels: set[str] = set()
    for entry in matches[0]:
        if not isinstance(entry, Mapping):
            raise UpstreamContractError(
                "legacy enterprise-type dictionary is malformed"
            )
        code = entry.get("dicValue")
        label = entry.get("dicName")
        if (
            not isinstance(code, str)
            or not code.strip()
            or not isinstance(label, str)
            or not label.strip()
            or code.strip() in labels
            or label.strip() in seen_labels
        ):
            raise UpstreamContractError(
                "legacy enterprise-type dictionary is malformed"
            )
        labels[code.strip()] = label.strip()
        seen_labels.add(label.strip())
    if not labels:
        raise UpstreamContractError("legacy enterprise-type dictionary is empty")
    return labels


def _unwrap_event_category_dictionary(response: httpx.Response) -> dict[str, str]:
    raw_dictionary = _unwrap_standard_result(response)
    if not isinstance(raw_dictionary, list):
        raise UpstreamContractError("legacy dictionary response data is not a list")
    matches: list[object] = []
    for item in raw_dictionary:
        if not isinstance(item, Mapping):
            raise UpstreamContractError("legacy dictionary response is malformed")
        if "eventtype_code1" in item:
            matches.append(item["eventtype_code1"])
    if len(matches) != 1 or not isinstance(matches[0], list):
        raise UpstreamContractError(
            "legacy event-category dictionary is missing or duplicated"
        )
    labels: dict[str, str] = {}
    seen_labels: set[str] = set()
    for entry in matches[0]:
        if not isinstance(entry, Mapping):
            raise UpstreamContractError(
                "legacy event-category dictionary is malformed"
            )
        code = entry.get("dicValue")
        label = entry.get("dicName")
        if (
            not isinstance(code, str)
            or not code.strip()
            or not isinstance(label, str)
            or not label.strip()
            or code.strip() in labels
            or label.strip() in seen_labels
        ):
            raise UpstreamContractError(
                "legacy event-category dictionary is malformed"
            )
        labels[code.strip()] = label.strip()
        seen_labels.add(label.strip())
    if not labels:
        raise UpstreamContractError("legacy event-category dictionary is empty")
    return labels


def _validate_housing_next_area_query(
    arguments: QueryHousingMetricsInput,
) -> None:
    if arguments.query.group_by != ["next_area"]:
        raise SemanticValidationError(
            "legacy housing adapter supports only group_by=['next_area']"
        )
    if len(arguments.query.scope.area_code) not in _NEXT_AREA_CHILD_LABEL:
        raise SemanticValidationError(
            "legacy housing adapter supports next_area grouping only for "
            "city, district, street, or community scopes"
        )


def _parse_percent(value: object) -> float:
    try:
        parsed = float(str(value).strip().removesuffix("%"))
    except (TypeError, ValueError) as exc:
        raise UpstreamContractError("legacy event finish rate is malformed") from exc
    if not 0 <= parsed <= 100:
        raise UpstreamContractError("legacy event finish rate is out of range")
    return parsed


def _unwrap_standard_envelope(response: httpx.Response) -> dict[str, object]:
    try:
        envelope = response.json()
    except ValueError as exc:
        raise UpstreamContractError("legacy response is not valid JSON") from exc
    if not isinstance(envelope, dict) or "data" not in envelope:
        raise UpstreamContractError("legacy response envelope is malformed")
    if envelope.get("code") in {401, 403}:
        raise ReauthenticationRequired("登录凭据已失效，请重新认证")
    if envelope.get("state") is not True or envelope.get("code") != 200:
        raise UpstreamUnavailable(str(envelope.get("msg") or "legacy service failed"))
    return envelope


def _unwrap_standard_result(response: httpx.Response) -> Any:
    envelope = _unwrap_standard_envelope(response)
    return envelope["data"]


def _unwrap_standard_result_page(response: httpx.Response) -> tuple[Any, bool]:
    envelope = _unwrap_standard_envelope(response)
    explicitly_truncated = any(
        envelope.get(field) is True
        for field in ("truncated", "hasMore", "has_more")
    )
    return envelope["data"], explicitly_truncated


def _is_token_failure_response(response: httpx.Response) -> bool:
    if response.status_code not in {401, 403}:
        return False
    try:
        envelope = response.json()
    except ValueError:
        return response.status_code == 401
    return isinstance(envelope, dict) and envelope.get("code") in {401, 403}


def _validate_population_query(arguments: QueryPopulationMetricsInput) -> str:
    filters = arguments.query.filters
    if not filters:
        population_kind = "general"
    elif (
        len(filters) == 1
        and filters[0].field == "person_category"
        and filters[0].operator == "eq"
        and filters[0].value == "solitary_elderly"
    ):
        population_kind = "solitary_elderly"
    else:
        raise SemanticValidationError(
            "population adapter supports only general population or "
            "person_category=solitary_elderly"
        )
    area_length = len(arguments.query.scope.area_code)
    supported_groups = {
        4: {"district", "descendant_street", "descendant_community"},
        6: {"street"},
        9: {"community"},
        12: {"grid"},
    }.get(area_length, set())
    if len(arguments.query.group_by) != 1 or arguments.query.group_by[0] not in supported_groups:
        raise SemanticValidationError(
            "legacy adapter supports only declared population area groupings"
        )
    return population_kind


def _population_result(
    *,
    arguments: QueryPopulationMetricsInput,
    parsed_rows: list[tuple[str, str, int]],
    upstream_truncated: bool,
    ranking: bool,
) -> TableDataResult:
    """Apply the declared operation once, after the complete safe projection."""

    operator = arguments.query.operator
    if upstream_truncated and operator in {
        "sum",
        "avg",
        "min",
        "max",
        "top",
        "bottom",
        "rank",
    }:
        raise UpstreamContractError(
            "population operation requires a complete upstream row set"
        )
    if operator in {"sum", "avg", "min", "max"}:
        values = [row[2] for row in parsed_rows]
        if not values:
            raise UpstreamContractError(
                "population aggregate is undefined for an empty row set"
            )
        if operator == "sum":
            value = float(sum(values))
        elif operator == "avg":
            value = sum(values) / len(values)
        elif operator == "min":
            value = float(min(values))
        else:
            value = float(max(values))
        data = PopulationAggregateTable(
            rows=[
                PopulationAggregateRow(
                    operator=cast(Literal["sum", "avg", "min", "max"], operator),
                    metric="person_count",
                    value=value,
                    area_count=len(values),
                    completeness="complete",
                )
            ]
        )
        schema_ref = "schema://data/population-aggregate-table/1.0.0"
        return TableDataResult(
            result_id=new_id("res"),
            data_schema_ref=schema_ref,
            result_fingerprint=canonical_fingerprint(
                domain="data-result:population-aggregate-table:1.0.0",
                value=data,
            ),
            data=data,
            row_count=1,
        )

    ordered = list(parsed_rows)
    if operator in {"top", "rank"}:
        ordered.sort(key=lambda row: (-row[2], row[0]))
    elif operator == "bottom":
        ordered.sort(key=lambda row: (row[2], row[0]))
    elif arguments.query.order_by:
        direction = arguments.query.order_by[0].direction
        ordered.sort(
            key=(
                (lambda row: (-row[2], row[0]))
                if direction == "desc"
                else (lambda row: (row[2], row[0]))
            )
        )

    limit = arguments.query.limit
    selected = ordered[:limit]
    if operator in {"top", "bottom", "rank"} and selected and len(ordered) > limit:
        boundary = selected[-1][2]
        selected.extend(row for row in ordered[limit:] if row[2] == boundary)

    use_ranking = ranking or operator in {"top", "bottom", "rank"}
    if use_ranking:
        ranked_rows: list[PopulationRankingRow] = []
        previous_value: int | None = None
        previous_rank = 0
        for index, (area_code, area_name, person_count) in enumerate(
            selected, start=1
        ):
            rank = previous_rank if person_count == previous_value else index
            ranked_rows.append(
                PopulationRankingRow(
                    rank=rank,
                    area_code=area_code,
                    area_name=area_name,
                    person_count=person_count,
                )
            )
            previous_value = person_count
            previous_rank = rank
        data = PopulationRankingTable(
            rows=ranked_rows,
            candidate_count=len(parsed_rows),
            tie_policy="include_all",
        )
        schema_ref = "schema://data/population-ranking-table/1.0.0"
        domain = "data-result:population-ranking-table:1.0.0"
    else:
        data = PopulationMetricTable(
            rows=[
                PopulationMetricRow(
                    area_code=area_code,
                    area_name=area_name,
                    person_count=person_count,
                )
                for area_code, area_name, person_count in selected
            ]
        )
        schema_ref = "schema://data/population-metric-table/1.0.0"
        domain = "data-result:population-metric-table:1.0.0"
    return TableDataResult(
        result_id=new_id("res"),
        data_schema_ref=schema_ref,
        result_fingerprint=canonical_fingerprint(domain=domain, value=data),
        data=data,
        row_count=len(selected),
        truncated=(
            upstream_truncated
            or (
                operator in {"list", "rank"}
                and len(selected) < len(ordered)
            )
        ),
    )


def _validate_housing_descendant_street_query(
    arguments: QueryHousingMetricsInput,
) -> None:
    if arguments.query.group_by != ["descendant_street"]:
        raise SemanticValidationError(
            "legacy housing adapter supports only group_by=['descendant_street']"
        )
    area_code = arguments.query.scope.area_code
    if (
        len(area_code) != 4
        or not area_code.isascii()
        or not area_code.isdigit()
    ):
        raise SemanticValidationError(
            "housing descendant_street grouping requires a city scope"
        )
