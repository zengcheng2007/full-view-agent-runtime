from typing import Literal, Protocol

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
    EventFinishRateRow,
    EventFinishRateTable,
    GetObjectProfileInput,
    HousingAreaGroupRow,
    HousingAreaGroupTable,
    HousingLeaseTypeRow,
    HousingLeaseTypeTable,
    InternalToolManifest,
    ObjectProfileData,
    ObjectProfileField,
    ObjectProfileResult,
    PolicyDecision,
    PopulationMetricRow,
    PopulationMetricTable,
    QueryEventMetricsInput,
    QueryHousingMetricsInput,
    QueryPopulationMetricsInput,
    ResolveAreaInput,
    TableDataResult,
)


class InMemoryGovernanceAdapter:
    def __init__(self) -> None:
        self._areas = [
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
        candidates = [
            area
            for area in self._areas
            if arguments.query in area.area_name
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
        data = PopulationMetricTable(
            rows=[
                PopulationMetricRow(
                    area_code=f"{area_code}001",
                    area_name="示例街道",
                    person_count=128,
                )
            ][: arguments.query.limit]
        )
        return TableDataResult(
            result_id=new_id("res"),
            data_schema_ref="schema://data/population-metric-table/1.0.0",
            result_fingerprint=canonical_fingerprint(
                domain="data-result:population-metric-table:1.0.0",
                value=data,
            ),
            data=data,
            row_count=len(data.rows),
        )

    @staticmethod
    def _query_event_metrics(
        arguments: QueryEventMetricsInput,
    ) -> TableDataResult:
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
    def _query_housing_metrics(
        arguments: QueryHousingMetricsInput,
    ) -> TableDataResult:
        if arguments.query.group_by == ["next_area"]:
            return InMemoryGovernanceAdapter._query_housing_area_distribution(
                arguments
            )
        rows = [
            HousingLeaseTypeRow(lease_type="住宅出租", dwelling_count=3200),
            HousingLeaseTypeRow(lease_type="商铺出租", dwelling_count=850),
            HousingLeaseTypeRow(lease_type="公寓出租", dwelling_count=1200),
            HousingLeaseTypeRow(lease_type="群租房", dwelling_count=320),
            HousingLeaseTypeRow(lease_type="工业出租", dwelling_count=180),
        ]
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
        )

    @staticmethod
    def _query_housing_area_distribution(
        arguments: QueryHousingMetricsInput,
    ) -> TableDataResult:
        _validate_housing_next_area_query(arguments)
        area_code = arguments.query.scope.area_code
        child_label = _NEXT_AREA_CHILD_LABEL[len(area_code)]
        rows = [
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
        ][: arguments.query.limit]
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
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._credential_broker = credential_broker
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient()

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
        _validate_solitary_elderly_query(arguments)
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
                "tableName": "dm_empty_nest_old",
            },
            token=token,
            timeout_seconds=timeout_seconds,
            max_attempts=max_attempts,
        )
        raw_rows = _unwrap_standard_result(response)
        try:
            rows = [
                PopulationMetricRow(
                    area_code=str(item["areaCode"]),
                    area_name=str(item["areaName"]),
                    person_count=int(item["total"]),
                )
                for item in raw_rows[: arguments.query.limit]
            ]
        except (KeyError, TypeError, ValueError) as exc:
            raise UpstreamContractError(
                "legacy population response is malformed"
            ) from exc
        data = PopulationMetricTable(rows=rows)
        return TableDataResult(
            result_id=new_id("res"),
            data_schema_ref="schema://data/population-metric-table/1.0.0",
            result_fingerprint=canonical_fingerprint(
                domain="data-result:population-metric-table:1.0.0",
                value=data,
            ),
            data=data,
            row_count=len(rows),
            truncated=len(raw_rows) > arguments.query.limit,
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

    async def _query_housing_metrics_http(
        self,
        *,
        arguments: QueryHousingMetricsInput,
        auth_context: AuthContext,
        timeout_seconds: float,
        max_attempts: int,
    ) -> TableDataResult:
        if arguments.query.group_by == ["next_area"]:
            return await self._query_housing_area_distribution_http(
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
        raw_rows = _unwrap_standard_result(response)
        try:
            rows = [
                HousingLeaseTypeRow(
                    lease_type=str(item["house_type"]),
                    dwelling_count=int(item["total"]),
                )
                for item in (raw_rows if isinstance(raw_rows, list) else [])
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
            truncated=False,
        )

    async def _query_housing_area_distribution_http(
        self,
        *,
        arguments: QueryHousingMetricsInput,
        auth_context: AuthContext,
        timeout_seconds: float,
        max_attempts: int,
    ) -> TableDataResult:
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
        source_rows = raw_rows if isinstance(raw_rows, list) else []
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
    return {
        4: "city_code",
        6: "county_code",
        9: "town_code",
        12: "community_code",
        15: "grid_code",
        17: "courtyard_code",
        21: "unifiedaddressid",
    }[len(area_code)]


# 出租房 next_area 分组与人口独居老人同一通用下级聚合端点。
# 支持范围：市→区县、区县→街道、街道→社区、社区→网格
# （市级经 city_code 列聚合，qxst-sj commonUtil.paramsLoader case 4）。
_NEXT_AREA_CHILD_LABEL = {4: "区县", 6: "街道", 9: "社区", 12: "网格"}


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


def _unwrap_standard_result(response: httpx.Response):
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
    return envelope["data"]


def _is_token_failure_response(response: httpx.Response) -> bool:
    if response.status_code not in {401, 403}:
        return False
    try:
        envelope = response.json()
    except ValueError:
        return response.status_code == 401
    return isinstance(envelope, dict) and envelope.get("code") in {401, 403}


def _validate_solitary_elderly_query(arguments: QueryPopulationMetricsInput) -> None:
    filters = arguments.query.filters
    if (
        len(filters) != 1
        or filters[0].field != "person_category"
        or filters[0].operator != "eq"
        or filters[0].value != "solitary_elderly"
    ):
        raise SemanticValidationError(
            "legacy adapter currently supports only person_category=solitary_elderly"
        )
    expected_group = {6: "street", 9: "community", 12: "grid"}.get(
        len(arguments.query.scope.area_code)
    )
    if expected_group is None or arguments.query.group_by != [expected_group]:
        raise SemanticValidationError(
            "legacy adapter supports only the immediate child area grouping"
        )
