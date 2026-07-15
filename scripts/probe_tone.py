#!/usr/bin/env python3
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
from mcg.substrate.client import SubstrateClient
from mcg.token.fabric import TokenFabric


async def probe(mid: str, tone: str) -> dict:
    cfg = load_config(str(ROOT / "config.yaml"))
    pool = AccountPool(
        cfg.gateway.data_dir,
        TokenFabric(cfg.gateway.data_dir),
        strategy=cfg.pool.strategy,
        cooldown_sec=cfg.pool.cooldown_sec,
        max_consecutive_errors=cfg.pool.max_consecutive_errors,
    )
    token = next(a.token for a in pool.accounts.values() if a.token)
    client = SubstrateClient(
        access_token=token,
        origin=cfg.substrate.origin,
        time_zone=cfg.substrate.time_zone,
    )
    t0 = time.time()
    try:
        text = await asyncio.wait_for(
            client.chat("Reply exactly: OK", tone=tone, is_start_of_session=True),
            timeout=45,
        )
        return {
            "id": mid,
            "tone": tone,
            "ok": bool(text and str(text).strip()),
            "ms": int((time.time() - t0) * 1000),
            "text": (text or "")[:120],
        }
    except Exception as e:  # noqa: BLE001
        return {
            "id": mid,
            "tone": tone,
            "ok": False,
            "ms": int((time.time() - t0) * 1000),
            "text": f"{type(e).__name__}:{e}",
        }


def main() -> None:
    if len(sys.argv) < 3:
        print("usage: probe_tone.py <model_id> <tone>", file=sys.stderr)
        sys.exit(2)
    mid, tone = sys.argv[1], sys.argv[2]
    print(json.dumps(asyncio.run(probe(mid, tone)), ensure_ascii=False))


if __name__ == "__main__":
    main()
