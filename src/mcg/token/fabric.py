from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .jwtutil import decode_jwt_payload, is_substrate_token, seconds_remaining


@dataclass
class TokenStatus:
    valid: bool
    source: str
    seconds_remaining: int
    error: str | None = None
    oid: str | None = None
    tid: str | None = None


class TokenFabric:
    """Token resolution without Chrome by default.

    L0 memory → L1 disk JWT → L1.5 OAuth refresh_token (HTTP) → L2 CDP optional → L3 paste/device-code
    """

    def __init__(
        self,
        data_dir: Path,
        refresh_skew_sec: int = 300,
        *,
        prefer_cdp: bool = False,
        cdp_port: int = 9222,
        cdp_timeout_sec: float = 90.0,
        browser_binary: str | None = None,
        headless: bool = False,
        oauth_client_id: str | None = None,
        oauth_tenant: str = "common",
        oauth_scope: str = "https://substrate.office.com/ows/.default offline_access openid profile",
        oauth_client_secret: str | None = None,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.refresh_skew_sec = refresh_skew_sec
        self.prefer_cdp = prefer_cdp
        self.cdp_port = cdp_port
        self.cdp_timeout_sec = cdp_timeout_sec
        self.browser_binary = browser_binary
        self.headless = headless
        self.oauth_client_id = oauth_client_id
        self.oauth_tenant = oauth_tenant
        self.oauth_scope = oauth_scope
        self.oauth_client_secret = oauth_client_secret
        self._hot: dict[str, str] = {}
        self._refresh_tokens: dict[str, str] = {}
        self._lock = asyncio.Lock()
        self._refresh_locks: dict[str, asyncio.Lock] = {}
        self._load_refresh_store()

    def profile_dir_for(self, account_id: str) -> Path:
        return self.data_dir / "browser-profiles" / account_id

    def _refresh_store_path(self) -> Path:
        return self.data_dir / "refresh_tokens.json"

    def _load_refresh_store(self) -> None:
        p = self._refresh_store_path()
        if not p.exists():
            return
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                self._refresh_tokens = {str(k): str(v) for k, v in data.items() if v}
        except Exception:  # noqa: BLE001
            self._refresh_tokens = {}

    def _save_refresh_store(self) -> None:
        p = self._refresh_store_path()
        p.write_text(json.dumps(self._refresh_tokens, indent=2), encoding="utf-8")
        try:
            p.chmod(0o600)
        except OSError:
            pass

    def put_refresh_token(self, account_id: str, refresh_token: str) -> None:
        if not refresh_token:
            return
        self._refresh_tokens[account_id] = refresh_token
        self._save_refresh_store()

    def get_refresh_token(self, account_id: str) -> str | None:
        return self._refresh_tokens.get(account_id)

    def validate(self, token: str) -> TokenStatus:
        try:
            claims = decode_jwt_payload(token)
        except Exception as exc:  # noqa: BLE001
            return TokenStatus(False, "invalid", 0, str(exc))
        if not is_substrate_token(claims):
            return TokenStatus(False, "invalid", 0, "aud is not substrate.office.com")
        rem = seconds_remaining(claims)
        if rem <= 0:
            return TokenStatus(
                False, "expired", 0, "token expired", claims.get("oid"), claims.get("tid")
            )
        return TokenStatus(True, "ok", rem, None, str(claims.get("oid")), str(claims.get("tid")))

    def put_hot(self, account_id: str, token: str) -> TokenStatus:
        st = self.validate(token)
        if not st.valid:
            return st
        self._hot[account_id] = token
        path = self.data_dir / "tokens" / f"{account_id}.jwt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(token, encoding="utf-8")
        try:
            path.chmod(0o600)
        except OSError:
            pass
        return st

    def get_hot(self, account_id: str) -> str | None:
        if account_id in self._hot:
            st = self.validate(self._hot[account_id])
            if st.valid and st.seconds_remaining > max(30, self.refresh_skew_sec // 10):
                return self._hot[account_id]
        path = self.data_dir / "tokens" / f"{account_id}.jwt"
        if path.exists():
            token = path.read_text(encoding="utf-8").strip()
            st = self.validate(token)
            if st.valid:
                self._hot[account_id] = token
                return token
        return None

    def _refresh_lock(self, account_id: str) -> asyncio.Lock:
        if account_id not in self._refresh_locks:
            self._refresh_locks[account_id] = asyncio.Lock()
        return self._refresh_locks[account_id]

    async def refresh_via_oauth(
        self,
        account_id: str,
        *,
        refresh_token: str | None = None,
        on_status: Callable[[str], None] | None = None,
    ) -> TokenStatus:
        """HTTP-only silent renew using stored/offline refresh_token."""
        from .oauth import OAuthError, refresh_with_refresh_token

        rt = refresh_token or self.get_refresh_token(account_id)
        if not rt:
            return TokenStatus(False, "L1.5-oauth", 0, "no refresh_token stored")
        if not self.oauth_client_id:
            return TokenStatus(
                False,
                "L1.5-oauth",
                0,
                "token.oauth_client_id not set (required for refresh_token grant)",
            )
        if on_status:
            on_status("refreshing access_token via OAuth refresh_token (HTTP)…")
        try:
            toks = await refresh_with_refresh_token(
                refresh_token=rt,
                client_id=self.oauth_client_id,
                tenant=self.oauth_tenant,
                scope=self.oauth_scope,
                client_secret=self.oauth_client_secret,
            )
        except OAuthError as exc:
            return TokenStatus(False, "L1.5-oauth", 0, str(exc))
        st = self.put_hot(account_id, toks.access_token)
        if not st.valid:
            # access token may not be substrate aud if scope wrong
            return TokenStatus(
                False,
                "L1.5-oauth",
                0,
                st.error
                or "refreshed token failed substrate aud check — fix oauth_scope / client",
            )
        if toks.refresh_token:
            self.put_refresh_token(account_id, toks.refresh_token)
        st.source = "L1.5-oauth"
        return st

    async def login_device_code(
        self,
        account_id: str,
        *,
        on_status: Callable[[str], None] | None = None,
    ) -> TokenStatus:
        """Device code: user opens microsoft.com/devicelogin on any device; no Chrome here."""
        from .oauth import OAuthError, device_code_login

        if not self.oauth_client_id:
            return TokenStatus(False, "device-code", 0, "token.oauth_client_id not set")
        try:
            toks = await device_code_login(
                client_id=self.oauth_client_id,
                tenant=self.oauth_tenant,
                scope=self.oauth_scope,
                client_secret=self.oauth_client_secret,
                on_status=on_status,
            )
        except OAuthError as exc:
            return TokenStatus(False, "device-code", 0, str(exc))
        st = self.put_hot(account_id, toks.access_token)
        if toks.refresh_token:
            self.put_refresh_token(account_id, toks.refresh_token)
        if st.valid:
            st.source = "device-code"
        else:
            st = TokenStatus(
                False,
                "device-code",
                0,
                st.error
                or "token aud is not substrate — app registration/API permissions must include substrate scope",
            )
        return st

    async def capture_via_cdp(
        self,
        account_id: str,
        *,
        cdp_http: str | None = None,
        interactive: bool = True,
        on_status: Callable[[str], None] | None = None,
        profile_path: str | None = None,
    ) -> TokenStatus:
        """Optional heavy path — only if prefer_cdp / explicit CLI."""
        from .cdp import capture_substrate_token

        profile = Path(profile_path) if profile_path else self.profile_dir_for(account_id)
        result = await capture_substrate_token(
            cdp_http=cdp_http,
            profile_dir=profile,
            port=self.cdp_port,
            binary=self.browser_binary,
            headless=self.headless and not interactive,
            timeout_sec=self.cdp_timeout_sec,
            interactive_wait=interactive,
            launch_if_needed=True,
            on_status=on_status,
        )
        if not result.ok or not result.token:
            return TokenStatus(False, "L2-cdp", 0, result.error or "cdp capture failed")
        st = self.put_hot(account_id, result.token)
        st.source = f"L2-cdp:{result.source}"
        return st

    async def ensure(
        self,
        account_id: str,
        fallback_token: str | None = None,
        *,
        allow_cdp: bool | None = None,
        allow_oauth: bool = True,
        profile_path: str | None = None,
        on_status: Callable[[str], None] | None = None,
    ) -> str:
        use_cdp = self.prefer_cdp if allow_cdp is None else allow_cdp
        async with self._refresh_lock(account_id):
            tok = self.get_hot(account_id) or fallback_token
            if tok:
                st = self.validate(tok)
                if st.valid and st.seconds_remaining > self.refresh_skew_sec:
                    if tok != self._hot.get(account_id):
                        self.put_hot(account_id, tok)
                    return tok
                # near expiry: try OAuth first (no browser)
                if allow_oauth and (not st.valid or st.seconds_remaining <= self.refresh_skew_sec):
                    ost = await self.refresh_via_oauth(account_id, on_status=on_status)
                    if ost.valid:
                        tok2 = self.get_hot(account_id)
                        if tok2:
                            return tok2
                if st.valid and not use_cdp:
                    return tok
            elif allow_oauth:
                ost = await self.refresh_via_oauth(account_id, on_status=on_status)
                if ost.valid:
                    tok2 = self.get_hot(account_id)
                    if tok2:
                        return tok2

            if use_cdp:
                st = await self.capture_via_cdp(
                    account_id,
                    interactive=False,
                    on_status=on_status,
                    profile_path=profile_path,
                )
                if st.valid:
                    tok2 = self.get_hot(account_id)
                    if tok2:
                        return tok2
            if fallback_token:
                st = self.put_hot(account_id, fallback_token)
                if st.valid and st.seconds_remaining > 0:
                    return fallback_token
            raise RuntimeError(
                f"no valid substrate token for account {account_id}; "
                "import JWT, or store refresh_token + oauth_client_id, "
                "or run: mcg device-login / mcg import-token"
            )

    def status_dict(self, account_id: str, token: str | None = None) -> dict[str, Any]:
        tok = token or self.get_hot(account_id)
        if not tok:
            return {
                "valid": False,
                "source": "none",
                "seconds_remaining": 0,
                "has_refresh_token": bool(self.get_refresh_token(account_id)),
            }
        st = self.validate(tok)
        return {
            "valid": st.valid,
            "source": st.source,
            "seconds_remaining": st.seconds_remaining,
            "error": st.error,
            "oid": st.oid,
            "tid": st.tid,
            "checked_at": int(time.time()),
            "needs_refresh": st.valid and st.seconds_remaining <= self.refresh_skew_sec,
            "has_refresh_token": bool(self.get_refresh_token(account_id)),
            "oauth_configured": bool(self.oauth_client_id),
            "prefer_cdp": self.prefer_cdp,
        }
