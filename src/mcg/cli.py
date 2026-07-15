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
        oauth_client_id=cfg.token.oauth_client_id,
        oauth_tenant=cfg.token.oauth_tenant,
        oauth_scope=cfg.token.oauth_scope,
        oauth_client_secret=cfg.token.oauth_client_secret,
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="mcg", description="M365 Copilot Gateway (no Chrome required)")
    parser.add_argument("-c", "--config", default="config.yaml", help="config path")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_serve = sub.add_parser("serve", help="run API + WebUI")
    p_serve.add_argument("--host", default=None)
    p_serve.add_argument("--port", type=int, default=None)

    p_imp = sub.add_parser("import-token", help="import substrate access JWT (paste / file)")
    p_imp.add_argument("token_file", help="file containing JWT (or - for stdin)")
    p_imp.add_argument("--label", default="")
    p_imp.add_argument("--refresh-token", default=None, help="optional OAuth refresh_token for silent renew")

    p_rt = sub.add_parser("set-refresh-token", help="store refresh_token for an account (HTTP renew)")
    p_rt.add_argument("account_id")
    p_rt.add_argument("refresh_file", help="file with refresh_token or - for stdin")

    p_dev = sub.add_parser(
        "device-login",
        help="EXPERIMENTAL OAuth device code (may not yield ChatHub-valid substrate JWT)",
    )
    p_dev.add_argument("--label", default="")
    p_dev.add_argument("--id", dest="account_id", default=None)

    p_blogin = sub.add_parser(
        "browser-login",
        help="OPTIONAL heavy: CDP Chrome/Edge capture (needs browser binary)",
    )
    p_blogin.add_argument("--label", default="")
    p_blogin.add_argument("--id", dest="account_id", default=None)
    p_blogin.add_argument("--port", type=int, default=None)
    p_blogin.add_argument("--cdp", default=None)
    p_blogin.add_argument("--timeout", type=float, default=None)
    p_blogin.add_argument("--headless", action="store_true")

    p_refresh = sub.add_parser("refresh-token", help="renew access token (OAuth first, CDP only if enabled)")
    p_refresh.add_argument("account_id")
    p_refresh.add_argument("--cdp", default=None, help="force CDP attach URL")
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
            "WARNING: device-login is EXPERIMENTAL.\n"
            "Probes: device_code START may work for some first-party client_ids;\n"
            "without user MFA you get no AT/RT; ChatHub-valid RT→AT is NOT guaranteed.\n"
            "Reliable path: mcg import-token (paste browser JWT). See docs/TOKEN_CDP.md",
            file=sys.stderr,
        )
        if not fabric.oauth_client_id:
            print("FAILED: set token.oauth_client_id (custom apps usually cannot get substrate).", file=sys.stderr)
            sys.exit(2)
        account_id = args.account_id or f"pending-{uuid.uuid4().hex[:10]}"

        def on_status(msg: str) -> None:
            print(f"[oauth] {msg}", flush=True)

        st = asyncio.run(fabric.login_device_code(account_id, on_status=on_status))
        if not st.valid:
            print(f"FAILED: {st.error}", file=sys.stderr)
            print(
                "Hint: set token.oauth_client_id to an Entra app that can mint substrate tokens, "
                "or just paste a JWT with: mcg import-token -",
                file=sys.stderr,
            )
            sys.exit(1)
        token = fabric.get_hot(account_id)
        assert token
        from mcg.token.jwtutil import decode_jwt_payload

        claims = decode_jwt_payload(token)
        real_id = str(claims.get("oid") or account_id)
        label = args.label or f"user-{real_id[:8]}"
        acc = pool.import_token(token, label=label)
        if fabric.get_refresh_token(account_id):
            fabric.put_refresh_token(acc.id, fabric.get_refresh_token(account_id) or "")
        print(f"OK account={acc.id} label={acc.label} ttl={st.seconds_remaining}s source={st.source}")
        return

    if args.cmd == "browser-login":
        print(
            "NOTE: browser-login needs Chrome/Edge. Prefer: mcg import-token / mcg device-login",
            file=sys.stderr,
        )
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
        profile = fabric.profile_dir_for(real_id)
        if account_id != real_id:
            src = fabric.profile_dir_for(account_id)
            if src.exists() and not profile.exists():
                try:
                    src.rename(profile)
                except OSError:
                    pass
            fabric.put_hot(real_id, token)
        pool.bind_profile(acc.id, str(profile))
        print(f"OK account={acc.id} label={acc.label} ttl={st.seconds_remaining}s source={st.source}")
        return

    if args.cmd == "refresh-token":
        if args.account_id not in pool.accounts:
            print("unknown account", file=sys.stderr)
            sys.exit(1)
        acc = pool.accounts[args.account_id]
        if args.timeout:
            fabric.cdp_timeout_sec = args.timeout

        def on_status(msg: str) -> None:
            print(f"[refresh] {msg}", flush=True)

        async def run():
            st = await fabric.refresh_via_oauth(args.account_id, on_status=on_status)
            if st.valid:
                return st
            if args.cdp or fabric.prefer_cdp:
                return await fabric.capture_via_cdp(
                    args.account_id,
                    cdp_http=args.cdp,
                    interactive=True,
                    on_status=on_status,
                    profile_path=acc.profile_path or None,
                )
            return st

        st = asyncio.run(run())
        if not st.valid:
            print(f"FAILED: {st.error}", file=sys.stderr)
            sys.exit(1)
        token = fabric.get_hot(args.account_id)
        assert token
        pool.refresh_token(args.account_id, token)
        print(f"OK refreshed {args.account_id} ttl={st.seconds_remaining}s source={st.source}")
        return

    if args.cmd == "accounts":
        for a in pool.list_public():
            rt = "rt" if fabric.get_refresh_token(a["id"]) else "  "
            print(
                f"{a['id'][:8]}…  {a['label']:<20}  status={a['status']:<10}  "
                f"ttl={a['token_ttl_sec']}s  valid={a['token_valid']}  {rt}"
            )
        return

    if args.cmd == "models":
        for m in list_models():
            print(f"{m.id:<32} tone={m.tone}")
        return


if __name__ == "__main__":
    main()
