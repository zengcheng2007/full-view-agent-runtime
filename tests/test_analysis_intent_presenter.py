"""P2 研判意图虚拟能力呈现测试：动态 goals 词表 + fail closed。

AnalysisIntentToolPresenter 由当前 SemanticCatalog + AuthContext 派生模型
可见的 ``agent.request_regional_analysis`` 呈现：

- goals enum 只暴露 Catalog 声明、存在 verified binding、且授权
  entitlement/dataset/scope 可用的主题；overview 仅在至少一个主题可执行
  时暴露；
- 意图范围（named_area/current_area）一律经服务端生产区划解析落地：
  授权必须同时持有 governance.area.read entitlement、administrative_area
  dataset 与至少一个可达区域 scope 才呈现，缺任一项返回 None；
- 无任何可研判主题时返回 None（虚拟能力对模型不可见）；
- schema 只含 named_area/current_area scope，additionalProperties=false，
  绝不暴露 steps/sql/url/adapter/budget/area_code。
"""

import json

from full_view_agent.application.analysis_intent_presenter import (
    AnalysisIntentToolPresenter,
)
from full_view_agent.application.harness import ANALYSIS_INTENT_TOOL_ID
from full_view_agent.domain.models import AuthContext, AuthorizedAreaScope
from full_view_agent.semantic.catalog import SemanticCatalog

from .test_policy import population_auth_context

POPULATION_ENTITLEMENT = "governance.population.aggregate.read"
HOUSING_ENTITLEMENT = "governance.housing.aggregate.read"
EVENT_ENTITLEMENT = "governance.event.aggregate.read"
# 生产区划解析（governance.resolve_area）前置授权：正向用例必须显式持有。
AREA_ENTITLEMENT = "governance.area.read"
AREA_DATASET = "administrative_area"

SENSITIVE_FIELDS = ("steps", "sql", "url", "adapter", "budget", "area_code")


def _auth_context(
    *,
    entitlements: tuple[str, ...],
    datasets: tuple[str, ...],
    areas: tuple[AuthorizedAreaScope, ...] | None = None,
) -> AuthContext:
    base = population_auth_context()
    data_scopes = base.data_scopes.model_copy(
        update={
            "datasets": list(datasets),
            "areas": list(areas) if areas is not None else list(base.data_scopes.areas),
        }
    )
    return base.model_copy(
        update={"entitlements": list(entitlements), "data_scopes": data_scopes}
    )


def _population_auth() -> AuthContext:
    return _auth_context(
        entitlements=(POPULATION_ENTITLEMENT, AREA_ENTITLEMENT),
        datasets=("population", AREA_DATASET),
    )


def _full_auth() -> AuthContext:
    return _auth_context(
        entitlements=(
            POPULATION_ENTITLEMENT,
            HOUSING_ENTITLEMENT,
            EVENT_ENTITLEMENT,
            AREA_ENTITLEMENT,
        ),
        datasets=("population", "housing", "event", AREA_DATASET),
    )


def test_presenter_exposes_only_authorized_bindable_goals() -> None:
    presenter = AnalysisIntentToolPresenter(catalog=SemanticCatalog.default())

    presentation = presenter.present(auth_context=_population_auth())

    assert presentation is not None
    assert presentation.tool_id == ANALYSIS_INTENT_TOOL_ID
    assert presentation.goals == ("overview", "population")
    schema = presentation.input_schema
    goals_property = schema["properties"]["goals"]
    assert isinstance(goals_property, dict)
    assert goals_property["items"]["enum"] == ["overview", "population"]


def test_presenter_exposes_all_goals_for_full_authorization() -> None:
    presenter = AnalysisIntentToolPresenter(catalog=SemanticCatalog.default())

    presentation = presenter.present(auth_context=_full_auth())

    assert presentation is not None
    assert presentation.goals == ("overview", "population", "housing", "event")


def test_presenter_follows_catalog_bindings_not_hardcoded_whitelist() -> None:
    catalog = SemanticCatalog.default()
    partial = SemanticCatalog(
        catalog_version=catalog.catalog_version,
        supported_spec_versions=catalog.supported_spec_versions,
        subjects=catalog.subjects,
        bindings={"housing": catalog.bindings["housing"]},
    )
    presenter = AnalysisIntentToolPresenter(catalog=partial)
    auth = _auth_context(
        entitlements=(POPULATION_ENTITLEMENT, HOUSING_ENTITLEMENT, AREA_ENTITLEMENT),
        datasets=("population", "housing", AREA_DATASET),
    )

    presentation = presenter.present(auth_context=auth)

    assert presentation is not None
    # population 已授权但无绑定：不得暴露；housing 有绑定：暴露。
    assert presentation.goals == ("overview", "housing")


def test_presenter_returns_none_without_entitlement() -> None:
    presenter = AnalysisIntentToolPresenter(catalog=SemanticCatalog.default())
    # 区划解析前置齐备、仅缺主题 entitlement：同样不得呈现。
    auth = _auth_context(
        entitlements=(AREA_ENTITLEMENT,),
        datasets=("population", AREA_DATASET),
    )

    assert presenter.present(auth_context=auth) is None


def test_presenter_returns_none_without_dataset() -> None:
    presenter = AnalysisIntentToolPresenter(catalog=SemanticCatalog.default())
    auth = _auth_context(
        entitlements=(POPULATION_ENTITLEMENT, AREA_ENTITLEMENT),
        datasets=(AREA_DATASET,),
    )

    assert presenter.present(auth_context=auth) is None


def test_presenter_returns_none_when_scope_cannot_reach_any_subject() -> None:
    presenter = AnalysisIntentToolPresenter(catalog=SemanticCatalog.default())
    # 市级编码且不含下级：population 仅支持 6/9/12 层级，主题不可达。
    auth = _auth_context(
        entitlements=(POPULATION_ENTITLEMENT, AREA_ENTITLEMENT),
        datasets=("population", AREA_DATASET),
        areas=(AuthorizedAreaScope(area_code="3301", include_descendants=False),),
    )

    assert presenter.present(auth_context=auth) is None


def test_presenter_returns_none_without_area_resolution_entitlement() -> None:
    presenter = AnalysisIntentToolPresenter(catalog=SemanticCatalog.default())
    # 主题授权与区划数据集齐备、仅缺 governance.area.read：
    # named_area/current_area 无法经生产区划解析落地，不得呈现。
    auth = _auth_context(
        entitlements=(POPULATION_ENTITLEMENT,),
        datasets=("population", AREA_DATASET),
    )

    assert presenter.present(auth_context=auth) is None


def test_presenter_returns_none_without_area_resolution_dataset() -> None:
    presenter = AnalysisIntentToolPresenter(catalog=SemanticCatalog.default())
    auth = _auth_context(
        entitlements=(POPULATION_ENTITLEMENT, AREA_ENTITLEMENT),
        datasets=("population",),
    )

    assert presenter.present(auth_context=auth) is None


def test_presenter_returns_none_without_any_reachable_area_scope() -> None:
    presenter = AnalysisIntentToolPresenter(catalog=SemanticCatalog.default())
    # entitlement/dataset 齐备但无任何可达授权区域：区划解析没有可落地
    # 的范围，fail closed 不呈现。
    auth = _auth_context(
        entitlements=(POPULATION_ENTITLEMENT, AREA_ENTITLEMENT),
        datasets=("population", AREA_DATASET),
        areas=(),
    )

    assert presenter.present(auth_context=auth) is None


def test_presenter_returns_none_when_no_subject_is_bindable() -> None:
    catalog = SemanticCatalog.default()
    unbound = SemanticCatalog(
        catalog_version=catalog.catalog_version,
        supported_spec_versions=catalog.supported_spec_versions,
        subjects=catalog.subjects,
        bindings={},
    )
    presenter = AnalysisIntentToolPresenter(catalog=unbound)

    assert presenter.present(auth_context=_full_auth()) is None


def test_presenter_schema_is_closed_and_free_of_sensitive_fields() -> None:
    presenter = AnalysisIntentToolPresenter(catalog=SemanticCatalog.default())

    presentation = presenter.present(auth_context=_population_auth())

    assert presentation is not None
    schema = presentation.input_schema
    assert schema["additionalProperties"] is False
    assert set(schema["properties"]) == {"schema_version", "kind", "goals", "scope"}
    assert schema["required"] == ["goals", "scope"]
    defs = schema["$defs"]
    assert set(defs) == {"NamedAreaScopeIntent", "CurrentAreaScopeIntent"}
    for definition in defs.values():
        assert definition["additionalProperties"] is False
    scope_schema = schema["properties"]["scope"]
    assert isinstance(scope_schema["oneOf"], list)
    referenced = {
        str(option["$ref"]).rsplit("/", 1)[-1]
        for option in scope_schema["oneOf"]
        if isinstance(option, dict)
    }
    assert referenced == {"NamedAreaScopeIntent", "CurrentAreaScopeIntent"}
    serialized = json.dumps(schema, ensure_ascii=False)
    for field in SENSITIVE_FIELDS:
        assert f'"{field}"' not in serialized
    # 工具描述同样不得暴露敏感执行字段。
    for field in SENSITIVE_FIELDS:
        assert f"{field}=" not in presentation.description


def test_presenter_description_only_claims_authorized_goals() -> None:
    presenter = AnalysisIntentToolPresenter(catalog=SemanticCatalog.default())

    presentation = presenter.present(auth_context=_population_auth())

    assert presentation is not None
    assert "population" in presentation.description
    assert "housing" not in presentation.description
    assert "event" not in presentation.description
