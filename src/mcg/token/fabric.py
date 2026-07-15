from __future__ import annotations

import asyncio
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
    """Fastest-path token resolution: memory → disk → CDP → manual.

    L0 memory, L1 account record / file, L2 CDP browser capture, L3 interactive import.
    """

    def __init__(
        self,
        data_dir: Path,
        refresh_skew_sec: int = 300,
        *,
        prefer_cdp: bool = True,
        cdp_port: int = 9222,
        cdp_timeout_sec: float = 90.0,
        browser_binary: str | None = None,
        headless: bool = False,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.refresh_skew_sec = refresh_skew_sec
        self.prefer_cdp = prefer_cdp
        self.cdp_port = cdp_port
        self.cdp_timeout_sec = cdp_timeout_sec
        self.browser_binary = browser_binary
        self.headless = headless
        self._hot: dict[str, str] = {}
        self._lock = asyncio.Lock()
        self._refresh_locks: dict[str, asyncio.Lock] = {}

    def profile_dir_for(self, account_id: str) -> Path:
        return self.data_dir / "browser-profiles" / account_id

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
        profile_path: str | None = None,
        on_status: Callable[[str], None] | None = None,
    ) -> str:
        """Return a valid token. Refreshes via CDP when near expiry if enabled."""
        use_cdp = self.prefer_cdp if allow_cdp is None else allow_cdp
        async with self._lock:
            # singleflight per account for refresh
            pass
        async with self._refresh_lock(account_id):
            tok = self.get_hot(account_id) or fallback_token
            if tok:
                st = self.validate(tok)
                if st.valid and st.seconds_remaining > self.refresh_skew_sec:
                    if tok != self._hot.get(account_id):
                        self.put_hot(account_id, tok)
                    return tok
                # still usable but near expiry — try CDP refresh
                if st.valid and not use_cdp:
                    return tok
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
                "run: mcg account browser-login --id <id>   or import-token"
            )

    def status_dict(self, account_id: str, token: str | None = None) -> dict[str, Any]:
        tok = token or self.get_hot(account_id)
        if not tok:
            return {"valid": False, "source": "none", "seconds_remaining": 0}
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
            "profile_dir": str(self.profile_dir_for(account_id)),
        }
