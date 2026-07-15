from __future__ import annotations

import json
import time
import uuid
from typing import Any

from pydantic import BaseModel, Field

from .canonical import CanonicalMessage, CanonicalRequest, CanonicalTool


class AnthropicMessagesRequest(BaseModel):
    model: str = "m365-copilot"
    messages: list[dict[str, Any]]
    system: str | list[dict[str, Any]] | None = None
    max_tokens: int = 4096
    stream: bool = False
    tools: list[dict[str, Any]] | None = None
    tool_choice: Any = None
    temperature: float | None = None
    metadata: dict[str, Any] | None = None
    # sticky extensions
    conversation_id: str | None = None
    user: str | None = None
    reset_conversation: bool = False


def _system_text(system: str | list[dict[str, Any]] | None) -> str:
    if system is None:
        return ""
    if isinstance(system, str):
        return system
    parts = []
    for block in system:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text") or ""))
        elif isinstance(block, str):
            parts.append(block)
    return "\n".join(parts)


def _content_to_text(content: Any) -> tuple[str, list[dict[str, Any]]]:
    """Return (text, raw_parts_for_multimodal)."""
    if content is None:
        return "", []
    if isinstance(content, str):
        return content, []
    if not isinstance(content, list):
        return str(content), []
    texts: list[str] = []
    raw_parts: list[dict[str, Any]] = []
    for block in content:
        if not isinstance(block, dict):
            texts.append(str(block))
            continue
        btype = block.get("type")
        if btype == "text":
            texts.append(str(block.get("text") or ""))
        elif btype == "image":
            source = block.get("source") or {}
            if source.get("type") == "base64":
                mime = source.get("media_type") or "image/png"
                data = source.get("data") or ""
                raw_parts.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{mime};base64,{data}"},
                    }
                )
            elif source.get("type") == "url":
                raw_parts.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": source.get("url") or ""},
                    }
                )
        elif btype == "tool_result":
            texts.append(str(block.get("content") or ""))
        elif btype == "tool_use":
            # assistant tool use — handled at message level
            pass
        elif "text" in block:
            texts.append(str(block["text"]))
    return "\n".join(t for t in texts if t), raw_parts


def to_canonical(req: AnthropicMessagesRequest) -> CanonicalRequest:
    messages: list[CanonicalMessage] = []
    sys_t = _system_text(req.system)
    if sys_t:
        messages.append(CanonicalMessage(role="system", content=sys_t))

    # stash multimodal via extra for route layer
    media_parts: list[dict[str, Any]] = []

    for m in req.messages:
        role = m.get("role") or "user"
        if role not in ("user", "assistant"):
            role = "user"
        content = m.get("content")
        # assistant tool_use blocks
        if role == "assistant" and isinstance(content, list):
            tool_calls = []
            text_bits = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text":
                    text_bits.append(str(block.get("text") or ""))
                elif block.get("type") == "tool_use":
                    tool_calls.append(
                        {
                            "id": block.get("id") or f"call_{uuid.uuid4().hex[:12]}",
                            "type": "function",
                            "function": {
                                "name": block.get("name") or "tool",
                                "arguments": json.dumps(
                                    block.get("input") or {}, ensure_ascii=False
                                ),
                            },
                        }
                    )
            messages.append(
                CanonicalMessage(
                    role="assistant",
                    content="\n".join(text_bits),
                    tool_calls=tool_calls or None,
                )
            )
            continue
        # user tool_result blocks → tool role
        if role == "user" and isinstance(content, list) and any(
            isinstance(b, dict) and b.get("type") == "tool_result" for b in content
        ):
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_result":
                    c = block.get("content")
                    if isinstance(c, list):
                        c = " ".join(
                            str(x.get("text") if isinstance(x, dict) else x) for x in c
                        )
                    messages.append(
                        CanonicalMessage(
                            role="tool",
                            content=str(c or ""),
                            tool_call_id=str(block.get("tool_use_id") or ""),
                            name=str(block.get("name") or "tool"),
                        )
                    )
                elif block.get("type") == "text" and block.get("text"):
                    messages.append(
                        CanonicalMessage(role="user", content=str(block["text"]))
                    )
            continue

        text, parts = _content_to_text(content)
        media_parts.extend(parts)
        if parts:
            # keep OpenAI-shaped parts for multimodal adapter
            media_parts  # noqa: B018 — collected in extra
        messages.append(CanonicalMessage(role="user" if role == "user" else "assistant", content=text))

    tools: list[CanonicalTool] = []
    for t in req.tools or []:
        name = t.get("name")
        if not name:
            continue
        tools.append(
            CanonicalTool(
                name=name,
                description=t.get("description") or "",
                parameters=t.get("input_schema") or t.get("parameters") or {},
            )
        )

    user = req.user
    if not user and req.metadata:
        user = (req.metadata or {}).get("user_id")

    return CanonicalRequest(
        model=req.model,
        messages=messages,
        tools=tools,
        tool_choice=req.tool_choice,
        stream=req.stream,
        conversation_id=req.conversation_id,
        user=user,
        extra={
            "reset_conversation": req.reset_conversation,
            "anthropic_media_parts": media_parts,
            "max_tokens": req.max_tokens,
        },
    )


def final_anthropic_response(
    *,
    model: str,
    content: str,
    tool_calls: list[dict[str, Any]] | None = None,
    conversation_id: str | None = None,
) -> dict[str, Any]:
    blocks: list[dict[str, Any]] = []
    if content:
        blocks.append({"type": "text", "text": content})
    stop = "end_turn"
    if tool_calls:
        stop = "tool_use"
        for tc in tool_calls:
            fn = tc.get("function") or {}
            args = fn.get("arguments") or "{}"
            if isinstance(args, str):
                try:
                    inp = json.loads(args)
                except json.JSONDecodeError:
                    inp = {"raw": args}
            else:
                inp = args
            blocks.append(
                {
                    "type": "tool_use",
                    "id": tc.get("id") or f"toolu_{uuid.uuid4().hex[:20]}",
                    "name": fn.get("name") or "tool",
                    "input": inp,
                }
            )
    out: dict[str, Any] = {
        "id": f"msg_{uuid.uuid4().hex[:24]}",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": blocks or [{"type": "text", "text": ""}],
        "stop_reason": stop,
        "stop_sequence": None,
        "usage": {"input_tokens": 0, "output_tokens": 0},
    }
    if conversation_id:
        out["conversation_id"] = conversation_id
    return out


def anthropic_sse_events(
    *,
    model: str,
    text: str,
    tool_calls: list[dict[str, Any]] | None = None,
    conversation_id: str | None = None,
) -> list[str]:
    """Build Anthropic-style SSE event frames (non-token-true; one-shot after full gen)."""
    msg_id = f"msg_{uuid.uuid4().hex[:24]}"
    events: list[str] = []

    def pack(payload: dict[str, Any]) -> str:
        return f"event: {payload['type']}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"

    events.append(
        pack(
            {
                "type": "message_start",
                "message": {
                    "id": msg_id,
                    "type": "message",
                    "role": "assistant",
                    "model": model,
                    "content": [],
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 0, "output_tokens": 0},
                    **({"conversation_id": conversation_id} if conversation_id else {}),
                },
            }
        )
    )
    idx = 0
    if text:
        events.append(
            pack(
                {
                    "type": "content_block_start",
                    "index": idx,
                    "content_block": {"type": "text", "text": ""},
                }
            )
        )
        events.append(
            pack(
                {
                    "type": "content_block_delta",
                    "index": idx,
                    "delta": {"type": "text_delta", "text": text},
                }
            )
        )
        events.append(pack({"type": "content_block_stop", "index": idx}))
        idx += 1
    for tc in tool_calls or []:
        fn = tc.get("function") or {}
        args = fn.get("arguments") or "{}"
        if not isinstance(args, str):
            args = json.dumps(args, ensure_ascii=False)
        try:
            inp = json.loads(args)
        except json.JSONDecodeError:
            inp = {"raw": args}
        tid = tc.get("id") or f"toolu_{uuid.uuid4().hex[:16]}"
        events.append(
            pack(
                {
                    "type": "content_block_start",
                    "index": idx,
                    "content_block": {
                        "type": "tool_use",
                        "id": tid,
                        "name": fn.get("name") or "tool",
                        "input": {},
                    },
                }
            )
        )
        events.append(
            pack(
                {
                    "type": "content_block_delta",
                    "index": idx,
                    "delta": {
                        "type": "input_json_delta",
                        "partial_json": json.dumps(inp, ensure_ascii=False),
                    },
                }
            )
        )
        events.append(pack({"type": "content_block_stop", "index": idx}))
        idx += 1
    stop = "tool_use" if tool_calls else "end_turn"
    events.append(
        pack(
            {
                "type": "message_delta",
                "delta": {"stop_reason": stop, "stop_sequence": None},
                "usage": {"output_tokens": 0},
            }
        )
    )
    events.append(pack({"type": "message_stop"}))
    return events
