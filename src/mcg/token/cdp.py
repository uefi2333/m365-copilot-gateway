from __future__ import annotations



"""CDP / browser token capture for Substrate JWT.

L2 path: attach to Chrome/Edge remote-debugging, open M365 Copilot, snatch
access_token from ChatHub WebSocket URLs or network requests.

Design notes (independent reimplementation; patterns from kuchris / nizar CDP flows):
- Prefer attaching to an already-running debug port (no kill).
- Else launch a dedicated profile under data_dir/browser-profiles/<id>.
- Never require server-side tool registration; this is token plumbing only.
"""


import asyncio
import json
import os
import re
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, unquote, urlparse

import httpx

from .jwtutil import decode_jwt_payload, is_substrate_token, seconds_remaining

TOKEN_IN_URL_RE = re.compile(
    r"access_token=([^&\\s\"']+)",
    re.IGNORECASE,
)
SUBSTRATE_HINTS = (
    "substrate.office.com",
    "m365Copilot/Chathub",
    "m365copilot/chathub",
)

DEFAULT_COPILOT_URLS = (
    "https://m365.cloud.microsoft/chat",
    "https://m365.cloud.microsoft/chat/?auth=2",
    "https://www.office.com/chat",
    "https://copilot.cloud.microsoft/",
)


@dataclass
class CaptureResult:
    ok: bool
    token: str | None = None
    source: str = ""
    error: str | None = None
    oid: str | None = None
    tid: str | None = None
    seconds_remaining: int = 0
    profile_path: str | None = None
    cdp_url: str | None = None


def extract_token_from_text(text: str) -> str | None:
    """Pull substrate JWT from URL / log line / raw text."""
    if not text:
        return None
    # direct JWT-ish blob
    if text.count(".") >= 2 and text.startswith("eyJ") and len(text) > 80:
        try:
            claims = decode_jwt_payload(text.strip())
            if is_substrate_token(claims) and seconds_remaining(claims) > 0:
                return text.strip()
        except Exception:  # noqa: BLE001
            pass
    for m in TOKEN_IN_URL_RE.finditer(text):
        cand = unquote(m.group(1))
        try:
            claims = decode_jwt_payload(cand)
        except Exception:  # noqa: BLE001
            continue
        if is_substrate_token(claims) and seconds_remaining(claims) > 0:
            return cand
    # query parse
    if "access_token=" in text:
        try:
            q = text.split("?", 1)[-1]
            qs = parse_qs(q)
            for key in ("access_token", "Access_Token"):
                if key in qs and qs[key]:
                    cand = unquote(qs[key][0])
                    claims = decode_jwt_payload(cand)
                    if is_substrate_token(claims) and seconds_remaining(claims) > 0:
                        return cand
        except Exception:  # noqa: BLE001
            pass
    return None


def looks_like_substrate_url(url: str) -> bool:
    u = (url or "").lower()
    return any(h.lower() in u for h in SUBSTRATE_HINTS)


def find_browser_binary(prefer: list[str] | None = None) -> str | None:
    candidates = prefer or [
        os.environ.get("MCG_BROWSER"),
        os.environ.get("CHROME_PATH"),
        os.environ.get("EDGE_PATH"),
        "google-chrome",
        "google-chrome-stable",
        "chromium",
        "chromium-browser",
        "microsoft-edge",
        "microsoft-edge-stable",
        "chrome",
        # common absolute paths
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/usr/bin/microsoft-edge",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    ]
    for c in candidates:
        if not c:
            continue
        if Path(c).is_file():
            return c
        found = shutil.which(c)
        if found:
            return found
    return None


async def cdp_version(cdp_http: str, timeout: float = 2.0) -> dict[str, Any] | None:
    base = cdp_http.rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.get(f"{base}/json/version")
            if r.status_code == 200:
                return r.json()
    except Exception:  # noqa: BLE001
        return None
    return None


async def cdp_list_targets(cdp_http: str, timeout: float = 3.0) -> list[dict[str, Any]]:
    base = cdp_http.rstrip("/")
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.get(f"{base}/json/list")
        r.raise_for_status()
        data = r.json()
        return data if isinstance(data, list) else []


class BrowserLauncher:
    """Launch dedicated Chromium with remote debugging + isolated user-data-dir."""

    def __init__(
        self,
        profile_dir: Path,
        *,
        port: int = 9222,
        binary: str | None = None,
        headless: bool = False,
    ) -> None:
        self.profile_dir = Path(profile_dir)
        self.port = port
        self.binary = binary or find_browser_binary()
        self.headless = headless
        self.proc: subprocess.Popen[bytes] | None = None

    @property
    def cdp_http(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    async def ensure_running(self) -> str:
        ver = await cdp_version(self.cdp_http)
        if ver:
            return self.cdp_http
        if not self.binary:
            raise RuntimeError(
                "no Chrome/Edge binary found; set MCG_BROWSER or install chromium"
            )
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        args = [
            self.binary,
            f"--remote-debugging-port={self.port}",
            f"--user-data-dir={self.profile_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-popup-blocking",
            "--disable-background-timer-throttling",
            # do NOT use user's daily profile — isolated dir only
        ]
        if self.headless:
            args.append("--headless=new")
            args.append("--disable-gpu")
        # open blank; capture will navigate
        args.append("about:blank")
        self.proc = subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        # wait for CDP
        deadline = time.time() + 25
        while time.time() < deadline:
            ver = await cdp_version(self.cdp_http)
            if ver:
                return self.cdp_http
            if self.proc.poll() is not None:
                raise RuntimeError(f"browser exited early code={self.proc.returncode}")
            await asyncio.sleep(0.25)
        raise RuntimeError(f"CDP not ready on port {self.port}")

    def stop(self) -> None:
        if not self.proc or self.proc.poll() is not None:
            return
        try:
            os.killpg(self.proc.pid, signal.SIGTERM)
        except Exception:  # noqa: BLE001
            try:
                self.proc.terminate()
            except Exception:  # noqa: BLE001
                pass


class CdpSession:
    """Minimal CDP client over the browser Target WebSocket."""

    def __init__(self, ws_url: str) -> None:
        self.ws_url = ws_url
        self._id = 0
        self._ws: Any = None
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._event_handlers: list[Callable[[str, dict[str, Any]], None]] = []
        self._reader_task: asyncio.Task[None] | None = None

    def on_event(self, handler: Callable[[str, dict[str, Any]], None]) -> None:
        self._event_handlers.append(handler)

    async def connect(self) -> None:
        import websockets

        self._ws = await websockets.connect(
            self.ws_url,
            max_size=50 * 1024 * 1024,
            open_timeout=15,
        )
        self._reader_task = asyncio.create_task(self._read_loop())

    async def close(self) -> None:
        if self._reader_task:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except Exception:  # noqa: BLE001
                pass
        if self._ws is not None:
            await self._ws.close()
            self._ws = None

    async def call(self, method: str, params: dict[str, Any] | None = None, timeout: float = 20.0) -> Any:
        self._id += 1
        msg_id = self._id
        fut: asyncio.Future[Any] = asyncio.get_event_loop().create_future()
        self._pending[msg_id] = fut
        payload = {"id": msg_id, "method": method, "params": params or {}}
        await self._ws.send(json.dumps(payload))
        return await asyncio.wait_for(fut, timeout=timeout)

    async def _read_loop(self) -> None:
        assert self._ws is not None
        try:
            async for raw in self._ws:
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8", errors="ignore")
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if "id" in msg and msg["id"] in self._pending:
                    fut = self._pending.pop(msg["id"])
                    if "error" in msg:
                        fut.set_exception(RuntimeError(str(msg["error"])))
                    else:
                        fut.set_result(msg.get("result"))
                elif "method" in msg:
                    method = msg["method"]
                    params = msg.get("params") or {}
                    for h in list(self._event_handlers):
                        try:
                            h(method, params)
                        except Exception:  # noqa: BLE001
                            pass
        except asyncio.CancelledError:
            return
        except Exception:  # noqa: BLE001
            return


async def _pick_page_ws(cdp_http: str) -> str:
    targets = await cdp_list_targets(cdp_http)
    pages = [t for t in targets if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]
    # prefer non-devtools
    for t in pages:
        url = t.get("url") or ""
        if not url.startswith("devtools://"):
            return t["webSocketDebuggerUrl"]
    if pages:
        return pages[0]["webSocketDebuggerUrl"]
    # create target
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.put(f"{cdp_http.rstrip('/')}/json/new?about:blank")
        r.raise_for_status()
        data = r.json()
        ws = data.get("webSocketDebuggerUrl")
        if not ws:
            raise RuntimeError("failed to create CDP page target")
        return ws


async def capture_substrate_token(
    *,
    cdp_http: str | None = None,
    profile_dir: Path | None = None,
    port: int = 9222,
    binary: str | None = None,
    headless: bool = False,
    navigate_urls: list[str] | None = None,
    timeout_sec: float = 90.0,
    interactive_wait: bool = True,
    launch_if_needed: bool = True,
    on_status: Callable[[str], None] | None = None,
) -> CaptureResult:
    """Capture a live substrate JWT via CDP network/WS events.

    Flow:
    1. Attach or launch browser
    2. Enable Network + Page
    3. Navigate to M365 chat
    4. Watch request/WS URLs for access_token=
    5. Validate aud + exp
    """
    def status(msg: str) -> None:
        if on_status:
            on_status(msg)

    launcher: BrowserLauncher | None = None
    cdp = (cdp_http or f"http://127.0.0.1:{port}").rstrip("/")

    ver = await cdp_version(cdp)
    if not ver and launch_if_needed:
        if profile_dir is None:
            return CaptureResult(False, error="profile_dir required to launch browser")
        status(f"launching browser profile={profile_dir} port={port}")
        launcher = BrowserLauncher(profile_dir, port=port, binary=binary, headless=headless)
        try:
            cdp = await launcher.ensure_running()
        except Exception as exc:  # noqa: BLE001
            return CaptureResult(False, error=str(exc), profile_path=str(profile_dir))
    elif not ver:
        return CaptureResult(False, error=f"CDP not reachable at {cdp}")

    status(f"CDP ready {cdp}")
    found: dict[str, str] = {"token": ""}

    def consider(url_or_text: str, source: str) -> None:
        if found["token"]:
            return
        if url_or_text and (looks_like_substrate_url(url_or_text) or "access_token=" in url_or_text):
            tok = extract_token_from_text(url_or_text)
            if tok:
                found["token"] = tok
                found["source"] = source
                status(f"token captured via {source}")

    session: CdpSession | None = None
    try:
        ws_url = await _pick_page_ws(cdp)
        session = CdpSession(ws_url)
        await session.connect()

        def on_event(method: str, params: dict[str, Any]) -> None:
            if method == "Network.requestWillBeSent":
                req = params.get("request") or {}
                consider(str(req.get("url") or ""), "Network.requestWillBeSent")
            elif method == "Network.webSocketCreated":
                consider(str(params.get("url") or ""), "Network.webSocketCreated")
            elif method == "Network.webSocketWillSendHandshakeRequest":
                req = params.get("request") or {}
                consider(str(req.get("url") or ""), "Network.webSocketHandshake")
                headers = req.get("headers") or {}
                if isinstance(headers, dict):
                    for v in headers.values():
                        consider(str(v), "Network.wsHeaders")
            elif method == "Network.responseReceived":
                resp = params.get("response") or {}
                consider(str(resp.get("url") or ""), "Network.responseReceived")
            elif method == "Page.frameNavigated":
                frame = params.get("frame") or {}
                consider(str(frame.get("url") or ""), "Page.frameNavigated")

        session.on_event(on_event)
        await session.call("Network.enable", {"maxPostDataSize": 65536})
        await session.call("Page.enable")
        # extra: Runtime for later eval hooks
        try:
            await session.call("Runtime.enable")
        except Exception:  # noqa: BLE001
            pass

        urls = navigate_urls or list(DEFAULT_COPILOT_URLS)
        deadline = time.time() + timeout_sec
        for i, url in enumerate(urls):
            if found["token"] or time.time() >= deadline:
                break
            status(f"navigate ({i+1}/{len(urls)}): {url}")
            try:
                await session.call("Page.navigate", {"url": url})
            except Exception as exc:  # noqa: BLE001
                status(f"navigate error: {exc}")
            # wait a bit for WS
            slice_deadline = min(deadline, time.time() + (25 if interactive_wait else 12))
            while time.time() < slice_deadline and not found["token"]:
                await asyncio.sleep(0.2)
            if found["token"]:
                break
            # try scrape performance / document URL
            try:
                res = await session.call(
                    "Runtime.evaluate",
                    {
                        "expression": "location.href",
                        "returnByValue": True,
                    },
                )
                consider(str((res or {}).get("result", {}).get("value") or ""), "location.href")
            except Exception:  # noqa: BLE001
                pass

        # final passive wait if interactive (user may still be logging in)
        if not found["token"] and interactive_wait:
            status("waiting for login / ChatHub WS (complete MFA in the browser window)…")
            while time.time() < deadline and not found["token"]:
                await asyncio.sleep(0.3)

        token = found.get("token") or None
        if not token:
            return CaptureResult(
                False,
                error="timeout: no substrate access_token seen on CDP network. "
                "Log into M365 Copilot in the opened browser, open Chat, retry.",
                profile_path=str(profile_dir) if profile_dir else None,
                cdp_url=cdp,
            )
        claims = decode_jwt_payload(token)
        return CaptureResult(
            True,
            token=token,
            source=found.get("source") or "cdp",
            oid=str(claims.get("oid") or ""),
            tid=str(claims.get("tid") or ""),
            seconds_remaining=seconds_remaining(claims),
            profile_path=str(profile_dir) if profile_dir else None,
            cdp_url=cdp,
        )
    except Exception as exc:  # noqa: BLE001
        return CaptureResult(
            False,
            error=str(exc),
            profile_path=str(profile_dir) if profile_dir else None,
            cdp_url=cdp,
        )
    finally:
        if session is not None:
            await session.close()
        # keep browser alive for session reuse (do not stop launcher)
