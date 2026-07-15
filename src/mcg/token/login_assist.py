from __future__ import annotations

"""Lightweight PKCE assist: open browser + local paste page (no Chrome CDP).

User signs in normally; when AAD lands on nativeclient?code=… (or wrongplace),
paste that URL into the local page. Server finishes exchange + imports account.
"""

import asyncio
import html
import json
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable


ASSIST_HTML = """<!doctype html>
<html lang="zh-CN"><head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>MCG PKCE Assist</title>
<style>
body{font-family:system-ui,sans-serif;max-width:720px;margin:2rem auto;padding:0 1rem;
 background:#0f1419;color:#e7ecf1}
a,button{color:#7dd3fc}
textarea{width:100%;min-height:120px;background:#1a2332;color:#e7ecf1;border:1px solid #334;
 border-radius:8px;padding:.75rem;font-family:ui-monospace,monospace}
button{background:#2563eb;border:0;border-radius:8px;padding:.6rem 1rem;color:#fff;cursor:pointer}
.card{background:#151c27;border:1px solid #2a3544;border-radius:12px;padding:1rem;margin:1rem 0}
.ok{color:#4ade80}.err{color:#f87171}.muted{color:#94a3b8;font-size:.9rem}
code{background:#1a2332;padding:.1rem .3rem;border-radius:4px}
</style></head><body>
<h1>M365 Copilot Gateway — 登录助手</h1>
<div class="card">
<p>1. 已尝试打开登录页。若未打开，点：
<a href="__AUTH_URL__" target="_blank" rel="noopener">打开 Microsoft 登录</a></p>
<p class="muted">auth_url 文件：<code>__URL_FILE__</code></p>
<p>2. 登录后浏览器会到 <code>oauth2/nativeclient?code=...</code>
（页面可能显示 wrongplace，没关系）。</p>
<p>3. 把<strong>完整地址栏 URL</strong>（含 code=）粘贴到下方提交。</p>
</div>
<div class="card">
<form method="POST" action="/finish">
<label>Redirect URL 或 code</label>
<textarea name="code" placeholder="https://login.microsoftonline.com/common/oauth2/nativeclient?code=..." required></textarea>
<p style="margin-top:.75rem"><button type="submit">完成登录 / Finish</button></p>
</form>
</div>
<p class="muted">account_key=__ACCOUNT_KEY__ · label=__LABEL__ · 本页仅监听 127.0.0.1</p>
</body></html>
"""

RESULT_HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"/><title>MCG Login</title>
<style>body{font-family:system-ui;max-width:640px;margin:3rem auto;background:#0f1419;color:#e7ecf1}
.ok{color:#4ade80}.err{color:#f87171}code{background:#1a2332;padding:.2rem .4rem;border-radius:4px}</style>
</head><body>
{body}
</body></html>
"""


class LoginAssistResult:
    def __init__(self) -> None:
        self.done = threading.Event()
        self.ok = False
        self.error: str | None = None
        self.account: dict[str, Any] | None = None
        self.raw: dict[str, Any] | None = None


def run_login_assist(
    *,
    auth_url: str,
    account_key: str,
    label: str,
    url_file: Path,
    finish_callback: Callable[[str], dict[str, Any]],
    host: str = "127.0.0.1",
    port: int = 17890,
    open_browser: bool = True,
    timeout_sec: float = 600.0,
) -> LoginAssistResult:
    """Block until user pastes code or timeout. finish_callback(code_or_url)->result dict."""
    result = LoginAssistResult()
    state = {"auth_url": auth_url, "account_key": account_key, "label": label, "url_file": str(url_file)}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:  # quiet
            return

        def _send(self, code: int, body: str, content_type: str = "text/html; charset=utf-8") -> None:
            data = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:  # noqa: N802
            if self.path.startswith("/status"):
                payload = {
                    "done": result.done.is_set(),
                    "ok": result.ok,
                    "error": result.error,
                    "account": result.account,
                }
                self._send(200, json.dumps(payload), "application/json")
                return
            page = (
                ASSIST_HTML
                .replace("__AUTH_URL__", html.escape(state["auth_url"], quote=True))
                .replace("__URL_FILE__", html.escape(state["url_file"]))
                .replace("__ACCOUNT_KEY__", html.escape(state["account_key"]))
                .replace("__LABEL__", html.escape(state["label"] or ""))
            )
            self._send(200, page)

        def do_POST(self) -> None:  # noqa: N802
            if not self.path.startswith("/finish"):
                self._send(404, "not found")
                return
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length).decode("utf-8", "ignore")
            form = urllib.parse.parse_qs(raw)
            code = (form.get("code") or [""])[0].strip()
            if not code:
                self._send(
                    400,
                    RESULT_HTML.format(body='<p class="err">empty code</p><p><a href="/">back</a></p>'),
                )
                return
            try:
                out = finish_callback(code)
                result.ok = bool(out.get("ok", True))
                result.account = out.get("account")
                result.raw = out
                result.error = out.get("error")
                if result.ok:
                    body = (
                        f'<h1 class="ok">登录成功</h1>'
                        f'<p>account=<code>{html.escape(str((result.account or {}).get("id","")))}</code></p>'
                        f'<p>ttl=<code>{html.escape(str(out.get("ttl","")))}</code>s '
                        f'source=<code>{html.escape(str(out.get("source","")))}</code></p>'
                        f'<p>has_refresh=<code>{html.escape(str(out.get("has_refresh")))}</code></p>'
                        f'<p class="muted">可关闭本页，网关 keep-alive 会静默续期。</p>'
                    )
                else:
                    body = f'<h1 class="err">失败</h1><p>{html.escape(str(result.error))}</p><p><a href="/">retry</a></p>'
            except Exception as exc:  # noqa: BLE001
                result.ok = False
                result.error = str(exc)
                body = f'<h1 class="err">失败</h1><p>{html.escape(str(exc))}</p><p><a href="/">retry</a></p>'
            result.done.set()
            self._send(200 if result.ok else 400, RESULT_HTML.format(body=body))

    server = ThreadingHTTPServer((host, port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    local = f"http://{host}:{port}/"
    print(f"[login-assist] open paste page: {local}", flush=True)
    print(f"[login-assist] auth_url_file: {url_file}", flush=True)
    if open_browser:
        try:
            webbrowser.open(auth_url)
        except Exception:  # noqa: BLE001
            pass
        try:
            webbrowser.open(local)
        except Exception:  # noqa: BLE001
            pass

    deadline = time.time() + timeout_sec
    try:
        while time.time() < deadline:
            if result.done.wait(0.5):
                break
        if not result.done.is_set():
            result.error = f"timeout after {timeout_sec}s"
            result.ok = False
            result.done.set()
    finally:
        server.shutdown()
        thread.join(timeout=2)
    return result


async def run_login_assist_async(**kwargs: Any) -> LoginAssistResult:
    return await asyncio.to_thread(run_login_assist, **kwargs)
