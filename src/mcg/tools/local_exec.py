from __future__ import annotations

import asyncio
import json
import os
import shlex
from dataclasses import dataclass
from typing import Any


@dataclass
class ExecResult:
    ok: bool
    name: str
    tool_call_id: str
    content: str
    exit_code: int | None = None


class LocalToolRunner:
    """Server-side tool execution for shell-like tools.

    Safety defaults: timeout, max output, optional allowlist prefixes.
    """

    def __init__(
        self,
        *,
        enabled: bool = False,
        timeout_sec: float = 30.0,
        max_output_bytes: int = 32_000,
        cwd: str | None = None,
        shell: bool = True,
        allow_names: list[str] | None = None,
        deny_patterns: list[str] | None = None,
    ) -> None:
        self.enabled = enabled
        self.timeout_sec = timeout_sec
        self.max_output_bytes = max_output_bytes
        self.cwd = cwd or os.getcwd()
        self.shell = shell
        self.allow_names = {n.lower() for n in (allow_names or [])}
        self.deny_patterns = deny_patterns or [
            "rm -rf /",
            "mkfs",
            ":(){",
            "shutdown",
            "reboot",
        ]

    def _name_allowed(self, name: str) -> bool:
        if not self.allow_names:
            # default: only shell-ish names
            n = name.lower()
            return any(k in n for k in ("bash", "shell", "run", "exec", "cmd", "terminal"))
        return name.lower() in self.allow_names

    def _command_from_args(self, arguments: str | dict[str, Any]) -> str:
        if isinstance(arguments, str):
            try:
                obj = json.loads(arguments)
            except json.JSONDecodeError:
                return arguments
        else:
            obj = arguments
        if isinstance(obj, dict):
            for k in ("command", "cmd", "script", "input", "code"):
                if obj.get(k) is not None:
                    return str(obj[k])
            return json.dumps(obj, ensure_ascii=False)
        return str(obj)

    def _denied(self, cmd: str) -> str | None:
        low = cmd.lower()
        for p in self.deny_patterns:
            if p.lower() in low:
                return f"denied pattern: {p}"
        return None

    async def run_one(self, tool_call: dict[str, Any]) -> ExecResult:
        tid = str(tool_call.get("id") or "")
        fn = tool_call.get("function") or {}
        name = str(fn.get("name") or "tool")
        if not self.enabled:
            return ExecResult(False, name, tid, "local execution disabled (tools.execution!=local)")
        if not self._name_allowed(name):
            return ExecResult(
                False,
                name,
                tid,
                f"tool '{name}' not allowed for local execution",
            )
        cmd = self._command_from_args(fn.get("arguments") or "{}")
        if not cmd.strip():
            return ExecResult(False, name, tid, "empty command")
        bad = self._denied(cmd)
        if bad:
            return ExecResult(False, name, tid, bad, exit_code=126)

        try:
            if self.shell:
                proc = await asyncio.create_subprocess_shell(
                    cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    cwd=self.cwd,
                )
            else:
                args = shlex.split(cmd)
                proc = await asyncio.create_subprocess_exec(
                    *args,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    cwd=self.cwd,
                )
            try:
                out_b, _ = await asyncio.wait_for(
                    proc.communicate(), timeout=self.timeout_sec
                )
            except asyncio.TimeoutError:
                proc.kill()
                await proc.communicate()
                return ExecResult(
                    False,
                    name,
                    tid,
                    f"timeout after {self.timeout_sec}s",
                    exit_code=124,
                )
            out = (out_b or b"").decode("utf-8", errors="replace")
            if len(out.encode("utf-8", errors="replace")) > self.max_output_bytes:
                out = out[: self.max_output_bytes] + "\n…[truncated]"
            code = proc.returncode
            header = f"exit_code={code}\n"
            return ExecResult(code == 0, name, tid, header + out, exit_code=code)
        except Exception as exc:  # noqa: BLE001
            return ExecResult(False, name, tid, f"exec error: {exc}")

    async def run_all(self, tool_calls: list[dict[str, Any]]) -> list[ExecResult]:
        return [await self.run_one(tc) for tc in tool_calls]

    @staticmethod
    def as_tool_messages(results: list[ExecResult]) -> list[dict[str, Any]]:
        msgs: list[dict[str, Any]] = []
        for r in results:
            msgs.append(
                {
                    "role": "tool",
                    "name": r.name,
                    "tool_call_id": r.tool_call_id,
                    "content": r.content,
                }
            )
        return msgs
