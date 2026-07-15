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
    """Token resolution — mature default = Sydney MSAL (cramt/lezi).

    Order in ensure():
      L0 memory/disk JWT
      L1 Sydney MSAL silent + sidecar RT  (default)
      L1.5 legacy oauth refresh_token     (optional)
      L2 CDP                              (optional prefer_cdp)
      L3 paste / mcg login PKCE
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
        use_sydney_msal: bool = True,
        msal_client_id: str = "c0ab8ce9-e9a0-42e7-b064-33d422df41f1",
        msal_authority: str = "https://login.microsoftonline.com/common",
        msal_redirect_uri: str = "https://login.microsoftonline.com/common/oauth2/nativeclient",
        msal_scopes: str = "",
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
        self.use_sydney_msal = use_sydney_msal
        self.msal_client_id = msal_client_id
        self.msal_authority = msal_authority
        self.msal_redirect_uri = msal_redirect_uri
        self.msal_scopes = [s for s in (msal_scopes or "").split() if s]
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
        # also seed MSAL sidecar so silent path works without separate store
        try:
            self._sydney(account_key=account_id).seed_sidecar_rt(refresh_token)
        except Exception:  # noqa: BLE001
            pass

    def get_refresh_token(self, account_id: str) -> str | None:
        return self._refresh_tokens.get(account_id)

    def _sydney(self, account_key: str = "default"):
        from .sydney_msal import SydneyMsal

        return SydneyMsal(
            self.data_dir,
            client_id=self.msal_client_id,
            authority=self.msal_authority,
            redirect_uri=self.msal_redirect_uri,
            scopes=self.msal_scopes or None,
            account_key=account_key,
        )

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

    async def refresh_via_sydney_msal(
        self,
        account_id: str,
        *,
        on_status: Callable[[str], None] | None = None,
    ) -> TokenStatus:
        """MSAL silent + sidecar RT (mature path)."""
        if not self.use_sydney_msal:
            return TokenStatus(False, "L1-msal", 0, "use_sydney_msal=false")

        def _run() -> TokenStatus:
            from .sydney_msal import SydneyAuthError

            sm = self._sydney(account_key=account_id)
            try:
                if on_status:
                    on_status("Sydney MSAL silent…")
                b = sm.acquire_silent()
                if not b:
                    if on_status:
                        on_status("Sydney sidecar refresh_token…")
                    b = sm.refresh_with_sidecar_rt()
                if not b:
                    return TokenStatus(False, "L1-msal", 0, "no MSAL cache / sidecar RT")
            except SydneyAuthError as exc:
                return TokenStatus(False, "L1-msal", 0, str(exc))
            st = self.put_hot(account_id, b.access_token)
            if not st.valid:
                return TokenStatus(False, "L1-msal", 0, st.error or "aud check failed")
            if b.refresh_token:
                self.put_refresh_token(account_id, b.refresh_token)
            st.source = f"L1-msal:{b.source}"
            return st

        return await asyncio.to_thread(_run)

    async def refresh_via_oauth(
        self,
        account_id: str,
        *,
        refresh_token: str | None = None,
        on_status: Callable[[str], None] | None = None,
    ) -> TokenStatus:
        """Legacy custom oauth_client_id path (usually wrong for ChatHub)."""
        from .oauth import OAuthError, refresh_with_refresh_token

        rt = refresh_token or self.get_refresh_token(account_id)
        if not rt:
            return TokenStatus(False, "L1.5-oauth", 0, "no refresh_token stored")
        if not self.oauth_client_id:
            return TokenStatus(False, "L1.5-oauth", 0, "token.oauth_client_id not set")
        if on_status:
            on_status("legacy OAuth refresh_token…")
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
            return TokenStatus(False, "L1.5-oauth", 0, st.error or "aud check failed")
        if toks.refresh_token:
            self.put_refresh_token(account_id, toks.refresh_token)
        st.source = "L1.5-oauth"
        return st

    async def login_pkce_start(self, account_key: str = "default") -> dict[str, str]:
        sm = self._sydney(account_key=account_key)
        start = await asyncio.to_thread(sm.start_pkce)
        return {
            "auth_url": start.auth_url,
            "state": start.state,
            "client_id": start.client_id,
            "redirect_uri": start.redirect_uri,
            "hint": (
                "Open auth_url, sign in, copy the navigation URL containing "
                "oauth2/nativeclient?code=… (page may show wrongplace — still copy that URL)"
            ),
        }

    async def login_pkce_finish(
        self,
        code_or_url: str,
        account_id: str,
        *,
        account_key: str | None = None,
    ) -> TokenStatus:
        key = account_key or account_id

        def _run() -> TokenStatus:
            from .sydney_msal import SydneyAuthError

            sm = self._sydney(account_key=key)
            try:
                b = sm.exchange_code(code_or_url)
            except SydneyAuthError as exc:
                return TokenStatus(False, "pkce", 0, str(exc))
            st = self.put_hot(account_id, b.access_token)
            if b.refresh_token:
                self.put_refresh_token(account_id, b.refresh_token)
                # keep under key used for msal cache too
                if key != account_id:
                    self.put_refresh_token(key, b.refresh_token)
            if st.valid:
                st.source = f"pkce:{b.source}"
            else:
                st = TokenStatus(False, "pkce", 0, st.error or "aud not substrate")
            return st

        return await asyncio.to_thread(_run)

    def rekey_msal_artifacts(self, from_key: str, to_key: str) -> None:
        """Copy MSAL cache / sidecar RT from pending key to oid account id."""
        if not from_key or not to_key or from_key == to_key:
            return
        msal_dir = self.data_dir / "msal"
        for name in (f"{from_key}.json", f"{from_key}.rt.json"):
            src = msal_dir / name
            if not src.exists():
                continue
            dst = msal_dir / name.replace(from_key, to_key, 1)
            try:
                if not dst.exists():
                    dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
                    try:
                        dst.chmod(0o600)
                    except OSError:
                        pass
            except OSError:
                pass
        rt = self.get_refresh_token(from_key)
        if rt:
            self.put_refresh_token(to_key, rt)

    async def login_device_code(
        self,
        account_id: str,
        *,
        on_status: Callable[[str], None] | None = None,
        use_sydney: bool = True,
    ) -> TokenStatus:
        if use_sydney and self.use_sydney_msal:

            def _run() -> TokenStatus:
                from .sydney_msal import SydneyAuthError

                sm = self._sydney(account_key=account_id)
                try:
                    b = sm.acquire_device_code(on_status=on_status)
                except SydneyAuthError as exc:
                    return TokenStatus(False, "device-code", 0, str(exc))
                st = self.put_hot(account_id, b.access_token)
                if b.refresh_token:
                    self.put_refresh_token(account_id, b.refresh_token)
                if st.valid:
                    st.source = "device-code-sydney"
                else:
                    st = TokenStatus(False, "device-code", 0, st.error or "aud check failed")
                return st

            return await asyncio.to_thread(_run)

        from .oauth import OAuthError, device_code_login

        if not self.oauth_client_id:
            return TokenStatus(False, "device-code", 0, "oauth_client_id not set")
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
            st = TokenStatus(False, "device-code", 0, st.error or "aud check failed")
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
                # near expiry / expired: mature MSAL first
                if allow_oauth and self.use_sydney_msal:
                    ost = await self.refresh_via_sydney_msal(account_id, on_status=on_status)
                    if ost.valid:
                        tok2 = self.get_hot(account_id)
                        if tok2:
                            return tok2
                if allow_oauth and self.oauth_client_id:
                    ost = await self.refresh_via_oauth(account_id, on_status=on_status)
                    if ost.valid:
                        tok2 = self.get_hot(account_id)
                        if tok2:
                            return tok2
                if st.valid and not use_cdp:
                    return tok
            else:
                if allow_oauth and self.use_sydney_msal:
                    ost = await self.refresh_via_sydney_msal(account_id, on_status=on_status)
                    if ost.valid:
                        tok2 = self.get_hot(account_id)
                        if tok2:
                            return tok2
                if allow_oauth and self.oauth_client_id:
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
                "run: mcg login  (PKCE, mature) | mcg import-token - | "
                "or enable prefer_cdp / browser-login"
            )

    def status_dict(self, account_id: str, token: str | None = None) -> dict[str, Any]:
        tok = token or self.get_hot(account_id)
        msal_cache = self.data_dir / "msal" / f"{account_id}.json"
        msal_rt = self.data_dir / "msal" / f"{account_id}.rt.json"
        base = {
            "has_refresh_token": bool(self.get_refresh_token(account_id)),
            "msal_cache": msal_cache.exists(),
            "msal_sidecar_rt": msal_rt.exists(),
            "use_sydney_msal": self.use_sydney_msal,
            "msal_client_id": self.msal_client_id,
            "oauth_configured": bool(self.oauth_client_id),
            "prefer_cdp": self.prefer_cdp,
        }
        if not tok:
            return {"valid": False, "source": "none", "seconds_remaining": 0, **base}
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
            **base,
        }
