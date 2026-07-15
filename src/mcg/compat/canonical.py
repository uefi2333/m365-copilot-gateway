from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, Field


class CanonicalTool(BaseModel):
    name: str
    description: str = ""
    parameters: dict[str, Any] = Field(default_factory=dict)


class CanonicalMessage(BaseModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str = ""
    name: str | None = None
    tool_call_id: str | None = None
    tool_calls: list[dict[str, Any]] | None = None


class CanonicalRequest(BaseModel):
    model: str = "m365-copilot"
    messages: list[CanonicalMessage]
    tools: list[CanonicalTool] = Field(default_factory=list)
    tool_choice: str | dict[str, Any] | None = None
    stream: bool = False
    conversation_id: str | None = None
    user: str | None = None
    extra: dict[str, Any] = Field(default_factory=dict)

    def prompt_text(self) -> str:
        """Flatten messages into a single user-facing prompt for Substrate.

        OpenAI multi-turn tool loop shape is preserved as labeled blocks so the
        model can see prior tool_calls + tool results on the next hop.
        """
        parts: list[str] = []
        for m in self.messages:
            if m.role == "system" and m.content:
                parts.append(f"[system]\n{m.content}")
            elif m.role == "user" and m.content:
                parts.append(m.content)
            elif m.role == "assistant":
                block = self._format_assistant(m)
                if block:
                    parts.append(block)
            elif m.role == "tool":
                parts.append(self._format_tool_result(m))
        return "\n\n".join(parts) if parts else ""

    @staticmethod
    def _format_assistant(m: CanonicalMessage) -> str:
        chunks: list[str] = []
        if m.content:
            chunks.append(m.content)
        if m.tool_calls:
            # compact OpenAI-like summary the model already produced
            simplified = []
            for tc in m.tool_calls:
                fn = tc.get("function") or {}
                name = fn.get("name") or "unknown"
                args = fn.get("arguments") or "{}"
                if not isinstance(args, str):
                    args = json.dumps(args, ensure_ascii=False)
                simplified.append(
                    {
                        "id": tc.get("id"),
                        "name": name,
                        "arguments": args,
                    }
                )
            chunks.append(
                "[assistant_tool_calls]\n"
                + json.dumps(simplified, ensure_ascii=False, indent=2)
            )
        if not chunks:
            return ""
        return "[assistant]\n" + "\n".join(chunks)

    @staticmethod
    def _format_tool_result(m: CanonicalMessage) -> str:
        name = m.name or "tool"
        tid = m.tool_call_id or ""
        header = f"[tool_result name={name}"
        if tid:
            header += f" tool_call_id={tid}"
        header += "]"
        body = m.content if m.content is not None else ""
        return f"{header}\n{body}"
