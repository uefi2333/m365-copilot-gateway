from __future__ import annotations

import uuid
from pathlib import Path

from mcg.config import AppConfig, save_config
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

router = APIRouter(tags=["ui"])
templates = Jinja2Templates(
    directory=str(Path(__file__).resolve().parent.parent / "webui" / "templates")
)


def _authed(request: Request) -> bool:
    return request.cookies.get("mcg_admin") == "1"


def _require_ui(request: Request) -> bool:
    return _authed(request)


@router.get("/ui", response_class=HTMLResponse)
async def ui_home(request: Request):
    pool = request.app.state.pool
    fabric = request.app.state.fabric
    accounts = []
    for a in pool.list_public():
        row = dict(a)
        row["detail"] = fabric.status_dict(a["id"])
        accounts.append(row)
    active = sum(1 for a in accounts if a.get("token_valid"))
    cfg = request.app.state.config
    err = request.query_params.get("err")
    ok = request.query_params.get("ok")
    flash_ok = {
        "import": "账号已导入",
        "deleted": "账号已删除",
        "refresh": "令牌已刷新",
        "pkce": "PKCE 登录成功",
        "login": "已登录管理端",
    }
    flash_err = {
        "login": "管理密码错误",
        "auth": "请先登录管理端",
        "import": "导入失败",
        "pkce": "PKCE 交换失败",
        "refresh": "刷新失败",
    }
    # public base for client snippets (best-effort)
    base = str(request.base_url).rstrip("/")
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "accounts": accounts,
            "active": active,
            "models": request.app.state.models,
            "authed": _authed(request),
            "logs": [],
            "msal_client_id": cfg.token.msal_client_id,
            "use_sydney_msal": cfg.token.use_sydney_msal,
            "prefer_cdp": cfg.token.prefer_cdp,
            "err": err,
            "ok": ok,
            "msg": request.query_params.get("msg", ""),
            "ok_text": flash_ok.get(ok or "", ""),
            "err_text": flash_err.get(err or "", ""),
            "base_url": base,
        },
    )


@router.post("/ui/login")
async def ui_login(request: Request, password: str = Form(...)):
    expected = request.app.state.config.gateway.admin_password
    if password != expected:
        return RedirectResponse("/ui?err=login", status_code=303)
    resp = RedirectResponse("/ui", status_code=303)
    resp.set_cookie("mcg_admin", "1", httponly=True, samesite="lax", max_age=86400 * 7)
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
    refresh_token: str = Form(""),
):
    if not _require_ui(request):
        return RedirectResponse("/ui?err=auth", status_code=303)
    try:
        acc = request.app.state.pool.import_token(token.strip(), label=label.strip())
        if refresh_token.strip():
            request.app.state.fabric.put_refresh_token(acc.id, refresh_token.strip())
    except Exception as exc:  # noqa: BLE001
        return RedirectResponse(f"/ui?err=import&msg={exc}", status_code=303)
    return RedirectResponse("/ui?ok=import", status_code=303)


@router.post("/ui/delete-account")
async def ui_delete_account(request: Request, account_id: str = Form(...)):
    if not _require_ui(request):
        return RedirectResponse("/ui?err=auth", status_code=303)
    request.app.state.pool.delete(account_id)
    return RedirectResponse("/ui?ok=deleted", status_code=303)


@router.post("/ui/refresh-account")
async def ui_refresh_account(request: Request, account_id: str = Form(...)):
    if not _require_ui(request):
        return RedirectResponse("/ui?err=auth", status_code=303)
    fabric = request.app.state.fabric
    pool = request.app.state.pool
    if account_id not in pool.accounts:
        return RedirectResponse("/ui?err=missing", status_code=303)
    st = await fabric.refresh_via_sydney_msal(account_id)
    if not st.valid and fabric.oauth_client_id:
        st = await fabric.refresh_via_oauth(account_id)
    if not st.valid:
        return RedirectResponse(f"/ui?err=refresh&msg={st.error or 'failed'}", status_code=303)
    token = fabric.get_hot(account_id)
    if token:
        pool.refresh_token(account_id, token)
    return RedirectResponse("/ui?ok=refresh", status_code=303)


@router.post("/ui/pkce/start")
async def ui_pkce_start(request: Request, label: str = Form("")):
    """JSON: start PKCE — returns auth_url for WebUI modal."""
    if not _require_ui(request):
        return JSONResponse({"ok": False, "error": "auth"}, status_code=401)
    fabric = request.app.state.fabric
    key = f"pending-{uuid.uuid4().hex[:10]}"
    info = await fabric.login_pkce_start(account_key=key)
    return JSONResponse(
        {
            "ok": True,
            "account_key": key,
            "label": label,
            **info,
        }
    )


@router.post("/ui/pkce/finish")
async def ui_pkce_finish(
    request: Request,
    account_key: str = Form(...),
    code_or_url: str = Form(...),
    label: str = Form(""),
):
    if not _require_ui(request):
        return RedirectResponse("/ui?err=auth", status_code=303)
    fabric = request.app.state.fabric
    pool = request.app.state.pool
    st = await fabric.login_pkce_finish(
        code_or_url.strip(),
        account_id=account_key,
        account_key=account_key,
    )
    if not st.valid:
        return RedirectResponse(f"/ui?err=pkce&msg={st.error or 'failed'}", status_code=303)
    token = fabric.get_hot(account_key)
    if not token:
        return RedirectResponse("/ui?err=pkce&msg=no+token", status_code=303)
    from mcg.token.jwtutil import decode_jwt_payload

    claims = decode_jwt_payload(token)
    real_id = str(claims.get("oid") or account_key)
    acc = pool.import_token(token, label=label.strip() or f"user-{real_id[:8]}")
    rt = fabric.get_refresh_token(account_key)
    if rt:
        fabric.put_refresh_token(acc.id, rt)
    data_dir = Path(pool.data_dir)
    for src_name, dst_name in (
        (f"{account_key}.json", f"{acc.id}.json"),
        (f"{account_key}.rt.json", f"{acc.id}.rt.json"),
    ):
        src = data_dir / "msal" / src_name
        dst = data_dir / "msal" / dst_name
        if src.exists() and not dst.exists():
            try:
                dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
            except OSError:
                pass
    return RedirectResponse("/ui?ok=pkce", status_code=303)


@router.post("/ui/browser-login")
async def ui_browser_login(request: Request, label: str = Form("")):
    if not _require_ui(request):
        return RedirectResponse("/ui?err=auth", status_code=303)
    fabric = request.app.state.fabric
    pool = request.app.state.pool
    account_id = f"pending-{uuid.uuid4().hex[:10]}"
    try:
        st = await fabric.capture_via_cdp(account_id, interactive=True)
        if not st.valid:
            return RedirectResponse(f"/ui?err=cdp&msg={st.error or 'cdp'}", status_code=303)
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
    except Exception as exc:  # noqa: BLE001
        return RedirectResponse(f"/ui?err=cdp&msg={exc}", status_code=303)
    return RedirectResponse("/ui?ok=cdp", status_code=303)


# ── Settings (read + write config.yaml) ──────────────────────────────

SETTABLE_SECTIONS = {
    "rate_limit": ["enabled", "requests_per_minute", "burst"],
    "pool": ["strategy", "cooldown_sec", "max_consecutive_errors"],
    "gateway": ["api_keys", "admin_password", "cors_origins"],
}


@router.get("/ui/settings", response_class=HTMLResponse)
async def ui_settings(request: Request):
    if not _require_ui(request):
        return RedirectResponse("/ui?err=auth", status_code=303)
    cfg = request.app.state.config
    authed = _authed(request)
    # Flatten for template
    flattened = {}
    for section, keys in SETTABLE_SECTIONS.items():
        obj = getattr(cfg, section, None)
        if obj is None:
            continue
        for k in keys:
            val = getattr(obj, k, None)
            # convert lists to comma-separated for form display
            if isinstance(val, list):
                val = ", ".join(str(v) for v in val)
            flattened[f"{section}.{k}"] = val
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "authed": authed,
            "config": flattened,
            "sections": list(SETTABLE_SECTIONS.keys()),
            "config_path": request.app.state.config_path,
            "err": request.query_params.get("err"),
            "ok": request.query_params.get("ok"),
        },
    )


@router.post("/ui/settings/save")
async def ui_settings_save(request: Request):
    if not _require_ui(request):
        return RedirectResponse("/ui?err=auth", status_code=303)
    form = await request.form()
    cfg: AppConfig = request.app.state.config

    for section, keys in SETTABLE_SECTIONS.items():
        obj = getattr(cfg, section, None)
        if obj is None:
            continue
        for k in keys:
            key = f"{section}.{k}"
            if key not in form:
                continue
            raw = form[key].strip()
            current = getattr(obj, k, None)
            # Determine type from current value
            if isinstance(current, bool):
                setattr(obj, k, raw.lower() in ("true", "1", "yes"))
            elif isinstance(current, int):
                try:
                    setattr(obj, k, int(raw))
                except (ValueError, TypeError):
                    pass
            elif isinstance(current, list):
                parts = [x.strip() for x in raw.split(",") if x.strip()]
                setattr(obj, k, parts)
            elif isinstance(current, str):
                setattr(obj, k, raw)

    # Also accept api_keys as JSON array if posted separately
    api_keys_raw = form.get("gateway.api_keys_raw", "")
    if api_keys_raw.strip():
        import json as _json
        try:
            parsed = _json.loads(api_keys_raw.strip())
            if isinstance(parsed, list):
                cfg.gateway.api_keys = [str(x) for x in parsed]
        except (_json.JSONDecodeError, TypeError):
            pass

    # Write back
    config_path = request.app.state.config_path
    try:
        from pathlib import Path as _Path
        save_config(cfg, _Path(config_path))
        return RedirectResponse("/ui/settings?ok=saved", status_code=303)
    except Exception as exc:
        return RedirectResponse(f"/ui/settings?err=write&msg={exc}", status_code=303)
