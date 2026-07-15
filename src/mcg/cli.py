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
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="mcg", description="M365 Copilot Gateway")
    parser.add_argument("-c", "--config", default="config.yaml", help="config path")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_serve = sub.add_parser("serve", help="run API + WebUI")
    p_serve.add_argument("--host", default=None)
    p_serve.add_argument("--port", type=int, default=None)

    p_imp = sub.add_parser("import-token", help="import substrate JWT into account pool")
    p_imp.add_argument("token_file", help="file containing JWT (or - for stdin)")
    p_imp.add_argument("--label", default="")

    p_blogin = sub.add_parser(
        "browser-login",
        help="semi-auto: open dedicated browser, capture substrate JWT via CDP",
    )
    p_blogin.add_argument("--label", default="")
    p_blogin.add_argument("--id", dest="account_id", default=None, help="reuse account id / profile")
    p_blogin.add_argument("--port", type=int, default=None, help="CDP port (default from config)")
    p_blogin.add_argument(
        "--cdp",
        default=None,
        help="attach existing CDP http://127.0.0.1:PORT (do not launch)",
    )
    p_blogin.add_argument("--timeout", type=float, default=None)
    p_blogin.add_argument("--headless", action="store_true", help="headless (needs existing cookies)")

    p_refresh = sub.add_parser("refresh-token", help="refresh one account via CDP profile")
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

    if args.cmd == "import-token":
        if args.token_file == "-":
            token = sys.stdin.read().strip()
        else:
            token = Path(args.token_file).read_text(encoding="utf-8").strip()
        acc = pool.import_token(token, label=args.label)
        print(f"imported {acc.id} label={acc.label} ttl_ok")
        return

    if args.cmd == "browser-login":
        account_id = args.account_id or f"pending-{uuid.uuid4().hex[:10]}"
        if args.port:
            fabric.cdp_port = args.port
        if args.timeout:
            fabric.cdp_timeout_sec = args.timeout
        if args.headless:
            fabric.headless = True

        def on_status(msg: str) -> None:
            print(f"[cdp] {msg}", flush=True)

        async def run():
            st = await fabric.capture_via_cdp(
                account_id,
                cdp_http=args.cdp,
                interactive=not args.headless,
                on_status=on_status,
            )
            return st

        st = asyncio.run(run())
        if not st.valid:
            print(f"FAILED: {st.error}", file=sys.stderr)
            sys.exit(1)
        token = fabric.get_hot(account_id)
        assert token
        # re-key by oid if pending
        from mcg.token.jwtutil import decode_jwt_payload

        claims = decode_jwt_payload(token)
        real_id = str(claims.get("oid") or account_id)
        label = args.label or f"user-{real_id[:8]}"
        acc = pool.import_token(token, label=label)
        profile = str(fabric.profile_dir_for(account_id if account_id.startswith("pending") else real_id))
        # keep profile under real oid
        real_profile = fabric.profile_dir_for(real_id)
        if account_id != real_id:
            src = fabric.profile_dir_for(account_id)
            if src.exists() and not real_profile.exists():
                try:
                    src.rename(real_profile)
                except OSError:
                    pass
            fabric.put_hot(real_id, token)
        pool.bind_profile(acc.id, str(real_profile))
        print(
            f"OK account={acc.id} label={acc.label} ttl={st.seconds_remaining}s "
            f"source={st.source} profile={real_profile}"
        )
        return

    if args.cmd == "refresh-token":
        if args.account_id not in pool.accounts:
            print("unknown account", file=sys.stderr)
            sys.exit(1)
        acc = pool.accounts[args.account_id]
        if args.timeout:
            fabric.cdp_timeout_sec = args.timeout

        def on_status(msg: str) -> None:
            print(f"[cdp] {msg}", flush=True)

        async def run():
            return await fabric.capture_via_cdp(
                args.account_id,
                cdp_http=args.cdp,
                interactive=True,
                on_status=on_status,
                profile_path=acc.profile_path or None,
            )

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
            print(
                f"{a['id'][:8]}…  {a['label']:<20}  status={a['status']:<10}  "
                f"ttl={a['token_ttl_sec']}s  valid={a['token_valid']}"
            )
        return

    if args.cmd == "models":
        for m in list_models():
            print(f"{m.id:<32} tone={m.tone}")
        return


if __name__ == "__main__":
    main()
