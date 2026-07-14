from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from typing import Any

from mcg.compat.canonical import CanonicalRequest, CanonicalTool
from .inject import build_tool_preamble

FENCE_RE = re.compile(r"```([a-zA-Z0-9_\-\.]+)\s*\n(.*?)```", re.DOTALL)
JSON_TOOL_RE = re.compile(r'\{\s*"tool_calls"\s*:\s*\[.*?\]\s*\}', re.DOTALL)


@dataclass
class ParsedTools:
    text: str
    tool_calls: list[dict[str, Any]]


def parse_tool_calls_from_text(text: str, tools: list[CanonicalTool]) -> ParsedTools:
    """Parse model output into OpenAI-shaped tool_calls. Independent reimplementation."""
    names = {t.name for t in tools}
    shell_names = [n for n in names if any(k in n.lower() for k in ("bash", "shell", "run", "exec", "cmd"))]
    found: list[dict[str, Any]] = []
    residual = text

    for m in FENCE_RE.finditer(text):
        info = m.group(1).strip()
        body = m.group(2).strip()
        tool_name = None
        arguments: dict[str, Any]
        if info in names:
            tool_name = info
            try:
                arguments = json.loads(body) if body else {}
            except json.JSONDecodeError:
                arguments = {"input": body}
        elif info.lower() in ("bash", "sh", "shell", "zsh") and shell_names:
            tool_name = shell_names[0]
            arguments = {"command": body}
        else:
            continue
        found.append(
            {
                "id": f"call_{uuid.uuid4().hex[:20]}",
                "type": "function",
                "function": {
                    "name": tool_name,
                    "arguments": json.dumps(arguments, ensure_ascii=False),
                },
            }
        )
        residual = residual.replace(m.group(0), "")

    if not found:
        jm = JSON_TOOL_RE.search(text)
        if jm:
            try:
                payload = json.loads(jm.group(0))
                for item in payload.get("tool_calls") or []:
                    name = item.get("name")
                    if name not in names:
                        continue
                    args = item.get("arguments") or {}
                    if not isinstance(args, str):
                        args = json.dumps(args, ensure_ascii=False)
                    found.append(
                        {
                            "id": f"call_{uuid.uuid4().hex[:20]}",
                            "type": "function",
                            "function": {"name": name, "arguments": args},
                        }
                    )
                residual = residual.replace(jm.group(0), "")
            except json.JSONDecodeError:
                pass

    clean = residual.strip()
    return ParsedTools(text=clean, tool_calls=found)


class ToolLoop:
    def __init__(self, strategies: list[str] | None = None, max_rounds: int = 8) -> None:
        self.strategies = strategies or ["fenced", "shell_route"]
        self.max_rounds = max_rounds

    def augment_prompt(self, req: CanonicalRequest) -> str:
        base = req.prompt_text()
        preamble = build_tool_preamble(req.tools, self.strategies)
        if not preamble:
            return base
        return f"{preamble}\n\n---\n\n{base}"

    def parse(self, model_text: str, tools: list[CanonicalTool]) -> ParsedTools:
        if not tools:
            return ParsedTools(text=model_text, tool_calls=[])
        return parse_tool_calls_from_text(model_text, tools)
