from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pathlib import Path

router = APIRouter(tags=["ui"])
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "webui" / "templates"))


def _authed(request: Request) -> bool:
    return request.cookies.get("mcg_admin") == "1"


@router.get("/ui", response_class=HTMLResponse)
async def ui_home(request: Request):
    pool = request.app.state.pool
    accounts = pool.list_public()
    active = sum(1 for a in accounts if a.get("token_valid"))
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "accounts": accounts,
            "active": active,
            "models": request.app.state.models,
            "authed": _authed(request),
            "logs": list(reversed(request.app.state.request_log[-20:])),
        },
    )


@router.post("/ui/login")
async def ui_login(request: Request, password: str = Form(...)):
    expected = request.app.state.config.gateway.admin_password
    if password != expected:
        return RedirectResponse("/ui?err=1", status_code=303)
    resp = RedirectResponse("/ui", status_code=303)
    resp.set_cookie("mcg_admin", "1", httponly=True, samesite="lax")
    return resp


@router.post("/ui/logout")
async def ui_logout():
    resp = RedirectResponse("/ui", status_code=303)
    resp.delete_cookie("mcg_admin")
    return resp


@router.post("/ui/import-token")
async def ui_import_token(
    request: Request,
    token: str = Form(...),
    label: str = Form(""),
):
    if not _authed(request):
        return RedirectResponse("/ui?err=auth", status_code=303)
    try:
        request.app.state.pool.import_token(token.strip(), label=label.strip())
    except Exception:
        return RedirectResponse("/ui?err=import", status_code=303)
    return RedirectResponse("/ui?ok=import", status_code=303)


@router.post("/ui/delete-account")
async def ui_delete_account(request: Request, account_id: str = Form(...)):
    if not _authed(request):
        return RedirectResponse("/ui?err=auth", status_code=303)
    request.app.state.pool.delete(account_id)
    return RedirectResponse("/ui", status_code=303)


@router.post("/ui/browser-login")
async def ui_browser_login(request: Request, label: str = Form("")):
    if not _authed(request):
        return RedirectResponse("/ui?err=auth", status_code=303)
    import uuid
    fabric = request.app.state.fabric
    pool = request.app.state.pool
    account_id = f"pending-{uuid.uuid4().hex[:10]}"
    try:
        st = await fabric.capture_via_cdp(account_id, interactive=True)
        if not st.valid:
            return RedirectResponse("/ui?err=cdp", status_code=303)
        token = fabric.get_hot(account_id)
        if not token:
            return RedirectResponse("/ui?err=cdp", status_code=303)
        from mcg.token.jwtutil import decode_jwt_payload
        claims = decode_jwt_payload(token)
        real_id = str(claims.get("oid") or account_id)
        acc = pool.import_token(token, label=label.strip() or f"user-{real_id[:8]}")
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
    except Exception:
        return RedirectResponse("/ui?err=cdp", status_code=303)
    return RedirectResponse("/ui?ok=cdp", status_code=303)
