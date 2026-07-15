"""One-shot tool-call repair when the model narrates instead of calling tools."""

from __future__ import annotations

from typing import Any

from mcg.compat.canonical import CanonicalMessage, CanonicalRequest, CanonicalTool
from mcg.tools.loop import ToolLoop, ParsedTools


_NARRATE_HINTS = (
    "reading relevant skills",
    "i need to review the available skills",
    "no tool is available",
    "tools available",
    "clarifying tool usage",
    "i need to use the",
    "let me use the tool",
    "tool the user requested isn't available",
    "isn't available in the current",
    "not available",
    "cannot use tools",
    "api_tool_skills",
    "docx",
    "spreadsheets",
)


def looks_like_failed_tool_turn(text: str, tools: list[CanonicalTool], user_wants_tool: bool) -> bool:
    if not tools or not text:
        return False
    low = text.lower()
    if any(h in low for h in _NARRATE_HINTS):
        return True
    # model mentioned a tool name but parser failed (malformed fence / glued prefix)
    if any(t.name.lower() in low for t in tools) and user_wants_tool:
        return True
    if user_wants_tool and not any(t.name.lower() in low for t in tools):
        return True
    return False


def user_requests_tool(req: CanonicalRequest) -> bool:
    blob = " ".join(
        (m.content or "")
        for m in req.messages
        if m.role in ("user", "system") and isinstance(m.content, str)
    ).lower()
    keys = (
        "use the tool",
        "call the tool",
        "tool",
        "skill",
        "/story",
        "read the file",
        "读取",
        "工具",
        "技能",
        "```",
    )
    return any(k in blob for k in keys)


def build_repair_prompt(tools: list[CanonicalTool], previous: str) -> str:
    names = ", ".join(t.name for t in tools)
    return (
        "Your previous reply did NOT call a tool correctly. "
        "Do NOT explain. Do NOT list fake skills. "
        f"Available tool names ONLY: {names}. "
        "Output a single tool call now using a fenced block:\n"
        "```EXACT_TOOL_NAME\n"
        '{"arg":"value"}\n'
        "```\n"
        "Or JSON: "
        '{"tool_calls":[{"name":"EXACT_TOOL_NAME","arguments":{...}}]}\n'
        f"Previous invalid reply (do not repeat):\n{previous[:800]}"
    )


async def maybe_repair_tool_call(
    *,
    client: Any,
    tool_loop: ToolLoop,
    canon: CanonicalRequest,
    stream_kwargs: dict[str, Any],
    full_text: str,
    repair_rounds: int,
) -> ParsedTools:
    """If model failed to emit parseable tool_calls, force one repair turn."""
    parsed = tool_loop.parse(full_text, canon.tools)
    if parsed.tool_calls or not canon.tools or repair_rounds <= 0:
        return parsed
    if not looks_like_failed_tool_turn(
        full_text, canon.tools, user_requests_tool(canon)
    ):
        return parsed

    rounds = 0
    text = full_text
    while rounds < repair_rounds and not parsed.tool_calls:
        rounds += 1
        repair = build_repair_prompt(canon.tools, text)
        # ephemeral user nudge — not persisted as real chat history for client
        kwargs = dict(stream_kwargs)
        kwargs["is_start_of_session"] = False
        kwargs["message_extras"] = None
        text = await client.chat(repair, **kwargs)
        parsed = tool_loop.parse(text, canon.tools)
    return parsed
