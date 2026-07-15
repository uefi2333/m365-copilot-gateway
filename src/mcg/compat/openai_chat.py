from __future__ import annotations

import json
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

from pydantic import BaseModel

from mcg.multimodal.adapter import extract_from_content, render_multimodal_prompt

from .canonical import CanonicalMessage, CanonicalRequest, CanonicalTool
from mcg.tools.platform_adapt import normalize_tools


class OpenAIChatRequest(BaseModel):
    model: str = "m365-copilot"
    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]] | None = None
    functions: list[dict[str, Any]] | None = None  # legacy OpenAI
    tool_choice: Any = None
    function_call: Any = None  # legacy
    stream: bool = False
    user: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    # sticky substrate conversation (optional OpenAI extension)
    conversation_id: str | None = None
    # force new substrate conversation even if sticky key exists
    reset_conversation: bool = False

    def model_post_init(self, __context: Any) -> None:
        # Merge legacy functions → tools
        if not self.tools and self.functions:
            converted = []
            for f in self.functions:
                if not isinstance(f, dict):
                    continue
                if f.get("type") == "function" or "function" in f:
                    converted.append(f)
                else:
                    converted.append({"type": "function", "function": f})
            object.__setattr__(self, "tools", converted)


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
    tools = normalize_tools(req.tools)
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


def estimate_tokens(text: str) -> int:
    """Rough OpenAI-compatible token estimate.

    Substrate never returns real token counts. Word-aware heuristic is better
    than pure char//4 for CJK/mixed text and keeps usage non-zero for auditors.
    """
    if not text:
        return 0
    words = [w for w in text.replace(chr(10), " ").split(" ") if w]
    if not words:
        return max(1, len(text) // 2)
    cjk = sum(1 for ch in text if "一" <= ch <= "鿿")
    latin_words = max(1, len(words) - cjk // 2)
    est = int(latin_words * 1.3 + cjk)
    return max(1, est)


def final_openai_response(
    *,
    model: str,
    content: str,
    tool_calls: list[dict[str, Any]] | None = None,
    finish_reason: str = "stop",
    conversation_id: str | None = None,
    usage: dict[str, int] | None = None,
) -> dict[str, Any]:
    msg: dict[str, Any] = {"role": "assistant", "content": content or None}
    if tool_calls:
        msg["tool_calls"] = tool_calls
        finish_reason = "tool_calls"
    if usage is None:
        pt = 0
        ct = estimate_tokens(content or "")
        usage = {
            "prompt_tokens": pt,
            "completion_tokens": ct,
            "total_tokens": pt + ct,
        }
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
        "usage": usage,
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
    usage: dict[str, int] | None = None,
) -> AsyncIterator[str]:
    """SSE chat.completion.chunk stream.

    Emits usage + timing (ttft_ms, speed_chars_per_sec) in the final data chunk.
    First content chunk carries ``timing.ttft_ms`` as an extension field.
    """
    cid = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    created = int(time.time())
    content_len = 0
    ttft_ms = 0

    def _make(
        delta: dict[str, Any],
        finish: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> str:
        body: dict[str, Any] = {
            "id": cid,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }
        if conversation_id:
            body["conversation_id"] = conversation_id
        if extra:
            body.update(extra)
        return "data: " + json.dumps(body, ensure_ascii=False) + "\n\n"

    t0 = time.perf_counter()
    yield _make({"role": "assistant", "content": ""})

    async for piece in text_iter:
        if piece:
            content_len += len(piece)
            if ttft_ms == 0:
                ttft_ms = int((time.perf_counter() - t0) * 1000)
                # first content chunk: include ttft in custom timing extension
                yield _make(
                    {"content": piece},
                    extra={"timing": {"ttft_ms": ttft_ms}},
                )
            else:
                yield _make({"content": piece})

    elapsed = time.perf_counter() - t0
    cps = round(content_len / elapsed, 1) if elapsed > 0 else 0.0

    timing_meta = {
        "ttft_ms": ttft_ms,
        "speed_chars_per_sec": cps,
        "output_chars": content_len,
        "elapsed_ms": int(elapsed * 1000),
    }

    usage_out = {}
    if usage is not None:
        est = max(1, content_len // 3)
        usage["completion_tokens"] = est
        usage["total_tokens"] = usage.get("prompt_tokens", 0) + est
        usage_out = dict(usage)

    finish_extra: dict[str, Any] = {}
    if usage_out:
        finish_extra["usage"] = usage_out
    if timing_meta:
        finish_extra["timing"] = timing_meta

    tools = list(tool_calls_holder or [])
    if tools:
        for frag in _openai_tool_call_deltas(tools):
            yield _make({"tool_calls": [frag]})
        # yield early finish block without blocking on slow math/dictionary merges
        yield _make({}, finish="tool_calls")
    else:
        yield _make({}, finish="stop")
    yield "data: [DONE]\n\n"

