from __future__ import annotations

"""Mature Sydney token path (aligned with cramt/m365-copilot-proxy + lezi-fun).

Client: Office web Copilot first-party
  c0ab8ce9-e9a0-42e7-b064-33d422df41f1

Scopes (NOT ows/.default):
  https://substrate.office.com/sydney/M365Chat.Read
  https://substrate.office.com/sydney/sydney.readwrite

Token aud: https://substrate.office.com/sydney

Flows:
  1) MSAL silent from disk cache (refresh under the hood)
  2) PKCE auth-code: gen URL → user pastes nativeclient?code=… → exchange
  3) Optional MSAL interactive (local browser)
  4) Device code is last-resort / often rejected for this client

Requires: pip install msal
"""


import base64
import hashlib
import json
import secrets
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import httpx

# First-party Office web Copilot client (community-verified June 2026)
DEFAULT_CLIENT_ID = "c0ab8ce9-e9a0-42e7-b064-33d422df41f1"
DEFAULT_AUTHORITY = "https://login.microsoftonline.com/common"
DEFAULT_REDIRECT_URI = "https://login.microsoftonline.com/common/oauth2/nativeclient"
SYDNEY_SCOPES = [
    "https://substrate.office.com/sydney/M365Chat.Read",
    "https://substrate.office.com/sydney/sydney.readwrite",
]
# Extra scopes used by cramt for Copilot Studio agent mgmt (optional acquire)
PP_SCOPE = "https://api.powerplatform.com/.default"
BAP_SCOPE = "https://api.bap.microsoft.com/.default"


class SydneyAuthError(RuntimeError):
    pass


@dataclass
class TokenBundle:
    access_token: str
    refresh_token: str | None = None
    expires_in: int = 0
    source: str = "unknown"
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class PkceStart:
    auth_url: str
    code_verifier: str
    state: str
    client_id: str
    redirect_uri: str
    scopes: list[str]


def _require_msal():
    try:
        import msal  # noqa: F401
    except ImportError as exc:
        raise SydneyAuthError(
            "msal not installed. Run: pip install 'm365-copilot-gateway[auth]' or pip install msal"
        ) from exc
    return __import__("msal")


def _cache_path(data_dir: Path, account_key: str = "default") -> Path:
    d = Path(data_dir) / "msal"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{account_key}.json"


def _pkce_pending_path(data_dir: Path) -> Path:
    d = Path(data_dir) / "msal"
    d.mkdir(parents=True, exist_ok=True)
    return d / "pkce_pending.json"


class SydneyMsal:
    """MSAL wrapper for Substrate Sydney tokens."""

    def __init__(
        self,
        data_dir: Path,
        *,
        client_id: str = DEFAULT_CLIENT_ID,
        authority: str = DEFAULT_AUTHORITY,
        redirect_uri: str = DEFAULT_REDIRECT_URI,
        scopes: list[str] | None = None,
        account_key: str = "default",
    ) -> None:
        self.data_dir = Path(data_dir)
        self.client_id = client_id
        self.authority = authority
        self.redirect_uri = redirect_uri
        self.scopes = list(scopes or SYDNEY_SCOPES)
        self.account_key = account_key
        self.cache_file = _cache_path(self.data_dir, account_key)

    def _app(self):
        msal = _require_msal()
        cache = msal.SerializableTokenCache()
        if self.cache_file.exists():
            try:
                cache.deserialize(self.cache_file.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                pass
        app = msal.PublicClientApplication(
            client_id=self.client_id,
            authority=self.authority,
            token_cache=cache,
        )
        return app, cache

    def _save(self, cache) -> None:
        if cache.has_state_changed or True:
            self.cache_file.write_text(cache.serialize(), encoding="utf-8")
            try:
                self.cache_file.chmod(0o600)
            except OSError:
                pass

    def acquire_silent(self) -> TokenBundle | None:
        app, cache = self._app()
        accounts = app.get_accounts()
        if not accounts:
            return None
        result = app.acquire_token_silent(scopes=self.scopes, account=accounts[0])
        self._save(cache)
        if result and result.get("access_token"):
            return TokenBundle(
                access_token=result["access_token"],
                refresh_token=result.get("refresh_token"),
                expires_in=int(result.get("expires_in") or 0),
                source="msal-silent",
                raw=result,
            )
        return None

    def start_pkce(self) -> PkceStart:
        """Build authorize URL via MSAL initiate_auth_code_flow (cramt-equivalent).

        Persist:
          - data/msal/pkce_pending.json  (full MSAL flow + verifier)
          - data/msal/last_auth_url.txt  (open this; do not trust chat-mangled links)
        Chat apps often corrupt %2F → AADSTS70011 (https:%F/...).
        """
        app, _cache = self._app()
        flow = app.initiate_auth_code_flow(
            scopes=self.scopes,
            redirect_uri=self.redirect_uri,
            response_mode="query",
        )
        auth_url = flow.get("auth_uri") or flow.get("auth_url")
        if not auth_url:
            raise SydneyAuthError(f"MSAL did not return auth_uri: {flow}")
        verifier = flow.get("code_verifier") or ""
        state = flow.get("state") or ""
        pending = {
            "msal_flow": flow,
            "code_verifier": verifier,
            "state": state,
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "scopes": self.scopes,
            "created_at": int(time.time()),
            "auth_url": auth_url,
        }
        pending_path = _pkce_pending_path(self.data_dir)
        pending_path.write_text(json.dumps(pending, indent=2), encoding="utf-8")
        url_path = self.data_dir / "msal" / "last_auth_url.txt"
        url_path.write_text(auth_url + "\n", encoding="utf-8")
        try:
            url_path.chmod(0o600)
            pending_path.chmod(0o600)
        except OSError:
            pass
        return PkceStart(
            auth_url=auth_url,
            code_verifier=verifier,
            state=state,
            client_id=self.client_id,
            redirect_uri=self.redirect_uri,
            scopes=self.scopes,
        )

    def exchange_code(
        self,
        code_or_url: str,
        *,
        code_verifier: str | None = None,
        timeout: float = 30.0,
    ) -> TokenBundle:
        """Exchange auth code (or full redirect URL containing code=)."""
        raw = code_or_url.strip()
        auth_response: dict[str, str] = {}
        if "code=" in raw or raw.startswith("http") or "?" in raw:
            parsed = urllib.parse.urlparse(raw if "://" in raw else ("https://x/?" + raw.lstrip("?")))
            qs = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items() if v}
            if not qs and parsed.fragment:
                qs = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.fragment).items() if v}
            auth_response = qs
            code = qs.get("code")
        else:
            code = raw
            auth_response = {"code": code}

        if not code:
            raise SydneyAuthError("no authorization code in input")

        pending_path = _pkce_pending_path(self.data_dir)
        pending: dict[str, Any] = {}
        if pending_path.exists():
            pending = json.loads(pending_path.read_text(encoding="utf-8"))

        # Preferred: MSAL auth-code flow (keeps cache coherent)
        msal_flow = pending.get("msal_flow")
        if msal_flow and auth_response.get("code"):
            app, cache = self._app()
            # MSAL expects state match when present
            if "state" not in auth_response and msal_flow.get("state"):
                auth_response = {**auth_response, "state": msal_flow["state"]}
            result = app.acquire_token_by_auth_code_flow(msal_flow, auth_response)
            self._save(cache)
            if result and result.get("access_token"):
                if pending_path.exists():
                    try:
                        pending_path.unlink()
                    except OSError:
                        pass
                # keep sidecar RT if present
                if result.get("refresh_token"):
                    self._ingest_into_msal_cache(result)
                return TokenBundle(
                    access_token=result["access_token"],
                    refresh_token=result.get("refresh_token"),
                    expires_in=int(result.get("expires_in") or 0),
                    source="msal-auth-code-flow",
                    raw=result,
                )
            err = (result or {}).get("error_description") or (result or {}).get("error")
            if err:
                # fall through to raw HTTP with stored verifier
                pass

        verifier = code_verifier or pending.get("code_verifier")
        if not verifier and msal_flow:
            verifier = msal_flow.get("code_verifier")
        if not verifier:
            raise SydneyAuthError("missing code_verifier — run pkce start first")

        data = {
            "client_id": self.client_id,
            "code": code,
            "redirect_uri": self.redirect_uri,
            "grant_type": "authorization_code",
            "code_verifier": verifier,
            "scope": " ".join(self.scopes),
        }
        token_url = f"{self.authority.rstrip('/')}/oauth2/v2.0/token"
        with httpx.Client(timeout=timeout) as client:
            r = client.post(
                token_url,
                data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            body = r.json() if r.content else {}
        if r.status_code >= 400 or not body.get("access_token"):
            err = body.get("error_description") or body.get("error") or r.text[:300]
            raise SydneyAuthError(f"code exchange failed: {err}")

        self._ingest_into_msal_cache(body)
        if pending_path.exists():
            try:
                pending_path.unlink()
            except OSError:
                pass
        return TokenBundle(
            access_token=body["access_token"],
            refresh_token=body.get("refresh_token"),
            expires_in=int(body.get("expires_in") or 0),
            source="pkce-exchange",
            raw=body,
        )

    def _ingest_into_msal_cache(
self, token_response: dict[str, Any]) -> None:
        """Best-effort: store RT/AT so acquire_token_silent works later."""
        try:
            msal = _require_msal()
        except SydneyAuthError:
            return
        app, cache = self._app()
        # MSAL has no public "inject RT" for arbitrary responses on all versions;
        # if refresh_token present, try refresh via raw HTTP and let next silent use cache
        # after a silent-friendly path: use acquire_token_by_refresh_token if available.
        rt = token_response.get("refresh_token")
        if rt and hasattr(app, "acquire_token_by_refresh_token"):
            try:
                result = app.acquire_token_by_refresh_token(rt, scopes=self.scopes)
                if result and result.get("access_token"):
                    self._save(cache)
                    return
            except Exception:  # noqa: BLE001
                pass
        # Fallback: keep a sidecar refresh store next to MSAL cache
        side = self.cache_file.with_suffix(".rt.json")
        side.write_text(
            json.dumps(
                {
                    "refresh_token": rt,
                    "client_id": self.client_id,
                    "scopes": self.scopes,
                    "updated_at": int(time.time()),
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        try:
            side.chmod(0o600)
        except OSError:
            pass
        self._save(cache)

    def refresh_with_sidecar_rt(self, timeout: float = 30.0) -> TokenBundle | None:
        """HTTP refresh using sidecar RT if MSAL silent failed."""
        side = self.cache_file.with_suffix(".rt.json")
        if not side.exists():
            return None
        try:
            data = json.loads(side.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return None
        rt = data.get("refresh_token")
        if not rt:
            return None
        form = {
            "client_id": self.client_id,
            "grant_type": "refresh_token",
            "refresh_token": rt,
            "scope": " ".join(self.scopes),
        }
        token_url = f"{self.authority.rstrip('/')}/oauth2/v2.0/token"
        with httpx.Client(timeout=timeout) as client:
            r = client.post(token_url, data=form)
            body = r.json() if r.content else {}
        if not body.get("access_token"):
            return None
        if body.get("refresh_token"):
            data["refresh_token"] = body["refresh_token"]
            data["updated_at"] = int(time.time())
            side.write_text(json.dumps(data, indent=2), encoding="utf-8")
        self._ingest_into_msal_cache(body)
        return TokenBundle(
            access_token=body["access_token"],
            refresh_token=body.get("refresh_token") or rt,
            expires_in=int(body.get("expires_in") or 0),
            source="rt-sidecar",
            raw=body,
        )

    def acquire_interactive(self) -> TokenBundle:
        app, cache = self._app()
        result = app.acquire_token_interactive(scopes=self.scopes)
        self._save(cache)
        if not result.get("access_token"):
            err = result.get("error_description") or result.get("error") or "interactive failed"
            raise SydneyAuthError(str(err))
        return TokenBundle(
            access_token=result["access_token"],
            refresh_token=result.get("refresh_token"),
            expires_in=int(result.get("expires_in") or 0),
            source="msal-interactive",
            raw=result,
        )

    def acquire_device_code(
        self,
        on_status: Callable[[str], None] | None = None,
    ) -> TokenBundle:
        """Often rejected for this client — kept for completeness."""
        app, cache = self._app()
        flow = app.initiate_device_flow(scopes=self.scopes)
        if "user_code" not in flow:
            raise SydneyAuthError(
                flow.get("error_description") or flow.get("error") or "device flow init failed"
            )
        msg = flow.get("message") or f"{flow.get('verification_uri')} code={flow.get('user_code')}"
        if on_status:
            on_status(msg)
        result = app.acquire_token_by_device_flow(flow)
        self._save(cache)
        if not result.get("access_token"):
            raise SydneyAuthError(
                result.get("error_description") or result.get("error") or "device flow failed"
            )
        return TokenBundle(
            access_token=result["access_token"],
            refresh_token=result.get("refresh_token"),
            expires_in=int(result.get("expires_in") or 0),
            source="msal-device",
            raw=result,
        )

    def ensure(
        self,
        *,
        allow_interactive: bool = False,
        on_status: Callable[[str], None] | None = None,
    ) -> TokenBundle:
        """Silent → sidecar RT → optional interactive."""
        if on_status:
            on_status("msal silent…")
        b = self.acquire_silent()
        if b:
            return b
        if on_status:
            on_status("sidecar refresh_token…")
        b = self.refresh_with_sidecar_rt()
        if b:
            return b
        if allow_interactive:
            if on_status:
                on_status("interactive browser login…")
            return self.acquire_interactive()
        raise SydneyAuthError(
            "no MSAL cache / refresh_token. Run: mcg login  (PKCE) or mcg import-token"
        )
