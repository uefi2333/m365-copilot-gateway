from __future__ import annotations


"""Lightweight Azure AD token paths — HTTP only, no browser binary.

1) refresh_token grant (silent)
2) device_code flow (user opens URL on any phone/PC; server only polls)

Substrate resource commonly appears as:
  https://substrate.office.com/ows/.default

You must use a client_id allowed for that resource (your own Entra app with
admin consent, or a token you already obtained offline). This module does not
ship Microsoft first-party secrets.
"""


import asyncio
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx

DEFAULT_SCOPE = "https://substrate.office.com/ows/.default offline_access openid profile"


@dataclass
class OAuthTokens:
    access_token: str
    refresh_token: str | None = None
    expires_in: int = 0
    token_type: str = "Bearer"
    raw: dict[str, Any] | None = None


@dataclass
class DeviceCodeStart:
    device_code: str
    user_code: str
    verification_uri: str
    expires_in: int
    interval: int
    message: str


class OAuthError(RuntimeError):
    pass


def _token_endpoint(tenant: str) -> str:
    return f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"


def _device_endpoint(tenant: str) -> str:
    return f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/devicecode"


async def refresh_with_refresh_token(
    *,
    refresh_token: str,
    client_id: str,
    tenant: str = "common",
    scope: str = DEFAULT_SCOPE,
    client_secret: str | None = None,
    timeout: float = 30.0,
) -> OAuthTokens:
    """Silent renew — only needs httpx. No browser."""
    data: dict[str, str] = {
        "client_id": client_id,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "scope": scope,
    }
    if client_secret:
        data["client_secret"] = client_secret
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.post(
            _token_endpoint(tenant),
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if r.status_code >= 400:
            err = body.get("error_description") or body.get("error") or r.text[:300]
            raise OAuthError(f"refresh_token failed: {err}")
        access = body.get("access_token")
        if not access:
            raise OAuthError("refresh response missing access_token")
        return OAuthTokens(
            access_token=access,
            refresh_token=body.get("refresh_token") or refresh_token,
            expires_in=int(body.get("expires_in") or 0),
            token_type=str(body.get("token_type") or "Bearer"),
            raw=body,
        )


async def start_device_code(
    *,
    client_id: str,
    tenant: str = "common",
    scope: str = DEFAULT_SCOPE,
    timeout: float = 30.0,
) -> DeviceCodeStart:
    data = {"client_id": client_id, "scope": scope}
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.post(
            _device_endpoint(tenant),
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        body = r.json() if r.content else {}
        if r.status_code >= 400:
            err = body.get("error_description") or body.get("error") or r.text[:300]
            raise OAuthError(f"device_code start failed: {err}")
        return DeviceCodeStart(
            device_code=body["device_code"],
            user_code=body["user_code"],
            verification_uri=body.get("verification_uri") or body.get("verification_uri_complete") or "",
            expires_in=int(body.get("expires_in") or 900),
            interval=int(body.get("interval") or 5),
            message=body.get("message") or f"open {body.get('verification_uri')} code {body.get('user_code')}",
        )


async def poll_device_code(
    *,
    client_id: str,
    device_code: str,
    tenant: str = "common",
    interval: int = 5,
    expires_in: int = 900,
    client_secret: str | None = None,
    timeout: float = 30.0,
    on_status: Any = None,
) -> OAuthTokens:
    """Poll until user finishes device login. HTTP only."""
    deadline = time.time() + expires_in
    data: dict[str, str] = {
        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        "client_id": client_id,
        "device_code": device_code,
    }
    if client_secret:
        data["client_secret"] = client_secret
    async with httpx.AsyncClient(timeout=timeout) as client:
        while time.time() < deadline:
            r = await client.post(
                _token_endpoint(tenant),
                data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            body = r.json() if r.content else {}
            if r.status_code < 400 and body.get("access_token"):
                return OAuthTokens(
                    access_token=body["access_token"],
                    refresh_token=body.get("refresh_token"),
                    expires_in=int(body.get("expires_in") or 0),
                    token_type=str(body.get("token_type") or "Bearer"),
                    raw=body,
                )
            err = body.get("error")
            if err in ("authorization_pending", "slow_down"):
                wait = interval + (3 if err == "slow_down" else 0)
                if on_status:
                    on_status(f"waiting user login… ({err})")
                await asyncio.sleep(wait)
                continue
            if err == "expired_token":
                raise OAuthError("device code expired")
            raise OAuthError(body.get("error_description") or err or r.text[:300])
    raise OAuthError("device code timed out")


async def device_code_login(
    *,
    client_id: str,
    tenant: str = "common",
    scope: str = DEFAULT_SCOPE,
    client_secret: str | None = None,
    on_status: Any = None,
) -> OAuthTokens:
    start = await start_device_code(client_id=client_id, tenant=tenant, scope=scope)
    if on_status:
        on_status(start.message)
        on_status(f"verification_uri={start.verification_uri} user_code={start.user_code}")
    return await poll_device_code(
        client_id=client_id,
        device_code=start.device_code,
        tenant=tenant,
        interval=start.interval,
        expires_in=start.expires_in,
        client_secret=client_secret,
        on_status=on_status,
    )


def form_body(**kwargs: str) -> str:
    return urlencode(kwargs)
