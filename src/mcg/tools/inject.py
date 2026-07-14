from __future__ import annotations

import json

from mcg.compat.canonical import CanonicalTool


def build_tool_preamble(tools: list[CanonicalTool], strategies: list[str] | None = None) -> str:
    """Ephemeral tool instructions from the *current request only* (zero registry).

    Strategies inspired by cramt fenced / shell-routing research.
    """
    if not tools:
        return ""
    strategies = strategies or ["fenced", "shell_route"]
    lines = [
        "You may call tools. Tools available for THIS turn only:",
    ]
    for t in tools:
        schema = json.dumps(t.parameters or {}, ensure_ascii=False)
        lines.append(f"- {t.name}: {t.description or '(no description)'}")
        lines.append(f"  parameters_schema: {schema}")

    lines.append("")
    if "fenced" in strategies:
        lines.append(
            "When calling a tool, output a fenced block where the info-string is the tool name "
            "and the body is a single JSON object of arguments. Example:\n"
            "```tool_name\n"
            '{"arg": "value"}\n'
            "```"
        )
    if "shell_route" in strategies:
        shellish = [t.name for t in tools if any(k in t.name.lower() for k in ("bash", "shell", "run", "exec", "cmd"))]
        if shellish:
            lines.append(
                f"Preferred shell tools: {', '.join(shellish)}. "
                "For those, you may use ```bash with the command as body."
            )
    if "json" in strategies:
        lines.append(
            'Alternatively emit a single JSON line: '
            '{"tool_calls":[{"name":"...","arguments":{...}}]}'
        )
    lines.append("If no tool is needed, answer the user normally without tool fences.")
    return "\n".join(lines)
