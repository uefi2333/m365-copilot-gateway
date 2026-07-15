from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from mcg.config import AppConfig, load_config
from mcg.models_catalog import ModelInfo, list_models
from mcg.pool.store import AccountPool
from mcg.pool.sessions import SessionStore
from mcg.token.fabric import TokenFabric
from mcg.tools.loop import ToolLoop
from mcg.token.keepalive import TokenKeepAlive

from .routes_admin import router as admin_router
from .routes_openai import router as openai_router
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
    extra_models = [
        ModelInfo(id=m.id, tone=m.tone, label=m.label or m.id) for m in cfg.models.advertise
    ]
    models = list_models(extra_models)
    tool_loop = ToolLoop(strategies=cfg.tools.strategies, max_rounds=cfg.tools.max_rounds)

    app = FastAPI(title="M365 Copilot Gateway", version="0.1.0")
    app.state.config = cfg
    app.state.fabric = fabric
    app.state.pool = pool
    app.state.models = models
    app.state.tool_loop = tool_loop
    app.state.sessions = SessionStore()
    app.state.request_log = []  # ring buffer of recent requests

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

    if cfg.gateway.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=cfg.gateway.cors_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    app.include_router(openai_router)
    app.include_router(admin_router)
    app.include_router(ui_router)

    static_dir = Path(__file__).resolve().parent.parent / "webui" / "static"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

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
        }

    return app
