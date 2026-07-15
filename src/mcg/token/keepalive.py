from __future__ import annotations

"""Background silent token refresh (no browser).

Requires MSAL cache and/or sidecar refresh_token from a prior PKCE login.
"""

import asyncio
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from mcg.pool.store import AccountPool
    from mcg.token.fabric import TokenFabric

log = logging.getLogger("mcg.keepalive")


class TokenKeepAlive:
    def __init__(
        self,
        fabric: TokenFabric,
        pool: AccountPool,
        *,
        interval_sec: int = 120,
        enabled: bool = True,
    ) -> None:
        self.fabric = fabric
        self.pool = pool
        self.interval_sec = max(30, int(interval_sec))
        self.enabled = enabled
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self.last: dict[str, Any] = {}

    def start(self) -> None:
        if not self.enabled or self._task:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(), name="mcg-token-keepalive")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def tick_once(self) -> dict[str, Any]:
        """Refresh any account near expiry. Safe to call from tests."""
        results: dict[str, Any] = {}
        for acc_id, acc in list(self.pool.accounts.items()):
            if acc.status == "disabled":
                continue
            st = self.fabric.status_dict(acc_id, acc.token or None)
            needs = bool(st.get("needs_refresh")) or not st.get("valid")
            has_msal = bool(st.get("msal_cache") or st.get("msal_sidecar_rt") or st.get("has_refresh_token"))
            if not needs:
                results[acc_id] = {"action": "skip", "ttl": st.get("seconds_remaining")}
                continue
            if not has_msal:
                results[acc_id] = {
                    "action": "no_refresh_material",
                    "ttl": st.get("seconds_remaining"),
                    "hint": "run mcg login (PKCE once) to store refresh",
                }
                continue
            try:
                ost = await self.fabric.refresh_via_sydney_msal(acc_id)
                if not ost.valid and self.fabric.oauth_client_id:
                    ost = await self.fabric.refresh_via_oauth(acc_id)
                if ost.valid:
                    tok = self.fabric.get_hot(acc_id)
                    if tok:
                        self.pool.refresh_token(acc_id, tok)
                    results[acc_id] = {
                        "action": "refreshed",
                        "ttl": ost.seconds_remaining,
                        "source": ost.source,
                    }
                    log.info("keepalive refreshed %s ttl=%s source=%s", acc_id, ost.seconds_remaining, ost.source)
                else:
                    results[acc_id] = {"action": "failed", "error": ost.error}
                    log.warning("keepalive failed %s: %s", acc_id, ost.error)
            except Exception as exc:  # noqa: BLE001
                results[acc_id] = {"action": "error", "error": str(exc)}
                log.exception("keepalive error %s", acc_id)
        self.last = {"accounts": results}
        return self.last

    async def _loop(self) -> None:
        # first pass after short delay so app is ready
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=5)
            return
        except asyncio.TimeoutError:
            pass
        while not self._stop.is_set():
            try:
                await self.tick_once()
            except Exception:  # noqa: BLE001
                log.exception("keepalive tick failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval_sec)
                return
            except asyncio.TimeoutError:
                continue
