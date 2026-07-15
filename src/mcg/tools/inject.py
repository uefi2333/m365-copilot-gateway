from __future__ import annotations

import json

from mcg.compat.canonical import CanonicalRequest, CanonicalTool


def build_tool_preamble(
    tools: list[CanonicalTool],
    strategies: list[str] | None = None,
    *,
    has_tool_results: bool = False,
) -> str:
    """Ephemeral tool instructions from the *current request only* (zero registry)."""
    if not tools and not has_tool_results:
        return ""
    strategies = strategies or ["fenced", "shell_route"]
    lines: list[str] = []

    if has_tool_results:
        lines.extend(
            [
                "Tool results are in [tool_result ...] blocks below. Answer the user now.",
                "Do not re-call the same tool unless results are insufficient.",
                "",
            ]
        )

    if not tools:
        return "\n".join(lines)

    # Compact: reasoning models treat long preambles as analysis targets
    lines.append("TOOLS (this turn only). To act, emit ONE fence then stop.")
    for t in tools:
        schema = json.dumps(t.parameters or {}, ensure_ascii=False, separators=(",", ":"))
        desc = (t.description or "").strip().replace("\n", " ")[:160]
        lines.append(f"- {t.name}: {desc}")
        lines.append(f"  schema:{schema}")

    primary = tools[0].name
    lines.append("")
    lines.append(
        "FORMAT (mandatory — no prose before/after):\n"
        f"```{primary}\n"
        "{}\n"
        "```\n"
        "Rules:\n"
        "- info-string = exact tool name; body = JSON object of args (may be {}).\n"
        "- Never say you cannot call a tool that is listed.\n"
        "- Never write Clarifying/Thinking/Analysis sections.\n"
        "- No Hide! / no meta commentary."
    )
    if "json" in strategies or True:
        lines.append(
            'Alt: {"tool_calls":[{"name":"'
            + primary
            + '","arguments":{}}]}'
        )
    if "shell_route" in strategies:
        shellish = [
            t.name
            for t in tools
            if any(k in t.name.lower() for k in ("bash", "shell", "run", "exec", "cmd"))
        ]
        if shellish:
            lines.append(
                f"Shell tools ({', '.join(shellish)}): ```bash body = command; "
                'or JSON key "command".'
            )
    return "\n".join(lines)


def request_has_tool_results(req: CanonicalRequest) -> bool:
    return any(m.role == "tool" for m in req.messages)
