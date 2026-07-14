from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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
    """Fastest-path token resolution: memory → disk → (future CDP) → manual.

    L0 memory, L1 account record / file, L2 CDP (optional), L3 interactive import.
    """

    def __init__(self, data_dir: Path, refresh_skew_sec: int = 300) -> None:
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.refresh_skew_sec = refresh_skew_sec
        self._hot: dict[str, str] = {}
        self._lock = asyncio.Lock()

    def validate(self, token: str) -> TokenStatus:
        try:
            claims = decode_jwt_payload(token)
        except Exception as exc:  # noqa: BLE001
            return TokenStatus(False, "invalid", 0, str(exc))
        if not is_substrate_token(claims):
            return TokenStatus(False, "invalid", 0, "aud is not substrate.office.com")
        rem = seconds_remaining(claims)
        if rem <= 0:
            return TokenStatus(False, "expired", 0, "token expired", claims.get("oid"), claims.get("tid"))
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
            if st.valid and st.seconds_remaining > self.refresh_skew_sec // 10:
                return self._hot[account_id]
        path = self.data_dir / "tokens" / f"{account_id}.jwt"
        if path.exists():
            token = path.read_text(encoding="utf-8").strip()
            st = self.validate(token)
            if st.valid:
                self._hot[account_id] = token
                return token
        return None

    async def ensure(self, account_id: str, fallback_token: str | None = None) -> str:
        async with self._lock:
            tok = self.get_hot(account_id)
            if tok:
                st = self.validate(tok)
                if st.seconds_remaining > self.refresh_skew_sec:
                    return tok
            if fallback_token:
                st = self.put_hot(account_id, fallback_token)
                if st.valid:
                    return fallback_token
            # CDP refresh hook (optional) — implemented in token/cdp.py later
            raise RuntimeError(
                f"no valid substrate token for account {account_id}; import via WebUI or mcg account import-token"
            )

    def status_dict(self, account_id: str, token: str | None = None) -> dict[str, Any]:
        tok = token or self.get_hot(account_id)
        if not tok:
            return {"valid": False, "source": "none", "seconds_remaining": 0}
        st = self.validate(tok)
        return {
            "valid": st.valid,
            "source": "L0/L1",
            "seconds_remaining": st.seconds_remaining,
            "error": st.error,
            "oid": st.oid,
            "tid": st.tid,
            "checked_at": int(time.time()),
        }
