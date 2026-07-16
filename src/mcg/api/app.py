from __future__ import annotations

import time
from pathlib import Path

from fastapi import FastAPI, Depends
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

from mcg.auth.deps import require_api_key
from mcg.config import AppConfig, load_config
from mcg.models_catalog import ModelInfo, list_models
from mcg.pool.store import AccountPool
from mcg.token.fabric import TokenFabric
from mcg.token.keepalive import TokenKeepAlive

from .routes_admin import router as admin_router
from .routes_chat import router as chat_router
from .routes_ui import router as ui_router


def create_app(config_path: str | Path | None = None, config: AppConfig | None = None) -> FastAPI:
    cfg = config or load_config(config_path)
    data_dir = Path(cfg.gateway.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)

    fabric = TokenFabric(
        data_dir,
        refresh_skew_sec=cfg.token.refresh_skew_sec,
        prefer_cdp=cfg.token.prefer_cdp,
        cdp_port=cfg.token.cdp_port,
        cdp_timeout_sec=cfg.token.cdp_timeout_sec,
        browser_binary=cfg.token.browser_binary,
        headless=cfg.token.headless,
        use_sydney_msal=cfg.token.use_sydney_msal,
        msal_client_id=cfg.token.msal_client_id,
        msal_authority=cfg.token.msal_authority,
        msal_redirect_uri=cfg.token.msal_redirect_uri,
        msal_scopes=cfg.token.msal_scopes,
        oauth_client_id=cfg.token.oauth_client_id,
        oauth_tenant=cfg.token.oauth_tenant,
        oauth_scope=cfg.token.oauth_scope,
        oauth_client_secret=cfg.token.oauth_client_secret,
    )
    pool = AccountPool(
        data_dir,
        fabric,
        strategy=cfg.pool.strategy,
        cooldown_sec=cfg.pool.cooldown_sec,
        max_consecutive_errors=cfg.pool.max_consecutive_errors,
    )
    models = list_models([
        ModelInfo(id=m.id, tone=m.tone, label=m.label or m.id) for m in cfg.models.advertise
    ])

    app = FastAPI(title="M365 Copilot Pool Core", version="0.1.0")
    app.state.config = cfg
    app.state.config_path = str(config_path) if config_path else str(Path("config.yaml").resolve())
    app.state.fabric = fabric
    app.state.pool = pool
    app.state.models = models
    app.state.request_log = []
    from .session_pool import SessionPool
    app.state.chat_sessions = SessionPool()

    keepalive = TokenKeepAlive(
        fabric,
        pool,
        interval_sec=getattr(cfg.token, "keepalive_interval_sec", 120),
        enabled=getattr(cfg.token, "keepalive_enabled", True),
    )
    app.state.keepalive = keepalive

    @app.on_event("startup")
    async def _startup_keepalive() -> None:
        keepalive.start()

    @app.on_event("shutdown")
    async def _shutdown_keepalive() -> None:
        await keepalive.stop()

    if cfg.rate_limit.enabled:
        from mcg.api.rate_limit import RateLimitMiddleware
        app.add_middleware(RateLimitMiddleware, cfg=cfg.rate_limit)

    if cfg.gateway.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=cfg.gateway.cors_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    app.include_router(admin_router)
    app.include_router(chat_router)
    app.include_router(ui_router)

    static_dir = Path(__file__).resolve().parent.parent / "webui" / "static"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    @app.get("/", include_in_schema=False)
    async def root():
        return RedirectResponse(url="/ui")

    @app.get("/health")
    async def health():
        accounts = pool.list_public()
        active = sum(1 for a in accounts if a.get("token_valid") and a.get("status") == "active")
        return {
            "ok": True,
            "version": "0.1.0",
            "accounts_total": len(accounts),
            "accounts_active": active,
            "models": len(models),
            "keepalive": {
                "enabled": keepalive.enabled,
                "interval_sec": keepalive.interval_sec,
                "last": keepalive.last,
            },
            "ts": int(time.time()),
        }

    @app.get("/v1/models")
    async def v1_models(_key: str = Depends(require_api_key)):
        return {
            "object": "list",
            "data": [
                {
                    "id": m.id,
                    "object": "model",
                    "created": 0,
                    "owned_by": "m365-copilot",
                    "metadata": {
                        "tone": m.tone,
                        "label": m.label,
                        "family": m.family,
                    },
                }
                for m in app.state.models
            ],
        }

    @app.get("/models")
    async def models_plain(_key: str = Depends(require_api_key)):
        return {"models": [m.__dict__ for m in app.state.models]}

    return app
