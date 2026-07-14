from __future__ import annotations

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
        """Flatten messages into a single user-facing prompt for Substrate."""
        parts: list[str] = []
        for m in self.messages:
            if m.role == "system" and m.content:
                parts.append(f"[system]\n{m.content}")
            elif m.role == "user" and m.content:
                parts.append(m.content)
            elif m.role == "assistant" and m.content:
                parts.append(f"[assistant]\n{m.content}")
            elif m.role == "tool":
                name = m.name or "tool"
                parts.append(f"[tool:{name}]\n{m.content}")
        return "\n\n".join(parts) if parts else ""
