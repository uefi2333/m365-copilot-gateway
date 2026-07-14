from __future__ import annotations

import json
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

from pydantic import BaseModel, Field

from .canonical import CanonicalMessage, CanonicalRequest, CanonicalTool


class OpenAIChatRequest(BaseModel):
    model: str = "m365-copilot"
    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]] | None = None
    tool_choice: Any = None
    stream: bool = False
    user: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None


def _extract_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts: list[str] = []
        for part in content:
            if isinstance(part, dict):
                if part.get("type") in ("text", "input_text") and part.get("text"):
                    texts.append(str(part["text"]))
                elif part.get("type") == "image_url":
                    url = (part.get("image_url") or {}).get("url") or ""
                    texts.append(f"[image:{url[:120]}]")
        return "\n".join(texts)
    return str(content)


def to_canonical(req: OpenAIChatRequest) -> CanonicalRequest:
    messages: list[CanonicalMessage] = []
    for m in req.messages:
        role = m.get("role") or "user"
        if role not in ("system", "user", "assistant", "tool"):
            role = "user"
        messages.append(
            CanonicalMessage(
                role=role,
                content=_extract_text(m.get("content")),
                name=m.get("name"),
                tool_call_id=m.get("tool_call_id"),
                tool_calls=m.get("tool_calls"),
            )
        )
    tools: list[CanonicalTool] = []
    for t in req.tools or []:
        fn = t.get("function") if t.get("type") == "function" else t
        if not isinstance(fn, dict):
            continue
        name = fn.get("name")
        if not name:
            continue
        tools.append(
            CanonicalTool(
                name=name,
                description=fn.get("description") or "",
                parameters=fn.get("parameters") or {},
            )
        )
    return CanonicalRequest(
        model=req.model,
        messages=messages,
        tools=tools,
        tool_choice=req.tool_choice,
        stream=req.stream,
        user=req.user,
    )


def final_openai_response(
    *,
    model: str,
    content: str,
    tool_calls: list[dict[str, Any]] | None = None,
    finish_reason: str = "stop",
) -> dict[str, Any]:
    msg: dict[str, Any] = {"role": "assistant", "content": content or None}
    if tool_calls:
        msg["tool_calls"] = tool_calls
        finish_reason = "tool_calls"
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": msg,
                "finish_reason": finish_reason,
            }
        ],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


async def stream_openai_chunks(
    *,
    model: str,
    text_iter: AsyncIterator[str],
    tool_calls: list[dict[str, Any]] | None = None,
) -> AsyncIterator[str]:
    cid = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    created = int(time.time())

    def pack(delta: dict[str, Any], finish: str | None = None) -> str:
        body = {
            "id": cid,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }
        return "data: " + json.dumps(body, ensure_ascii=False) + "\n\n"

    yield pack({"role": "assistant", "content": ""})
    async for piece in text_iter:
        if piece:
            yield pack({"content": piece})
    if tool_calls:
        yield pack({"tool_calls": tool_calls}, finish="tool_calls")
    else:
        yield pack({}, finish="stop")
    yield "data: [DONE]\n\n"
