from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel

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
    interactive: bool = True


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


def _admin(request: Request, body_password: str | None = None, x_admin_password: str | None = None) -> None:
    expected = request.app.state.config.gateway.admin_password
    pw = body_password or x_admin_password
    if not verify_admin_password(pw or "", expected):
        raise HTTPException(status_code=401, detail="invalid admin password")


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
        a["has_refresh_token"] = bool(fabric.get_refresh_token(a["id"]))
        rows.append(a)
    return {"accounts": rows}


@router.post("/accounts/import-token")
async def import_token(body: ImportTokenBody, request: Request, _key: str = Depends(require_api_key)):
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


@router.post("/accounts/device-login")
async def device_login(body: DeviceLoginBody, request: Request, _key: str = Depends(require_api_key)):
    """OAuth device code — no local Chrome. User opens verification_uri on any device."""
    _admin(request, body_password=body.admin_password)
    import uuid

    fabric = request.app.state.fabric
    pool = request.app.state.pool
    account_id = body.account_id or f"pending-{uuid.uuid4().hex[:10]}"
    logs: list[str] = []

    def on_status(msg: str) -> None:
        logs.append(msg)

    st = await fabric.login_device_code(account_id, on_status=on_status)
    if not st.valid:
        raise HTTPException(status_code=400, detail={"error": st.error, "logs": logs})
    token = fabric.get_hot(account_id)
    if not token:
        raise HTTPException(status_code=500, detail="token missing")
    from mcg.token.jwtutil import decode_jwt_payload

    claims = decode_jwt_payload(token)
    real_id = str(claims.get("oid") or account_id)
    acc = pool.import_token(token, label=body.label or f"user-{real_id[:8]}")
    rt = fabric.get_refresh_token(account_id)
    if rt:
        fabric.put_refresh_token(acc.id, rt)
    return {
        "ok": True,
        "account": acc.public_dict(),
        "source": st.source,
        "seconds_remaining": st.seconds_remaining,
        "has_refresh_token": bool(fabric.get_refresh_token(acc.id)),
        "logs": logs,
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
async def browser_login(body: BrowserLoginBody, request: Request, _key: str = Depends(require_api_key)):
    """Optional CDP path. Prefer import-token or device-login."""
    _admin(request, body_password=body.admin_password)
    import uuid

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
    token = fabric.get_hot(account_id)
    if not token:
        raise HTTPException(status_code=500, detail="token missing after capture")
    from mcg.token.jwtutil import decode_jwt_payload

    claims = decode_jwt_payload(token)
    real_id = str(claims.get("oid") or account_id)
    acc = pool.import_token(token, label=body.label or f"user-{real_id[:8]}")
    profile = fabric.profile_dir_for(real_id)
    if account_id != real_id:
        src = fabric.profile_dir_for(account_id)
        if src.exists() and not profile.exists():
            try:
                src.rename(profile)
            except OSError:
                pass
        fabric.put_hot(real_id, token)
    pool.bind_profile(acc.id, str(profile))
    return {
        "ok": True,
        "account": acc.public_dict(),
        "source": st.source,
        "seconds_remaining": st.seconds_remaining,
        "logs": logs,
    }


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


@router.get("/health-detail")
async def health_detail(request: Request, _key: str = Depends(require_api_key)):
    pool = request.app.state.pool
    fabric = request.app.state.fabric
    return {
        "accounts": pool.list_public(),
        "models": [
            {"id": m.id, "tone": m.tone, "label": m.label} for m in request.app.state.models
        ],
        "config": {
            "host": request.app.state.config.gateway.host,
            "port": request.app.state.config.gateway.port,
            "pool_strategy": request.app.state.config.pool.strategy,
            "prefer_cdp": request.app.state.config.token.prefer_cdp,
            "oauth_client_id_set": bool(request.app.state.config.token.oauth_client_id),
        },
        "token_status": {
            aid: fabric.status_dict(aid, acc.token) for aid, acc in pool.accounts.items()
        },
    }
