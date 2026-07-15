#!/usr/bin/env python3
"""Probe whether device_code START works for substrate-like scopes. No MFA completion."""
from __future__ import annotations

import asyncio
import json

import httpx

CLIENTS = {
    "custom-zero": "00000000-0000-0000-0000-000000000000",
    "ms-office": "d3590ed6-52b3-4102-aeff-aad2292ab01c",
    "az-cli": "04b07795-8ddb-461a-bbee-02f9e1bf7b46",
}
SCOPES = [
    "https://substrate.office.com/ows/.default offline_access openid profile",
    "https://substrate.office.com/.default offline_access openid profile",
    "https://graph.microsoft.com/.default offline_access openid profile",
]


async def try_device(client_id: str, scope: str) -> dict:
    url = "https://login.microsoftonline.com/common/oauth2/v2.0/devicecode"
    async with httpx.AsyncClient(timeout=20.0) as client:
        r = await client.post(url, data={"client_id": client_id, "scope": scope})
        body = r.json() if r.content else {}
        return {
            "http": r.status_code,
            "ok_start": r.status_code < 400 and "device_code" in body,
            "error": body.get("error"),
            "desc": (body.get("error_description") or body.get("message") or "")[:200],
            "user_code": body.get("user_code"),
        }


async def poll_pending(client_id: str, scope: str) -> dict:
    async with httpx.AsyncClient(timeout=20.0) as client:
        r = await client.post(
            "https://login.microsoftonline.com/common/oauth2/v2.0/devicecode",
            data={"client_id": client_id, "scope": scope},
        )
        start = r.json()
        if "device_code" not in start:
            return {"start": start}
        t = await client.post(
            "https://login.microsoftonline.com/common/oauth2/v2.0/token",
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "client_id": client_id,
                "device_code": start["device_code"],
            },
        )
        body = t.json()
        return {
            "start_ok": True,
            "poll_http": t.status_code,
            "poll_error": body.get("error"),
            "has_access_token": bool(body.get("access_token")),
            "has_refresh_token": bool(body.get("refresh_token")),
        }


async def main() -> int:
    for name, cid in CLIENTS.items():
        for scope in SCOPES:
            row = await try_device(cid, scope)
            flag = "OK" if row["ok_start"] else "NO"
            print(f"{flag} {name:12} {row['http']} {row.get('error')} {scope[:52]}")
    print("--- poll without user (ms-office + substrate ows) ---")
    print(json.dumps(await poll_pending(CLIENTS["ms-office"], SCOPES[0]), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
