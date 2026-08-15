from pydantic import JsonValue

from full_view_agent.application.answer_claims import AnswerClaim
from full_view_agent.application.contract_result_finisher import ContractResultFinisher
from full_view_agent.domain.models import (
    DynamicTableData,
    ResultDisplayField,
    ResultPresentation,
    SemanticResultLineage,
    TableDataResult,
    ToolResult,
)


def _lineage(*, operator: str, completeness: str = "complete") -> SemanticResultLineage:
    return SemanticResultLineage(
        virtual_tool_id="governance.semantic_query",
        virtual_tool_version="1.0.0",
        spec_version="1.0",
        catalog_version="run-contracts",
        subject="device",
        logical_dataset_id="devices",
        canonical_tool_id="governance.query_device_metrics",
        canonical_tool_version="2.0.0",
        spec_fingerprint="sha256:spec",
        plan_fingerprint="sha256:plan",
        semantic_contract_fingerprint="sha256:contract",
        semantic_shape_id=f"device_{operator}",
        semantic_operator=operator,
        semantic_completeness=completeness,
        semantic_tie_policy="include_all",
        area_code="3301",
        output="table",
    )


def _result(*, operator: str, rows: list[dict[str, JsonValue]]) -> ToolResult:
    return ToolResult(
        tool_call_id="tc-device",
        tool_id="governance.query_device_metrics",
        tool_version="2.0.0",
        status="success",
        summary="查询成功",
        semantic_lineage=_lineage(operator=operator),
        data_result=TableDataResult(
            result_id="res-device",
            data_schema_ref=f"schema://data/device-{operator}/1.0.0",
            result_fingerprint="sha256:result",
            data=DynamicTableData(rows=rows),
            row_count=len(rows),
            truncated=False,
            presentation=ResultPresentation(
                title="设备统计",
                summary="设备统计已完成。",
                status_label="查询完成",
                fields=[
                    ResultDisplayField(
                        field="rank", label="排名", role="dimension"
                    ),
                    ResultDisplayField(
                        field="area_code", label="区划编码", role="identifier"
                    ),
                    ResultDisplayField(
                        field="area_name", label="区划名称", role="dimension"
                    ),
                    ResultDisplayField(
                        field="device_count", label="设备数量", role="metric", unit="台"
                    ),
                ],
            ),
        ),
    )


def test_contract_result_finisher_uses_lineage_and_presentation_for_bottom_ties() -> None:
    result = _result(
        operator="bottom",
        rows=[
            {"rank": 1, "area_code": "a", "area_name": "甲街道", "device_count": 3},
            {"rank": 1, "area_code": "b", "area_name": "乙街道", "device_count": 3},
            {"rank": 2, "area_code": "c", "area_name": "丙街道", "device_count": 9},
        ],
    )

    finish = ContractResultFinisher().finish((result,))

    assert finish is not None
    assert finish.summary == "甲街道、乙街道的设备数量均为3台，并列本次查询最低。"
    assert finish.claims == [
        AnswerClaim(
            claim_id="contract-result-1-rank",
            result_id="res-device",
            result_fingerprint="sha256:result",
            collection="rows",
            row_locator={"area_code": "a"},
            field="rank",
            operation="value",
            value=1,
        ),
        AnswerClaim(
            claim_id="contract-result-1-metric",
            result_id="res-device",
            result_fingerprint="sha256:result",
            collection="rows",
            row_locator={"area_code": "a"},
            field="device_count",
            operation="value",
            value=3,
        ),
        AnswerClaim(
            claim_id="contract-result-2-rank",
            result_id="res-device",
            result_fingerprint="sha256:result",
            collection="rows",
            row_locator={"area_code": "b"},
            field="rank",
            operation="value",
            value=1,
        ),
        AnswerClaim(
            claim_id="contract-result-2-metric",
            result_id="res-device",
            result_fingerprint="sha256:result",
            collection="rows",
            row_locator={"area_code": "b"},
            field="device_count",
            operation="value",
            value=3,
        ),
    ]


def test_contract_result_finisher_uses_presentation_summary_for_average() -> None:
    result = _result(
        operator="avg",
        rows=[{"operator": "avg", "value": 12.5, "area_count": 4}],
    )
    assert result.data_result is not None
    result = result.model_copy(
        update={
            "data_result": result.data_result.model_copy(
                update={
                    "presentation": ResultPresentation(
                        title="设备平均值",
                        summary="基于4个完整区划计算，设备平均值为12.5台。",
                        status_label="查询完成",
                        fields=[
                            ResultDisplayField(
                                field="operator", label="聚合运算", role="dimension"
                            ),
                            ResultDisplayField(
                                field="value", label="设备平均值", role="metric", unit="台"
                            ),
                            ResultDisplayField(
                                field="area_count", label="参与区划数", role="metric", unit="个"
                            ),
                        ],
                    )
                }
            )
        }
    )

    finish = ContractResultFinisher().finish((result,))

    assert finish is not None
    assert finish.summary == "基于4个完整区划计算，设备平均值为12.5台。"
    assert finish.claims[0].field == "value"
    assert finish.claims[0].value == 12.5


def test_contract_result_finisher_refuses_partial_or_ambiguous_results() -> None:
    partial = _result(operator="avg", rows=[{"operator": "avg", "value": 12.5}])
    partial = partial.model_copy(
        update={"semantic_lineage": _lineage(operator="avg", completeness="partial")}
    )

    assert ContractResultFinisher().finish((partial,)) is None
    assert ContractResultFinisher().finish((_result(operator="top", rows=[]),)) is None
    assert ContractResultFinisher().finish(
        (_result(operator="top", rows=[]), _result(operator="bottom", rows=[]))
    ) is None
