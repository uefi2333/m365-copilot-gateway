"""User-facing API error mapping."""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException


def _msg(code: str, message: str, hint: str | None = None, **extra: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "error": {
            "code": code,
            "message": message,
            "type": "mcg_error",
        }
    }
    if hint:
        body["error"]["hint"] = hint
    if extra:
        body["error"].update(extra)
    return body


def http_error(
    status: int,
    code: str,
    message: str,
    *,
    hint: str | None = None,
    **extra: Any,
) -> HTTPException:
    return HTTPException(status_code=status, detail=_msg(code, message, hint, **extra)["error"])


def map_runtime(exc: BaseException) -> HTTPException:
    """Map internal RuntimeError / token failures to actionable responses."""
    text = str(exc).strip() or type(exc).__name__
    low = text.lower()

    if "no active accounts" in low or "valid substrate" in low:
        return http_error(
            503,
            "no_account",
            "没有可用的 M365 账号令牌",
            hint="打开 /ui → 管理登录 → PKCE 登录或粘贴 JWT。需要已开通 M365 Copilot 的企业账号。",
        )
    if "expired" in low or "token" in low and ("refresh" in low or "invalid" in low or "ensure" in low):
        return http_error(
            401,
            "token_invalid",
            "账号令牌无效或已过期",
            hint="在 WebUI 点「刷新」，或重新 PKCE 登录。确认 JWT aud 为 substrate.office.com。",
            raw=text[:200],
        )
    if "cooldown" in low:
        return http_error(
            503,
            "account_cooldown",
            "账号冷却中",
            hint="稍后重试，或在 WebUI 查看账号状态；可调低 pool.cooldown_sec。",
            raw=text[:200],
        )
    return http_error(503, "unavailable", text, hint="检查 /health 与 WebUI 账号池。")


def map_substrate(exc: BaseException) -> HTTPException:
    text = str(exc).strip() or type(exc).__name__
    low = text.lower()
    if "throttl" in low or "rate" in low or "429" in low:
        return http_error(
            429,
            "rate_limited",
            "上游限流",
            hint="放慢请求或增加号池账号。",
            raw=text[:240],
        )
    if "auth" in low or "401" in low or "403" in low or "unauthorized" in low:
        return http_error(
            401,
            "upstream_auth",
            "上游拒绝鉴权",
            hint="令牌可能失效或账号无 Copilot 许可，请重新登录。",
            raw=text[:240],
        )
    if "disengage" in low or "conversation" in low:
        return http_error(
            502,
            "conversation_reset",
            "会话被上游断开",
            hint="网关会重试；若持续失败请换会话或稍后重试。",
            raw=text[:240],
        )
    return http_error(
        502,
        "substrate_error",
        "上游 Substrate 错误",
        hint="查看服务端日志；短暂网络问题可重试。",
        raw=text[:240],
    )


# OpenAI SDK expects detail often as string or {message,type,code}
def openai_style_detail(exc_or_detail: Any) -> Any:
    if isinstance(exc_or_detail, dict) and "message" in exc_or_detail:
        return {
            "message": exc_or_detail.get("message"),
            "type": exc_or_detail.get("type", "mcg_error"),
            "code": exc_or_detail.get("code"),
            "param": None,
            "hint": exc_or_detail.get("hint"),
        }
    return exc_or_detail
