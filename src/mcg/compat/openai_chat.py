from __future__ import annotations

import json
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

from pydantic import BaseModel

from mcg.multimodal.adapter import extract_from_content, render_multimodal_prompt

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
    # sticky substrate conversation (optional OpenAI extension)
    conversation_id: str | None = None
    # force new substrate conversation even if sticky key exists
    reset_conversation: bool = False


def _extract_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    bundle = extract_from_content(content)
    if bundle.parts:
        return render_multimodal_prompt(bundle.text, bundle.parts)
    return bundle.text


def to_canonical(req: OpenAIChatRequest) -> CanonicalRequest:
    messages: list[CanonicalMessage] = []
    all_parts = []
    for m in req.messages:
        role = m.get("role") or "user"
        if role not in ("system", "user", "assistant", "tool"):
            role = "user"
        raw = m.get("content")
        if role in ("user", "system"):
            bundle = extract_from_content(raw)
            if bundle.parts:
                all_parts.extend(bundle.parts)
                content = render_multimodal_prompt(bundle.text, bundle.parts)
            else:
                content = bundle.text
        elif role == "assistant":
            if isinstance(raw, str) or raw is None:
                content = raw or ""
            else:
                content = extract_from_content(raw).text
        else:  # tool
            content = raw if isinstance(raw, str) else _extract_text(raw)
        messages.append(
            CanonicalMessage(
                role=role,
                content=content or "",
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
        conversation_id=req.conversation_id,
        user=req.user,
        extra={
            "reset_conversation": req.reset_conversation,
            "multimodal_parts": all_parts,
        },
    )


def final_openai_response(
    *,
    model: str,
    content: str,
    tool_calls: list[dict[str, Any]] | None = None,
    finish_reason: str = "stop",
    conversation_id: str | None = None,
) -> dict[str, Any]:
    msg: dict[str, Any] = {"role": "assistant", "content": content or None}
    if tool_calls:
        msg["tool_calls"] = tool_calls
        finish_reason = "tool_calls"
    out: dict[str, Any] = {
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
    if conversation_id:
        out["conversation_id"] = conversation_id
    return out


def _openai_tool_call_deltas(tool_calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Expand full tool_calls into OpenAI streaming delta fragments."""
    deltas: list[dict[str, Any]] = []
    for i, tc in enumerate(tool_calls):
        fn = tc.get("function") or {}
        deltas.append(
            {
                "index": i,
                "id": tc.get("id") or f"call_{uuid.uuid4().hex[:20]}",
                "type": tc.get("type") or "function",
                "function": {"name": fn.get("name") or "", "arguments": ""},
            }
        )
        args = fn.get("arguments") or ""
        if not isinstance(args, str):
            args = json.dumps(args, ensure_ascii=False)
        if args:
            deltas.append(
                {
                    "index": i,
                    "function": {"arguments": args},
                }
            )
    return deltas


async def stream_openai_chunks(
    *,
    model: str,
    text_iter: AsyncIterator[str],
    tool_calls_holder: list[dict[str, Any]] | None = None,
    conversation_id: str | None = None,
) -> AsyncIterator[str]:
    """SSE chat.completion.chunk stream."""
    cid = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    created = int(time.time())

    def pack(delta: dict[str, Any], finish: str | None = None) -> str:
        body: dict[str, Any] = {
            "id": cid,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }
        if conversation_id:
            body["conversation_id"] = conversation_id
        return "data: " + json.dumps(body, ensure_ascii=False) + "\n\n"

    yield pack({"role": "assistant", "content": ""})
    async for piece in text_iter:
        if piece:
            yield pack({"content": piece})

    tools = list(tool_calls_holder or [])
    if tools:
        for frag in _openai_tool_call_deltas(tools):
            yield pack({"tool_calls": [frag]})
        yield pack({}, finish="tool_calls")
    else:
        yield pack({}, finish="stop")
    yield "data: [DONE]\n\n"
