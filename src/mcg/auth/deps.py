from __future__ import annotations

from fastapi import Header, HTTPException, Request


def require_api_key(request: Request, authorization: str | None = Header(default=None)) -> str:
    cfg = request.app.state.config
    keys = set(cfg.gateway.api_keys or [])
    if not keys:
        raise HTTPException(status_code=500, detail="gateway.api_keys not configured")
    token = None
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    if not token:
        token = request.headers.get("x-api-key")
    if not token or token not in keys:
        raise HTTPException(status_code=401, detail="invalid api key")
    return token


def verify_admin_password(password: str, expected: str) -> bool:
    return bool(password) and password == expected
