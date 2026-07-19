import base64
import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta

from full_view_agent.application.errors import InvalidCursor


class SignedCursorCodec:
    def __init__(self, *, signing_key: bytes, ttl_seconds: int = 600) -> None:
        if len(signing_key) < 32:
            raise ValueError("cursor signing key must contain at least 32 bytes")
        self._signing_key = signing_key
        self._ttl = timedelta(seconds=ttl_seconds)

    def encode(
        self,
        *,
        user_id: str,
        resource_id: str,
        offset: int,
        limit: int,
    ) -> str:
        payload = json.dumps(
            {
                "user_id": user_id,
                "resource_id": resource_id,
                "offset": offset,
                "limit": limit,
                "expires_at": int((datetime.now(UTC) + self._ttl).timestamp()),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        signature = hmac.new(self._signing_key, payload, hashlib.sha256).digest()
        return f"{_encode(payload)}.{_encode(signature)}"

    def decode(
        self,
        cursor: str,
        *,
        user_id: str,
        resource_id: str,
        limit: int,
    ) -> int:
        try:
            payload_part, signature_part = cursor.split(".", 1)
            payload = _decode(payload_part)
            supplied_signature = _decode(signature_part)
            expected_signature = hmac.new(
                self._signing_key,
                payload,
                hashlib.sha256,
            ).digest()
            claims = json.loads(payload)
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise InvalidCursor("cursor is invalid or expired") from exc
        if not hmac.compare_digest(supplied_signature, expected_signature):
            raise InvalidCursor("cursor is invalid or expired")
        if (
            claims.get("user_id") != user_id
            or claims.get("resource_id") != resource_id
            or claims.get("limit") != limit
            or not isinstance(claims.get("offset"), int)
            or claims.get("offset") < 0
            or not isinstance(claims.get("expires_at"), int)
            or claims.get("expires_at") <= int(datetime.now(UTC).timestamp())
        ):
            raise InvalidCursor("cursor is invalid or expired")
        return claims["offset"]


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.b64decode(value + padding, altchars=b"-_", validate=True)
