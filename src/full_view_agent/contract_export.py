import json
from pathlib import Path

import yaml

from full_view_agent.api.app import create_app
from full_view_agent.application.tool_registry import ToolRegistry
from full_view_agent.contract_registry import SCHEMA_MODELS


def export_contracts(target_root: Path) -> None:
    openapi_path = target_root / "openapi" / "agent-api-v1.yaml"
    openapi_path.parent.mkdir(parents=True, exist_ok=True)
    openapi_path.write_text(
        yaml.safe_dump(
            create_app().openapi(),
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    schema_root = target_root / "schemas"
    for relative_path, model in SCHEMA_MODELS.items():
        schema_path = schema_root / relative_path
        schema_path.parent.mkdir(parents=True, exist_ok=True)
        schema_path.write_text(
            json.dumps(
                model.model_json_schema(mode="validation"),
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

    registry = ToolRegistry.default()
    manifest_root = target_root / "manifests" / "governance"
    manifest_root.mkdir(parents=True, exist_ok=True)
    for tool_id in registry.list_tool_ids():
        slug = tool_id.removeprefix("governance.").replace("_", "-")
        _write_json(
            manifest_root / f"{slug}.manifest.json",
            registry.get_manifest(tool_id).model_dump(mode="json", by_alias=True),
        )
    _write_json(
        manifest_root / "model-tool-descriptors.json",
        [
            registry.get_model_descriptor(tool_id).model_dump(
                mode="json",
                by_alias=True,
            )
            for tool_id in registry.list_tool_ids()
        ],
    )


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
