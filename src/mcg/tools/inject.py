from __future__ import annotations

import json

from mcg.compat.canonical import CanonicalRequest, CanonicalTool
from mcg.tools.platform_adapt import platform_preamble_extra


def build_tool_preamble(
    tools: list[CanonicalTool],
    strategies: list[str] | None = None,
    *,
    has_tool_results: bool = False,
    agent_id: str | None = None,
) -> str:
    """Ephemeral tool instructions from the *current request only* (zero registry)."""
    if not tools and not has_tool_results:
        return ""
    strategies = strategies or ["fenced", "shell_route"]
    lines: list[str] = []

    if has_tool_results:
        lines.extend(
            [
                "Tool results: prior tool results are in [tool_result ...] blocks in the conversation.",
                "MULTI-HOP: if the user task is NOT finished, call the NEXT tool now.",
                "Examples: use_skill loaded a skill → follow its steps with Write/Bash/Read.",
                "Only answer in plain text when the whole task is done or no more tools apply.",
                "Do NOT re-call the exact same tool with the same args.",
                "",
            ]
        )

    if not tools:
        return "\n".join(lines)

    # Compact: reasoning models treat long preambles as analysis targets
    lines.append("TOOLS (this turn only). To act, emit ONE fence then stop.")
    for t in tools:
        props = (t.parameters or {}).get("properties") if isinstance(t.parameters, dict) else None
        req = (t.parameters or {}).get("required") if isinstance(t.parameters, dict) else None
        # compact: name + required keys only (smaller prompt → faster TTFT)
        keys = list(req) if isinstance(req, list) and req else (list(props.keys())[:6] if isinstance(props, dict) else [])
        desc = (t.description or "").strip().replace("\n", " ")[:80]
        lines.append(f"- {t.name}({','.join(keys)}): {desc}")

    # show one sample per tool so model does not always pick tools[0]
    lines.append("")
    lines.append("FORMAT (mandatory — no prose before/after). Pick the tool that matches user intent:")
    for t in tools[:6]:
        params = t.parameters if isinstance(t.parameters, dict) else {}
        req = list(params.get("required") or []) if isinstance(params.get("required"), list) else []
        sample = {k: f"<{k}>" for k in req} if req else {}
        sample_s = json.dumps(sample, ensure_ascii=False)
        lines.append(f"```{t.name}")
        lines.append(sample_s)
        lines.append("```")
    lines.append(
        "Rules:\n"
        "- info-string = exact tool name from the list; body = JSON args.\n"
        "- If user writes /name or names a tool, call THAT tool.\n"
        "- Fill required keys from user message / tool results. Do not pick an unrelated tool.\n"
        "- Never say you cannot call a listed tool. Never invent tools.\n"
        "- Never claim you lack filesystem/write access if Write/Bash/Read are listed.\n"
        "- Never write Clarifying/Thinking/Analysis. No Hide! / no meta.\n"
        "- /story-setup → use_skill{name:story-setup}, then execute skill steps with Write/Bash.\n"
        "- After [tool_result]: chain next tool if work remains; plain text only when done."
    )
    if tools:
        primary = tools[0].name
        params = tools[0].parameters if isinstance(tools[0].parameters, dict) else {}
        req = list(params.get("required") or []) if isinstance(params.get("required"), list) else []
        sample = {k: f"<{k}>" for k in req} if req else {}
        sample_s = json.dumps(sample, ensure_ascii=False)
        lines.append(
            'Alt: {"tool_calls":[{"name":"'
            + primary
            + '","arguments":'
            + sample_s
            + "}]}"
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
    extra = platform_preamble_extra(tools)
    if extra:
        lines.append(extra)
    if agent_id:
        try:
            from mcg.tools.agents import PROFILES, agent_preamble_extra
            prof = PROFILES.get(agent_id)
            if prof:
                lines.append(agent_preamble_extra(prof, tools))
        except Exception:
            pass
    return "\n".join(lines)


def request_has_tool_results(req: CanonicalRequest) -> bool:
    return any(m.role == "tool" for m in req.messages)
