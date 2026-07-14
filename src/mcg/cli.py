from __future__ import annotations

import argparse
import sys
from pathlib import Path


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

    sub.add_parser("accounts", help="list accounts")
    sub.add_parser("models", help="list advertised models")

    args = parser.parse_args(argv)

    from mcg.config import load_config
    from mcg.api.app import create_app
    from mcg.models_catalog import list_models
    from mcg.pool.store import AccountPool
    from mcg.token.fabric import TokenFabric

    cfg = load_config(args.config)
    data_dir = Path(cfg.gateway.data_dir)

    if args.cmd == "serve":
        import uvicorn

        app = create_app(config=cfg)
        host = args.host or cfg.gateway.host
        port = args.port or cfg.gateway.port
        uvicorn.run(app, host=host, port=port, log_level="info")
        return

    fabric = TokenFabric(data_dir, refresh_skew_sec=cfg.token.refresh_skew_sec)
    pool = AccountPool(data_dir, fabric, strategy=cfg.pool.strategy)

    if args.cmd == "import-token":
        if args.token_file == "-":
            token = sys.stdin.read().strip()
        else:
            token = Path(args.token_file).read_text(encoding="utf-8").strip()
        acc = pool.import_token(token, label=args.label)
        print(f"imported {acc.id} label={acc.label} ttl_ok")
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
