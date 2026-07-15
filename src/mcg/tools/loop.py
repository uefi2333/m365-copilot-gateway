from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from typing import Any

from mcg.compat.canonical import CanonicalRequest, CanonicalTool
from .inject import build_tool_preamble, request_has_tool_results

FENCE_RE = re.compile(r"```([a-zA-Z0-9_\-\.]+)\s*\n(.*?)```", re.DOTALL)
JSON_TOOL_RE = re.compile(r'\{\s*"tool_calls"\s*:\s*\[.*?\]\s*\}', re.DOTALL)

_SHELL_FENCE_LANGS = frozenset({"bash", "sh", "shell", "zsh", "cmd", "powershell", "ps1"})
_SHELL_NAME_HINTS = ("bash", "shell", "run", "exec", "cmd", "terminal", "powershell")


@dataclass
class ParsedTools:
    text: str
    tool_calls: list[dict[str, Any]]


def _is_shell_tool_name(name: str) -> bool:
    n = name.lower()
    return any(k in n for k in _SHELL_NAME_HINTS)


def _shell_tool_names(names: set[str]) -> list[str]:
    return [n for n in names if _is_shell_tool_name(n)]


def _args_for_fence_body(tool_name: str, body: str) -> dict[str, Any]:
    """Map fence body → function.arguments dict.

    Shell tools always prefer ``command`` (OpenAI / agent convention).
    Non-shell: try JSON object, else ``input``.
    """
    body = body.strip()
    if not body:
        return {"command": ""} if _is_shell_tool_name(tool_name) else {}

    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        parsed = None

    if isinstance(parsed, dict):
        if _is_shell_tool_name(tool_name):
            return _normalize_shell_args(parsed)
        return parsed
    if isinstance(parsed, str) and _is_shell_tool_name(tool_name):
        return {"command": parsed}

    if _is_shell_tool_name(tool_name):
        return {"command": body}
    return {"input": body}


def _normalize_shell_args(args: dict[str, Any]) -> dict[str, Any]:
    if "command" in args and args["command"] is not None:
        out = dict(args)
        out["command"] = str(out["command"])
        return out
    for key in ("input", "cmd", "script", "code"):
        if key in args and args[key] is not None:
            out = {k: v for k, v in args.items() if k != key}
            out["command"] = str(args[key])
            return out
    # single-key freeform
    if len(args) == 1:
        k, v = next(iter(args.items()))
        if isinstance(v, str):
            return {"command": v}
    return args


def _make_call(name: str, arguments: dict[str, Any] | str) -> dict[str, Any]:
    if isinstance(arguments, dict):
        if _is_shell_tool_name(name):
            arguments = _normalize_shell_args(arguments)
        args_s = json.dumps(arguments, ensure_ascii=False)
    else:
        args_s = arguments
    return {
        "id": f"call_{uuid.uuid4().hex[:20]}",
        "type": "function",
        "function": {"name": name, "arguments": args_s},
    }


def parse_tool_calls_from_text(text: str, tools: list[CanonicalTool]) -> ParsedTools:
    """Parse model output into OpenAI-shaped tool_calls. Independent reimplementation."""
    names = {t.name for t in tools}
    shell_names = _shell_tool_names(names)
    found: list[dict[str, Any]] = []
    residual = text

    for m in FENCE_RE.finditer(text):
        info = m.group(1).strip()
        body = m.group(2).strip()
        tool_name: str | None = None

        if info in names:
            # Named fence: ```bash``` when tool is also called bash → shell args
            tool_name = info
        elif info.lower() in _SHELL_FENCE_LANGS and shell_names:
            tool_name = shell_names[0]
        else:
            continue

        arguments = _args_for_fence_body(tool_name, body)
        found.append(_make_call(tool_name, arguments))
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
                    if isinstance(args, str):
                        try:
                            args_obj = json.loads(args)
                        except json.JSONDecodeError:
                            args_obj = (
                                {"command": args}
                                if _is_shell_tool_name(name)
                                else {"input": args}
                            )
                    elif isinstance(args, dict):
                        args_obj = args
                    else:
                        args_obj = {"input": str(args)}
                    if _is_shell_tool_name(name) and isinstance(args_obj, dict):
                        args_obj = _normalize_shell_args(args_obj)
                    found.append(_make_call(name, args_obj))
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
        preamble = build_tool_preamble(
            req.tools,
            self.strategies,
            has_tool_results=request_has_tool_results(req),
        )
        if not preamble:
            return base
        return f"{preamble}\n\n---\n\n{base}"

    def parse(self, model_text: str, tools: list[CanonicalTool]) -> ParsedTools:
        if not tools:
            return ParsedTools(text=model_text, tool_calls=[])
        return parse_tool_calls_from_text(model_text, tools)
