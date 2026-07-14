from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field

from mcg.auth.deps import require_api_key, verify_admin_password

router = APIRouter(prefix="/admin", tags=["admin"])


class ImportTokenBody(BaseModel):
    token: str
    label: str = ""
    admin_password: str


class StatusBody(BaseModel):
    status: str
    admin_password: str


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
    return {"accounts": request.app.state.pool.list_public()}


@router.post("/accounts/import-token")
async def import_token(body: ImportTokenBody, request: Request, _key: str = Depends(require_api_key)):
    _admin(request, body_password=body.admin_password)
    try:
        acc = request.app.state.pool.import_token(body.token.strip(), label=body.label)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "account": acc.public_dict()}


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
    return {
        "accounts": pool.list_public(),
        "models": [
            {"id": m.id, "tone": m.tone, "label": m.label} for m in request.app.state.models
        ],
        "config": {
            "host": request.app.state.config.gateway.host,
            "port": request.app.state.config.gateway.port,
            "pool_strategy": request.app.state.config.pool.strategy,
        },
    }
