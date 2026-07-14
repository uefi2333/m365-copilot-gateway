from __future__ import annotations

import base64
import json
import time
from typing import Any

SUBSTRATE_AUD_PREFIX = "https://substrate.office.com/"


def decode_jwt_payload(token: str) -> dict[str, Any]:
    parts = token.split(".")
    if len(parts) < 2:
        raise ValueError("not a JWT")
    payload = parts[1]
    payload += "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))


def is_substrate_token(claims: dict[str, Any]) -> bool:
    return str(claims.get("aud", "")).startswith(SUBSTRATE_AUD_PREFIX)


def seconds_remaining(claims: dict[str, Any]) -> int:
    exp = int(claims.get("exp") or 0)
    return max(0, exp - int(time.time()))
