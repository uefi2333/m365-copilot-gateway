from __future__ import annotations

import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field

from mcg.auth.deps import require_api_key, verify_admin_password

router = APIRouter(prefix="/admin", tags=["admin"])


class ImportTokenBody(BaseModel):
    token: str
    label: str = ""
    admin_password: str
    refresh_token: str | None = None


class RefreshTokenBody(BaseModel):
    admin_password: str
    refresh_token: str | None = None
    cdp_http: str | None = None
    timeout_sec: float | None = None
    interactive: bool = False


class DeviceLoginBody(BaseModel):
    admin_password: str
    label: str = ""
    account_id: str | None = None


class BrowserLoginBody(BaseModel):
    admin_password: str
    label: str = ""
    account_id: str | None = None
    cdp_http: str | None = None
    timeout_sec: float | None = None
    interactive: bool = True


class PkceStartBody(BaseModel):
    admin_password: str
    label: str = ""
    account_key: str | None = None


class PkceFinishBody(BaseModel):
    admin_password: str
    code_or_url: str
    account_key: str
    label: str = ""


def _admin(
    request: Request,
    body_password: str | None = None,
    x_admin_password: str | None = None,
) -> None:
    expected = request.app.state.config.gateway.admin_password
    pw = body_password or x_admin_password
    if not verify_admin_password(pw or "", expected):
        raise HTTPException(status_code=401, detail="invalid admin password")


def _import_from_hot(pool, fabric, account_id: str, label: str, st):
    token = fabric.get_hot(account_id)
    if not token:
        raise HTTPException(status_code=500, detail="token missing")
    from mcg.token.jwtutil import decode_jwt_payload

    claims = decode_jwt_payload(token)
    real_id = str(claims.get("oid") or account_id)
    acc = pool.import_token(token, label=label or f"user-{real_id[:8]}")
    rt = fabric.get_refresh_token(account_id)
    if rt:
        fabric.put_refresh_token(acc.id, rt)
    data_dir = Path(pool.data_dir)
    for src_name, dst_name in (
        (f"{account_id}.json", f"{acc.id}.json"),
        (f"{account_id}.rt.json", f"{acc.id}.rt.json"),
    ):
        src = data_dir / "msal" / src_name
        dst = data_dir / "msal" / dst_name
        if src.exists() and not dst.exists():
            try:
                dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
            except OSError:
                pass
    return {
        "ok": True,
        "account": acc.public_dict(),
        "source": st.source,
        "seconds_remaining": st.seconds_remaining,
        "has_refresh_token": bool(fabric.get_refresh_token(acc.id)),
        "aud": claims.get("aud"),
    }


@router.get("/accounts")
async def accounts(
    request: Request,
    _key: str = Depends(require_api_key),
    x_admin_password: str | None = Header(default=None),
):
    _admin(request, x_admin_password=x_admin_password)
    fabric = request.app.state.fabric
    rows = []
    for a in request.app.state.pool.list_public():
        a = dict(a)
        st = fabric.status_dict(a["id"])
        a["has_refresh_token"] = st.get("has_refresh_token")
        a["msal_cache"] = st.get("msal_cache")
        a["token_detail"] = st
        rows.append(a)
    return {"accounts": rows}


@router.post("/accounts/import-token")
async def import_token(
    body: ImportTokenBody, request: Request, _key: str = Depends(require_api_key)
):
    _admin(request, body_password=body.admin_password)
    try:
        acc = request.app.state.pool.import_token(body.token.strip(), label=body.label)
        if body.refresh_token:
            request.app.state.fabric.put_refresh_token(acc.id, body.refresh_token.strip())
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "ok": True,
        "account": acc.public_dict(),
        "has_refresh_token": bool(request.app.state.fabric.get_refresh_token(acc.id)),
    }


@router.post("/auth/pkce/start")
async def pkce_start(
    body: PkceStartBody, request: Request, _key: str = Depends(require_api_key)
):
    _admin(request, body_password=body.admin_password)
    fabric = request.app.state.fabric
    key = body.account_key or f"pending-{uuid.uuid4().hex[:10]}"
    info = await fabric.login_pkce_start(account_key=key)
    return {
        "ok": True,
        "account_key": key,
        "label": body.label,
        **info,
    }


@router.post("/auth/pkce/finish")
async def pkce_finish(
    body: PkceFinishBody, request: Request, _key: str = Depends(require_api_key)
):
    _admin(request, body_password=body.admin_password)
    fabric = request.app.state.fabric
    pool = request.app.state.pool
    st = await fabric.login_pkce_finish(
        body.code_or_url.strip(),
        account_id=body.account_key,
        account_key=body.account_key,
    )
    if not st.valid:
        raise HTTPException(status_code=400, detail=st.error or "pkce failed")
    return _import_from_hot(pool, fabric, body.account_key, body.label, st)


@router.post("/accounts/device-login")
async def device_login(
    body: DeviceLoginBody, request: Request, _key: str = Depends(require_api_key)
):
    _admin(request, body_password=body.admin_password)
    fabric = request.app.state.fabric
    pool = request.app.state.pool
    account_id = body.account_id or f"pending-{uuid.uuid4().hex[:10]}"
    logs: list[str] = []

    def on_status(msg: str) -> None:
        logs.append(msg)

    st = await fabric.login_device_code(account_id, on_status=on_status, use_sydney=True)
    if not st.valid:
        raise HTTPException(status_code=400, detail={"error": st.error, "logs": logs})
    out = _import_from_hot(pool, fabric, account_id, body.label, st)
    out["logs"] = logs
    return out



@router.post("/auth/keepalive/tick")
async def keepalive_tick(request: Request, _key: str = Depends(require_api_key)):
    """Manual silent refresh pass (same as background keepalive)."""
    ka = getattr(request.app.state, "keepalive", None)
    if not ka:
        raise HTTPException(status_code=503, detail="keepalive not running")
    return {"ok": True, **(await ka.tick_once())}


@router.get("/auth/status")
async def auth_status(request: Request, _key: str = Depends(require_api_key)):
    pool = request.app.state.pool
    fabric = request.app.state.fabric
    ka = getattr(request.app.state, "keepalive", None)
    accounts = []
    for a in pool.list_public():
        st = fabric.status_dict(a["id"], pool.accounts[a["id"]].token if a["id"] in pool.accounts else None)
        accounts.append({**a, **{k: st.get(k) for k in (
            "has_refresh_token","msal_cache","msal_sidecar_rt","needs_refresh","use_sydney_msal"
        )}})
    return {
        "ok": True,
        "keepalive": {
            "enabled": bool(ka and ka.enabled),
            "interval_sec": getattr(ka, "interval_sec", None),
            "last": getattr(ka, "last", None),
        } if ka else None,
        "accounts": accounts,
    }


@router.post("/accounts/{account_id}/refresh")
async def refresh_account(
    account_id: str,
    request: Request,
    body: RefreshTokenBody,
    _key: str = Depends(require_api_key),
):
    _admin(request, body_password=body.admin_password)
    pool = request.app.state.pool
    fabric = request.app.state.fabric
    if account_id not in pool.accounts:
        raise HTTPException(status_code=404, detail="not found")
    if body.refresh_token:
        fabric.put_refresh_token(account_id, body.refresh_token.strip())
    if body.timeout_sec:
        fabric.cdp_timeout_sec = body.timeout_sec
    logs: list[str] = []

    def on_status(msg: str) -> None:
        logs.append(msg)

    st = await fabric.refresh_via_sydney_msal(account_id, on_status=on_status)
    if not st.valid and fabric.oauth_client_id:
        st = await fabric.refresh_via_oauth(account_id, on_status=on_status)
    if not st.valid and (body.cdp_http or fabric.prefer_cdp):
        acc = pool.accounts[account_id]
        st = await fabric.capture_via_cdp(
            account_id,
            cdp_http=body.cdp_http,
            interactive=body.interactive,
            on_status=on_status,
            profile_path=acc.profile_path or None,
        )
    if not st.valid:
        raise HTTPException(status_code=408, detail={"error": st.error, "logs": logs})
    token = fabric.get_hot(account_id)
    if not token:
        raise HTTPException(status_code=500, detail="token missing")
    pool.refresh_token(account_id, token)
    return {
        "ok": True,
        "account": pool.accounts[account_id].public_dict(),
        "source": st.source,
        "seconds_remaining": st.seconds_remaining,
        "logs": logs,
    }


@router.post("/accounts/browser-login")
async def browser_login(
    body: BrowserLoginBody, request: Request, _key: str = Depends(require_api_key)
):
    _admin(request, body_password=body.admin_password)
    fabric = request.app.state.fabric
    pool = request.app.state.pool
    account_id = body.account_id or f"pending-{uuid.uuid4().hex[:10]}"
    if body.timeout_sec:
        fabric.cdp_timeout_sec = body.timeout_sec
    logs: list[str] = []

    def on_status(msg: str) -> None:
        logs.append(msg)

    st = await fabric.capture_via_cdp(
        account_id,
        cdp_http=body.cdp_http,
        interactive=body.interactive,
        on_status=on_status,
    )
    if not st.valid:
        raise HTTPException(status_code=408, detail={"error": st.error, "logs": logs})
    out = _import_from_hot(pool, fabric, account_id, body.label, st)
    profile = fabric.profile_dir_for(out["account"]["id"])
    pool.bind_profile(out["account"]["id"], str(profile))
    out["logs"] = logs
    return out


@router.delete("/accounts/{account_id}")
async def delete_account(
    account_id: str,
    request: Request,
    _key: str = Depends(require_api_key),
    x_admin_password: str | None = Header(default=None),
):
    _admin(request, x_admin_password=x_admin_password)
    ok = request.app.state.pool.delete(account_id)
    if not ok:
        raise HTTPException(status_code=404, detail="not found")
    return {"ok": True}


@router.get("/logs")
async def logs(
    request: Request,
    _key: str = Depends(require_api_key),
    x_admin_password: str | None = Header(default=None),
):
    _admin(request, x_admin_password=x_admin_password)
    return {"logs": list(reversed(request.app.state.request_log[-100:]))}


@router.get("/capture-script")
async def capture_script_meta(request: Request):
    """Public metadata for browser capture helpers (no secrets)."""
    base = str(request.base_url).rstrip("/")
    return {
        "userscript_url": f"{base}/static/capture.user.js",
        "bookmarklet_source_url": f"{base}/static/capture.bookmarklet.js",
        "install": {
            "tampermonkey": "Install Tampermonkey → create script from userscript_url → open m365.cloud.microsoft → chat once → Copy JWT",
            "bookmarklet": "Create bookmark whose URL is the javascript:… one-liner shown in WebUI",
        },
    }


@router.get("/health-detail")
async def health_detail(request: Request, _key: str = Depends(require_api_key)):
    pool = request.app.state.pool
    fabric = request.app.state.fabric
    cfg = request.app.state.config
    return {
        "accounts": pool.list_public(),
        "models": [
            {"id": m.id, "tone": m.tone, "label": m.label} for m in request.app.state.models
        ],
        "config": {
            "host": cfg.gateway.host,
            "port": cfg.gateway.port,
            "pool_strategy": cfg.pool.strategy,
            "prefer_cdp": cfg.token.prefer_cdp,
            "use_sydney_msal": cfg.token.use_sydney_msal,
            "msal_client_id": cfg.token.msal_client_id,
            "oauth_client_id_set": bool(cfg.token.oauth_client_id),
        },
        "token_status": {
            aid: fabric.status_dict(aid, acc.token) for aid, acc in pool.accounts.items()
        },
    }
