#!/usr/bin/env python3
"""Client-side multi-turn tool loop smoke against a live MCG gateway.

Round 1: force a bash tool call.
Round 2: feed OpenAI-shaped tool result; expect final natural answer (no re-call).
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text())
    key = cfg["gateway"]["api_keys"][0]
    base = f"http://{cfg['gateway']['host']}:{cfg['gateway']['port']}"

    def post(body: dict) -> dict:
        req = urllib.request.Request(
            base + "/v1/chat/completions",
            data=json.dumps(body).encode(),
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            detail = e.read().decode()[:800]
            raise SystemExit(f"HTTP {e.code}: {detail}") from e

    tools = [
        {
            "type": "function",
            "function": {
                "name": "bash",
                "description": "Run a shell command and return stdout",
                "parameters": {
                    "type": "object",
                    "properties": {"command": {"type": "string"}},
                    "required": ["command"],
                },
            },
        }
    ]

    secret = "MCG-LOOP-OK-77"
    user = "tool-loop-smoke"

    # --- round 1: request tool call ---
    r1_messages = [
        {
            "role": "user",
            "content": (
                "You MUST call the bash tool exactly once to run:\n"
                f"echo {secret}\n"
                "Output only a bash fence, no other text."
            ),
        }
    ]
    print("=== ROUND 1: expect tool_calls ===")
    b1 = post(
        {
            "model": "m365-copilot",
            "user": user,
            "messages": r1_messages,
            "tools": tools,
            "stream": False,
        }
    )
    msg1 = b1["choices"][0]["message"]
    fr1 = b1["choices"][0]["finish_reason"]
    tcs = msg1.get("tool_calls") or []
    print("finish:", fr1)
    print("content:", repr(msg1.get("content")))
    print("tool_calls:", json.dumps(tcs, ensure_ascii=False, indent=2))
    if fr1 != "tool_calls" or not tcs:
        print("FAIL: round1 did not return tool_calls")
        return 1
    tc = tcs[0]
    fn = tc.get("function") or {}
    if fn.get("name") != "bash":
        print("FAIL: expected bash")
        return 1
    args = fn.get("arguments") or "{}"
    try:
        args_obj = json.loads(args) if isinstance(args, str) else args
    except json.JSONDecodeError:
        args_obj = {}
    cmd = str(args_obj.get("command") or args_obj.get("input") or "")
    if secret not in cmd and "echo" not in cmd:
        print("WARN: command unexpected:", args_obj)

    # client "runs" the tool
    tool_stdout = f"{secret}\n"

    # --- round 2: feed tool result ---
    r2_messages = [
        r1_messages[0],
        {
            "role": "assistant",
            "content": msg1.get("content") or "",
            "tool_calls": tcs,
        },
        {
            "role": "tool",
            "tool_call_id": tc.get("id") or "call_unknown",
            "name": "bash",
            "content": tool_stdout,
        },
        {
            "role": "user",
            "content": (
                "Using the tool result above, reply with exactly one line:\n"
                f"CODE={secret}\n"
                "Do not call tools again."
            ),
        },
    ]
    print("\n=== ROUND 2: expect final answer using tool result ===")
    b2 = post(
        {
            "model": "m365-copilot",
            "user": user,
            "messages": r2_messages,
            "tools": tools,
            "stream": False,
        }
    )
    msg2 = b2["choices"][0]["message"]
    fr2 = b2["choices"][0]["finish_reason"]
    content2 = msg2.get("content") or ""
    tcs2 = msg2.get("tool_calls") or []
    print("finish:", fr2)
    print("content:", repr(content2)[:500])
    print("tool_calls:", tcs2)
    print("conversation_id r1/r2:", b1.get("conversation_id"), b2.get("conversation_id"))

    ok = True
    if tcs2:
        print("FAIL: round2 still returned tool_calls (should final-answer)")
        ok = False
    if secret not in content2 and f"CODE={secret}" not in content2:
        # soft: model may paraphrase
        if secret.split("-")[-1] not in content2:
            print("FAIL: round2 answer missing secret")
            ok = False
        else:
            print("SOFT: secret fragment present")
    if fr2 not in ("stop", None):
        print("WARN: finish_reason", fr2)

    if ok:
        print("\nSMOKE_TOOL_LOOP_OK")
        return 0
    print("\nSMOKE_TOOL_LOOP_FAIL")
    return 2


if __name__ == "__main__":
    sys.exit(main())
