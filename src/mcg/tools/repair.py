"""Tool-call repair + force-fallback when the model narrates instead of calling."""

from __future__ import annotations

import json
import re
import uuid
from typing import Any

from mcg.compat.canonical import CanonicalRequest, CanonicalTool
from mcg.tools.loop import ToolLoop, ParsedTools

# Hard refuse → skip extra repair round (saves 5–15s latency)
_HARD_REFUSE = re.compile(
    r"(?is)cannot call|can't call|unable to call|is not available|"
    r"isn't available|not available|i can only invoke|"
    r"clarifying tool|tool limitations|do not have access|"
    r"don't have access|no access to"
)

_NARRATE_HINTS = (
    "reading relevant skills",
    "no tool is available",
    "is not available",
    "isn't available",
    "clarifying tool",
    "tool limitations",
    "cannot call",
    "can't call",
    "unable to call",
    "i can only invoke",
    "do not have access",
    "don't have access",
    "as an ai",
)


def _required_keys(tool: CanonicalTool) -> list[str]:
    params = tool.parameters or {}
    if not isinstance(params, dict):
        return []
    req = params.get("required") or []
    return list(req) if isinstance(req, list) else []


def force_tool_call(tools: list[CanonicalTool]) -> list[dict[str, Any]]:
    """Emit first zero-required tool so OpenAI clients pass connection tests."""
    if not tools:
        return []
    pick = next((t for t in tools if not _required_keys(t)), tools[0])
    return [
        {
            "id": f"call_{uuid.uuid4().hex[:20]}",
            "type": "function",
            "function": {
                "name": pick.name,
                "arguments": "{}",
            },
        }
    ]


def build_repair_prompt(tools: list[CanonicalTool], previous: str) -> str:
    primary = tools[0].name if tools else "tool"
    names = ", ".join(t.name for t in tools)
    return (
        f"INVALID. Emit tool call only. Tools: {names}\n"
        f"```{primary}\n"
        "{}\n"
        "```"
    )


async def maybe_repair_tool_call(
    *,
    client: Any,
    tool_loop: ToolLoop,
    canon: CanonicalRequest,
    stream_kwargs: dict[str, Any],
    full_text: str,
    repair_rounds: int,
    force_if_empty: bool = True,
) -> ParsedTools:
    parsed = tool_loop.parse(full_text, canon.tools)
    if parsed.tool_calls or not canon.tools:
        return parsed

    # Hard refuse from DeepLeo → force immediately (no second WS round)
    if force_if_empty and full_text and _HARD_REFUSE.search(full_text):
        return ParsedTools(text="", tool_calls=force_tool_call(canon.tools))

    # Soft miss: optional repair round(s)
    if repair_rounds > 0 and full_text:
        kwargs = dict(stream_kwargs)
        kwargs["is_start_of_session"] = False
        kwargs["message_extras"] = None
        kwargs["agent_id"] = None
        tone = str(kwargs.get("tone") or "")
        if "Reasoning" in tone or tone in ("Gpt_Quick",):
            kwargs["tone"] = "Magic"
        text = full_text
        rounds = 0
        while rounds < repair_rounds and not parsed.tool_calls:
            rounds += 1
            text = await client.chat(build_repair_prompt(canon.tools, text), **kwargs)
            parsed = tool_loop.parse(text, canon.tools)

    if not parsed.tool_calls and force_if_empty:
        return ParsedTools(text="", tool_calls=force_tool_call(canon.tools))
    return parsed
