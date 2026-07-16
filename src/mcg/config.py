from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings


class GatewayConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8080
    api_keys: list[str] = Field(default_factory=list)
    admin_password: str = "change-me-admin"
    data_dir: str = "./data"
    cors_origins: list[str] = Field(default_factory=list)


class SubstrateConfig(BaseModel):
    origin: str = "https://m365.cloud.microsoft"
    time_zone: str = "Asia/Shanghai"
    reuse_socket: bool = False
    request_timeout_sec: int = 120
    dump_frames: bool = False


class TokenConfig(BaseModel):
    refresh_skew_sec: int = 300
    # default OFF — no Chrome required for day-to-day
    prefer_cdp: bool = False
    cdp_port: int = 9222
    cdp_timeout_sec: float = 120.0
    browser_binary: str | None = None
    headless: bool = False
    # Mature path: MSAL + Office web Copilot client + Sydney scopes (cramt/lezi)
    use_sydney_msal: bool = True
    msal_client_id: str = "c0ab8ce9-e9a0-42e7-b064-33d422df41f1"
    msal_authority: str = "https://login.microsoftonline.com/common"
    msal_redirect_uri: str = "https://login.microsoftonline.com/common/oauth2/nativeclient"
    # space-separated; empty = default Sydney scopes
    msal_scopes: str = (
        "https://substrate.office.com/sydney/M365Chat.Read "
        "https://substrate.office.com/sydney/sydney.readwrite"
    )
    # MSAL adds offline_access automatically; do not put reserved scopes here
    # background silent refresh (no browser)
    keepalive_enabled: bool = True
    keepalive_interval_sec: int = 120
    # Legacy / experimental custom OAuth (ows etc.) — off by default
    oauth_client_id: str | None = None
    oauth_tenant: str = "common"
    oauth_scope: str = "https://substrate.office.com/ows/.default offline_access openid profile"
    oauth_client_secret: str | None = None


class RateLimitConfig(BaseModel):
    enabled: bool = False
    requests_per_minute: int = 60
    burst: int = 10

class PoolConfig(BaseModel):
    strategy: Literal["round_robin", "sticky", "least_load"] = "round_robin"
    cooldown_sec: int = 60
    max_consecutive_errors: int = 3


class ModelEntry(BaseModel):
    id: str
    tone: str
    label: str = ""


class ModelsConfig(BaseModel):
    advertise: list[ModelEntry] = Field(default_factory=list)


class AppConfig(BaseModel):
    gateway: GatewayConfig = Field(default_factory=GatewayConfig)
    substrate: SubstrateConfig = Field(default_factory=SubstrateConfig)
    token: TokenConfig = Field(default_factory=TokenConfig)
    pool: PoolConfig = Field(default_factory=PoolConfig)
    models: ModelsConfig = Field(default_factory=ModelsConfig)
    rate_limit: RateLimitConfig = Field(default_factory=RateLimitConfig)


def load_config(path: str | Path | None = None) -> AppConfig:
    p = Path(path or "config.yaml")
    if not p.exists():
        example = Path("config.example.yaml")
        if example.exists():
            raw: dict[str, Any] = yaml.safe_load(example.read_text(encoding="utf-8")) or {}
        else:
            return AppConfig()
    else:
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return AppConfig.model_validate(raw)


def save_config(cfg: AppConfig, path: str | Path) -> None:
    """Dump AppConfig back to YAML, preserving section order and comments where possible."""
    raw: dict[str, Any] = {}
    for section in ("gateway", "substrate", "token", "rate_limit", "pool", "models"):
        val = getattr(cfg, section, None)
        if val is None:
            continue
        d = val.model_dump(exclude_none=True, exclude_unset=False)
        # Remove empty defaults for cleaner output
        if section == "gateway" and d.get("api_keys") == []:
            d.pop("api_keys", None)
        if section == "rate_limit" and d.get("enabled") is False:
            pass  # keep default for visibility
        if section == "pool" and d.get("strategy") == "round_robin":
            pass
        raw[section] = d
    p = Path(path)
    p.write_text(yaml.dump(raw, default_flow_style=False, allow_unicode=True, sort_keys=False), encoding="utf-8")


class EnvSettings(BaseSettings):
    mcg_config: str = "config.yaml"
    mcg_data_dir: str | None = None

    model_config = {"env_prefix": "", "case_sensitive": False}
