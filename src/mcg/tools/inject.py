from __future__ import annotations

import json

from mcg.compat.canonical import CanonicalRequest, CanonicalTool


def build_tool_preamble(
    tools: list[CanonicalTool],
    strategies: list[str] | None = None,
    *,
    has_tool_results: bool = False,
) -> str:
    """Ephemeral tool instructions from the *current request only* (zero registry).

    Strategies inspired by cramt fenced / shell-routing research.
    """
    if not tools and not has_tool_results:
        return ""
    strategies = strategies or ["fenced", "shell_route"]
    lines: list[str] = []

    if has_tool_results:
        lines.extend(
            [
                "Tool results for a previous tool call are included below as "
                "[tool_result ...] blocks.",
                "Use those results to answer the user NOW.",
                "Do NOT re-call the same tool unless the result is clearly insufficient.",
                "Prefer a normal natural-language final answer (no tool fences) when results are enough.",
                "",
            ]
        )

    if tools:
        lines.append("You may call tools. Tools available for THIS turn only:")
        for t in tools:
            schema = json.dumps(t.parameters or {}, ensure_ascii=False)
            lines.append(f"- {t.name}: {t.description or '(no description)'}")
            lines.append(f"  parameters_schema: {schema}")
        lines.append("")
        if "fenced" in strategies:
            lines.append(
                "When calling a tool, output ONLY a fenced block — no prose before or after. "
                "The info-string is the exact tool name; the body is a single JSON object of arguments. "
                "Example:\n"
                "```tool_name\n"
                '{"arg": "value"}\n'
                "```\n"
                "Do not write sentences like \"I need to use the tool\" or \"Clarifying tool usage\". "
                "Either call the tool with a fence, or answer the user in plain language."
            )
        if "shell_route" in strategies:
            shellish = [
                t.name
                for t in tools
                if any(k in t.name.lower() for k in ("bash", "shell", "run", "exec", "cmd"))
            ]
            if shellish:
                lines.append(
                    f"Preferred shell tools: {', '.join(shellish)}. "
                    "For those, you may use ```bash with the command as body; "
                    'arguments should use key "command".'
                )
        if "json" in strategies:
            lines.append(
                'Alternatively emit a single JSON line: '
                '{"tool_calls":[{"name":"...","arguments":{...}}]}'
            )
        lines.append("If no tool is needed, answer the user normally without tool fences.")

    return "\n".join(lines)


def request_has_tool_results(req: CanonicalRequest) -> bool:
    return any(m.role == "tool" for m in req.messages)
