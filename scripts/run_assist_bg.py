#!/usr/bin/env python3
"""Background PKCE assist server (no browser)."""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mcg.config import load_config
from mcg.pool.store import AccountPool
from mcg.token.fabric import TokenFabric
from mcg.token.login_assist import run_login_assist


def main() -> int:
    cfg = load_config(ROOT / "config.yaml")
    data_dir = Path(cfg.gateway.data_dir)
    if not data_dir.is_absolute():
        data_dir = ROOT / data_dir
    fabric = TokenFabric(
        data_dir,
        use_sydney_msal=cfg.token.use_sydney_msal,
        msal_client_id=cfg.token.msal_client_id,
        msal_authority=cfg.token.msal_authority,
        msal_redirect_uri=cfg.token.msal_redirect_uri,
        msal_scopes=cfg.token.msal_scopes,
    )
    pool = AccountPool(data_dir, fabric, strategy=cfg.pool.strategy)
    account_id = "assist-run"
    label = "local-dev"
    info = asyncio.run(fabric.login_pkce_start(account_key=account_id))
    url_file = data_dir / "msal" / "last_auth_url.txt"
    (data_dir / "msal" / "assist_meta.json").write_text(
        json.dumps(
            {
                "account_key": account_id,
                "label": label,
                "paste_page": "http://127.0.0.1:17890/",
                "auth_url_file": str(url_file),
                "started_at": int(time.time()),
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    def _finish(code_or_url: str) -> dict:
        st = asyncio.run(
            fabric.login_pkce_finish(
                code_or_url, account_id=account_id, account_key=account_id
            )
        )
        if not st.valid:
            return {"ok": False, "error": st.error or "pkce failed"}
        token = fabric.get_hot(account_id)
        assert token
        from mcg.token.jwtutil import decode_jwt_payload

        claims = decode_jwt_payload(token)
        real_id = str(claims.get("oid") or account_id)
        if real_id != account_id:
            fabric.put_hot(real_id, token)
            fabric.rekey_msal_artifacts(account_id, real_id)
        acc = pool.import_token(token, label=label)
        fabric.rekey_msal_artifacts(account_id, acc.id)
        rt = fabric.get_refresh_token(acc.id) or fabric.get_refresh_token(account_id)
        if rt:
            fabric.put_refresh_token(acc.id, rt)
        out = {
            "ok": True,
            "account": acc.public_dict(),
            "ttl": st.seconds_remaining,
            "source": st.source,
            "has_refresh": bool(rt),
            "aud": claims.get("aud"),
            "upn": claims.get("upn") or claims.get("unique_name"),
        }
        (data_dir / "msal" / "assist_result.json").write_text(
            json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        return out

    res = run_login_assist(
        auth_url=info["auth_url"],
        account_key=account_id,
        label=label,
        url_file=url_file,
        finish_callback=_finish,
        port=17890,
        open_browser=False,
        timeout_sec=900,
    )
    (data_dir / "msal" / "assist_done.json").write_text(
        json.dumps(
            {"ok": res.ok, "error": res.error, "account": res.account},
            indent=2,
            default=str,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    print("DONE", res.ok, res.error)
    return 0 if res.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
