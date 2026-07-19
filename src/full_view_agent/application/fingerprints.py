import hashlib
import json

from pydantic import BaseModel, TypeAdapter

JSON_OBJECT_ADAPTER = TypeAdapter(dict[str, object])


def canonical_fingerprint(*, domain: str, value: BaseModel | dict[str, object]) -> str:
    payload = (
        value.model_dump(mode="json")
        if isinstance(value, BaseModel)
        else JSON_OBJECT_ADAPTER.dump_python(value, mode="json")
    )
    canonical = json.dumps(
        {"domain": domain, "payload": payload},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"
