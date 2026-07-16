from __future__ import annotations

from typing import Any


def message_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out: list[str] = []
        for part in content:
            if isinstance(part, dict):
                if part.get("type") in ("text", "input_text"):
                    out.append(str(part.get("text") or ""))
                elif part.get("type") in ("image_url", "input_image"):
                    out.append("[image omitted in P2]")
            else:
                out.append(str(part))
        return "\n".join(x for x in out if x)
    return str(content)


def format_openai_messages(messages: list[dict[str, Any]]) -> tuple[str, str | None]:
    system: list[str] = []
    body: list[str] = []
    for msg in messages:
        role = str(msg.get("role") or "user")
        text = message_text(msg.get("content"))
        if not text:
            continue
        if role == "system":
            system.append(text)
        elif role == "assistant":
            body.append(f"<assistant>\n{text}\n</assistant>")
        elif role == "tool":
            name = msg.get("name") or msg.get("tool_call_id") or "tool"
            body.append(f"<tool_response name=\"{name}\">\n{text}\n</tool_response>")
        else:
            body.append(f"<user>\n{text}\n</user>")
    return "\n\n".join(body).strip() or "Hello", "\n\n".join(system).strip() or None
