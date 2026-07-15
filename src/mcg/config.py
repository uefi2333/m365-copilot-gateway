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
    # default OFF — no Chrome required
    prefer_cdp: bool = False
    cdp_port: int = 9222
    cdp_timeout_sec: float = 120.0
    browser_binary: str | None = None
    headless: bool = False
    # pure HTTP OAuth (refresh_token / device_code)
    oauth_client_id: str | None = None
    oauth_tenant: str = "common"
    oauth_scope: str = "https://substrate.office.com/ows/.default offline_access openid profile"
    oauth_client_secret: str | None = None


class PoolConfig(BaseModel):
    strategy: Literal["round_robin", "sticky", "least_load"] = "round_robin"
    cooldown_sec: int = 60
    max_consecutive_errors: int = 3


class ToolsConfig(BaseModel):
    max_rounds: int = 8
    repair_rounds: int = 1
    execution: Literal["client", "local"] = "client"
    strategies: list[str] = Field(default_factory=lambda: ["fenced", "shell_route"])


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
    tools: ToolsConfig = Field(default_factory=ToolsConfig)
    models: ModelsConfig = Field(default_factory=ModelsConfig)


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


class EnvSettings(BaseSettings):
    mcg_config: str = "config.yaml"
    mcg_data_dir: str | None = None

    model_config = {"env_prefix": "", "case_sensitive": False}
