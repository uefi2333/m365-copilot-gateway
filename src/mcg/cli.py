from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from pathlib import Path


def _fabric(cfg):
    from mcg.token.fabric import TokenFabric

    return TokenFabric(
        Path(cfg.gateway.data_dir),
        refresh_skew_sec=cfg.token.refresh_skew_sec,
        prefer_cdp=cfg.token.prefer_cdp,
        cdp_port=cfg.token.cdp_port,
        cdp_timeout_sec=cfg.token.cdp_timeout_sec,
        browser_binary=cfg.token.browser_binary,
        headless=cfg.token.headless,
        use_sydney_msal=cfg.token.use_sydney_msal,
        msal_client_id=cfg.token.msal_client_id,
        msal_authority=cfg.token.msal_authority,
        msal_redirect_uri=cfg.token.msal_redirect_uri,
        msal_scopes=cfg.token.msal_scopes,
        oauth_client_id=cfg.token.oauth_client_id,
        oauth_tenant=cfg.token.oauth_tenant,
        oauth_scope=cfg.token.oauth_scope,
        oauth_client_secret=cfg.token.oauth_client_secret,
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="mcg",
        description="M365 Copilot Gateway — Sydney MSAL auth (cramt/lezi mature path)",
    )
    parser.add_argument("-c", "--config", default="config.yaml", help="config path")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_serve = sub.add_parser("serve", help="run API + WebUI")
    p_serve.add_argument("--host", default=None)
    p_serve.add_argument("--port", type=int, default=None)

    p_login = sub.add_parser(
        "login",
        help="mature PKCE login (Office web client + Sydney scopes) — print URL, then finish",
    )
    p_login.add_argument("--label", default="")
    p_login.add_argument("--id", dest="account_id", default=None)
    p_login.add_argument(
        "--finish",
        default=None,
        help="paste redirect URL or code=… to complete exchange",
    )
    p_login.add_argument(
        "--assist",
        action="store_true",
        help="open local paste page (127.0.0.1) + browser; lightest semi-auto login",
    )
    p_login.add_argument("--assist-port", type=int, default=17890)
    p_login.add_argument("--no-browser", action="store_true", help="with --assist, do not auto-open browser")

    p_imp = sub.add_parser("import-token", help="import substrate access JWT (paste / file)")
    p_imp.add_argument("token_file", help="file containing JWT (or - for stdin)")
    p_imp.add_argument("--label", default="")
    p_imp.add_argument("--refresh-token", default=None, help="optional refresh_token")

    p_rt = sub.add_parser("set-refresh-token", help="store legacy refresh_token for account")
    p_rt.add_argument("account_id")
    p_rt.add_argument("refresh_file", help="file with refresh_token or - for stdin")

    p_dev = sub.add_parser(
        "device-login",
        help="device code with Sydney client (often rejected; prefer mcg login)",
    )
    p_dev.add_argument("--label", default="")
    p_dev.add_argument("--id", dest="account_id", default=None)

    p_blogin = sub.add_parser(
        "browser-login",
        help="OPTIONAL: CDP Chrome capture of ChatHub JWT",
    )
    p_blogin.add_argument("--label", default="")
    p_blogin.add_argument("--id", dest="account_id", default=None)
    p_blogin.add_argument("--port", type=int, default=None)
    p_blogin.add_argument("--cdp", default=None)
    p_blogin.add_argument("--timeout", type=float, default=None)
    p_blogin.add_argument("--headless", action="store_true")

    p_refresh = sub.add_parser(
        "refresh-token",
        help="renew access token (Sydney MSAL silent first)",
    )
    p_refresh.add_argument("account_id")
    p_refresh.add_argument("--cdp", default=None)
    p_refresh.add_argument("--timeout", type=float, default=None)

    sub.add_parser("accounts", help="list accounts")
    sub.add_parser("models", help="list advertised models")

    args = parser.parse_args(argv)

    from mcg.config import load_config
    from mcg.api.app import create_app
    from mcg.models_catalog import list_models
    from mcg.pool.store import AccountPool

    cfg = load_config(args.config)
    data_dir = Path(cfg.gateway.data_dir)

    if args.cmd == "serve":
        import uvicorn

        app = create_app(config=cfg)
        host = args.host or cfg.gateway.host
        port = args.port or cfg.gateway.port
        uvicorn.run(app, host=host, port=port, log_level="info")
        return

    fabric = _fabric(cfg)
    pool = AccountPool(data_dir, fabric, strategy=cfg.pool.strategy)

    if args.cmd == "login":
        account_id = args.account_id or f"pending-{uuid.uuid4().hex[:10]}"
        if not args.finish and not args.assist:
            info = asyncio.run(fabric.login_pkce_start(account_key=account_id))
            print("=== M365 Sydney PKCE (mature path) ===")
            print(f"client_id: {info['client_id']}")
            print(f"account_key: {account_id}")
            print()
            print("IMPORTANT: Do NOT open a URL pasted through chat apps — they corrupt %2F")
            print("and trigger AADSTS70011. Prefer opening the local file or WebUI link.")
            url_file = data_dir / "msal" / "last_auth_url.txt"
            print(f"auth_url_file: {url_file}")
            print()
            print("Lightest path:  mcg login --assist --label me")
            print()
            print("1) Open the auth URL (from file above, or the line below) in a browser:")
            print(info["auth_url"])
            print()
            print("2) After login, copy the URL that contains oauth2/nativeclient?code=...")
            print("   (browser may land on /common/wrongplace — grab the nativeclient URL from")
            print("    address bar or DevTools Network → request URL)")
            print()
            print("3) Finish:")
            print(
                f'   mcg -c {args.config} login --id {account_id} '
                f'--finish "PASTE_URL_HERE" --label "{args.label or "me"}"'
            )
            return

        if args.assist and not args.finish:
            info = asyncio.run(fabric.login_pkce_start(account_key=account_id))
            url_file = data_dir / "msal" / "last_auth_url.txt"
            label = args.label or "me"

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
                return {
                    "ok": True,
                    "account": acc.public_dict(),
                    "ttl": st.seconds_remaining,
                    "source": st.source,
                    "has_refresh": bool(rt),
                    "aud": claims.get("aud"),
                }

            from mcg.token.login_assist import run_login_assist

            res = run_login_assist(
                auth_url=info["auth_url"],
                account_key=account_id,
                label=label,
                url_file=url_file,
                finish_callback=_finish,
                port=args.assist_port,
                open_browser=not args.no_browser,
            )
            if not res.ok:
                print(f"FAILED: {res.error}", file=sys.stderr)
                sys.exit(1)
            acc = res.account or {}
            print(
                f"OK account={acc.get('id')} label={acc.get('label')} "
                f"ttl={res.raw.get('ttl') if res.raw else '?'}s "
                f"has_refresh={res.raw.get('has_refresh') if res.raw else '?'}"
            )
            return

        st = asyncio.run(
            fabric.login_pkce_finish(args.finish, account_id=account_id, account_key=account_id)
        )
        if not st.valid:
            print(f"FAILED: {st.error}", file=sys.stderr)
            sys.exit(1)
        token = fabric.get_hot(account_id)
        assert token
        from mcg.token.jwtutil import decode_jwt_payload

        claims = decode_jwt_payload(token)
        real_id = str(claims.get("oid") or account_id)
        label = args.label or f"user-{real_id[:8]}"
        # re-key hot token under oid if different
        if real_id != account_id:
            fabric.put_hot(real_id, token)
            if fabric.get_refresh_token(account_id):
                fabric.put_refresh_token(real_id, fabric.get_refresh_token(account_id) or "")
        acc = pool.import_token(token, label=label)
        fabric.rekey_msal_artifacts(account_id, acc.id)
        print(
            f"OK account={acc.id} label={acc.label} ttl={st.seconds_remaining}s "
            f"source={st.source} aud={claims.get('aud')}"
        )
        return

    if args.cmd == "import-token":
        if args.token_file == "-":
            token = sys.stdin.read().strip()
        else:
            token = Path(args.token_file).read_text(encoding="utf-8").strip()
        acc = pool.import_token(token, label=args.label)
        if args.refresh_token:
            rt = args.refresh_token
            if rt == "-":
                rt = sys.stdin.read().strip()
            elif Path(rt).is_file():
                rt = Path(rt).read_text(encoding="utf-8").strip()
            fabric.put_refresh_token(acc.id, rt)
        print(f"imported {acc.id} label={acc.label} has_rt={bool(fabric.get_refresh_token(acc.id))}")
        return

    if args.cmd == "set-refresh-token":
        if args.account_id not in pool.accounts:
            print("unknown account", file=sys.stderr)
            sys.exit(1)
        if args.refresh_file == "-":
            rt = sys.stdin.read().strip()
        else:
            rt = Path(args.refresh_file).read_text(encoding="utf-8").strip()
        fabric.put_refresh_token(args.account_id, rt)
        print(f"stored refresh_token for {args.account_id}")
        return

    if args.cmd == "device-login":
        print(
            "NOTE: device-code often fails for Office web client. Prefer: mcg login\n",
            file=sys.stderr,
        )
        account_id = args.account_id or f"pending-{uuid.uuid4().hex[:10]}"

        def on_status(msg: str) -> None:
            print(f"[device] {msg}", flush=True)

        st = asyncio.run(fabric.login_device_code(account_id, on_status=on_status, use_sydney=True))
        if not st.valid:
            print(f"FAILED: {st.error}", file=sys.stderr)
            sys.exit(1)
        token = fabric.get_hot(account_id)
        assert token
        from mcg.token.jwtutil import decode_jwt_payload

        claims = decode_jwt_payload(token)
        label = args.label or f"user-{str(claims.get('oid', account_id))[:8]}"
        acc = pool.import_token(token, label=label)
        print(f"OK account={acc.id} label={acc.label} ttl={st.seconds_remaining}s")
        return

    if args.cmd == "browser-login":
        print("NOTE: browser-login needs Chrome/Edge. Prefer: mcg login", file=sys.stderr)
        account_id = args.account_id or f"pending-{uuid.uuid4().hex[:10]}"
        if args.port:
            fabric.cdp_port = args.port
        if args.timeout:
            fabric.cdp_timeout_sec = args.timeout
        if args.headless:
            fabric.headless = True

        def on_status(msg: str) -> None:
            print(f"[cdp] {msg}", flush=True)

        st = asyncio.run(
            fabric.capture_via_cdp(
                account_id,
                cdp_http=args.cdp,
                interactive=not args.headless,
                on_status=on_status,
            )
        )
        if not st.valid:
            print(f"FAILED: {st.error}", file=sys.stderr)
            sys.exit(1)
        token = fabric.get_hot(account_id)
        assert token
        from mcg.token.jwtutil import decode_jwt_payload

        claims = decode_jwt_payload(token)
        real_id = str(claims.get("oid") or account_id)
        label = args.label or f"user-{real_id[:8]}"
        acc = pool.import_token(token, label=label)
        print(f"OK account={acc.id} label={acc.label} ttl={st.seconds_remaining}s source={st.source}")
        return

    if args.cmd == "refresh-token":
        if args.account_id not in pool.accounts:
            print("unknown account", file=sys.stderr)
            sys.exit(1)
        if args.timeout:
            fabric.cdp_timeout_sec = args.timeout

        def on_status(msg: str) -> None:
            print(f"[refresh] {msg}", flush=True)

        async def _go():
            st = await fabric.refresh_via_sydney_msal(args.account_id, on_status=on_status)
            if st.valid:
                return st
            if fabric.oauth_client_id:
                st = await fabric.refresh_via_oauth(args.account_id, on_status=on_status)
                if st.valid:
                    return st
            if args.cdp or fabric.prefer_cdp:
                return await fabric.capture_via_cdp(
                    args.account_id,
                    cdp_http=args.cdp,
                    interactive=False,
                    on_status=on_status,
                )
            return st

        st = asyncio.run(_go())
        if not st.valid:
            print(f"FAILED: {st.error}", file=sys.stderr)
            sys.exit(1)
        print(f"OK ttl={st.seconds_remaining}s source={st.source}")
        return

    if args.cmd == "accounts":
        for a in pool.list_public():
            print(
                f"{a.get('id')}\t{a.get('label')}\tstatus={a.get('status')}\t"
                f"valid={a.get('token_valid')}\tttl={a.get('token_ttl_sec')}\t"
                f"errors={a.get('errors')}"
            )
        return

    if args.cmd == "models":
        for m in list_models(cfg):
            print(f"{m['id']}\t{m.get('owned_by','')}\t{m.get('description','')}")
        return


if __name__ == "__main__":
    main()
