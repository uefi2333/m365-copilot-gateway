from __future__ import annotations

import json
import time
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from mcg.auth.deps import require_api_key
from mcg.models_catalog import resolve_tone
from mcg.substrate.client import SubstrateClient, SubstrateError, is_transient_substrate_error

from .chat_format import format_openai_messages

router = APIRouter(prefix="/v1", tags=["chat"])


class ChatRequest(BaseModel):
    model: str = "m365-copilot"
    messages: list[dict[str, Any]] = Field(default_factory=list)
    stream: bool = False
    temperature: float | None = None
    user: str | None = None
    metadata: dict[str, Any] | None = None


def _chunk(completion_id: str, created: int, model: str, content: str = "", finish: str | None = None) -> dict[str, Any]:
    choice: dict[str, Any] = {"index": 0, "delta": {}, "finish_reason": finish}
    if content:
        choice["delta"] = {"content": content}
    return {"id": completion_id, "object": "chat.completion.chunk", "created": created, "model": model, "choices": [choice]}


def _sse(obj: dict[str, Any]) -> bytes:
    return ("data: " + json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n\n").encode()


@router.post("/chat/completions")
async def chat_completions(body: ChatRequest, request: Request, _key: str = Depends(require_api_key)):
    if not body.messages:
        raise HTTPException(status_code=400, detail="messages required")

    pool = request.app.state.pool
    cfg = request.app.state.config
    sessions = request.app.state.chat_sessions
    conv_id = None
    if body.metadata:
        conv_id = body.metadata.get("conversation_id") or body.metadata.get("thread_id")
    state = sessions.resolve(body.messages, explicit_id=str(conv_id) if conv_id else None)
    try:
        account = pool.acquire(sticky_key=state.conversation_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    tone = resolve_tone(body.model, request.app.state.models)
    text, custom_instructions = format_openai_messages(body.messages)
    is_start = state.sent_count == 0
    completion_id = f"chatcmpl-{uuid.uuid4().hex}"
    created = int(time.time())

    started = time.time()
    first_byte_at: float | None = None
    chars = 0

    def log_done(status: str, error: str = "") -> None:
        request.app.state.request_log.append({
            "ts": int(started),
            "status": status,
            "route": "/v1/chat/completions",
            "model": body.model,
            "tone": tone,
            "account": account.id,
            "stream": body.stream,
            "chars": chars,
            "ttfb_ms": int(((first_byte_at or time.time()) - started) * 1000),
            "total_ms": int((time.time() - started) * 1000),
            "error": error[:200],
        })
        del request.app.state.request_log[:-100]

    client = SubstrateClient(
        account.token,
        origin=cfg.substrate.origin,
        time_zone=cfg.substrate.time_zone,
        timeout_sec=cfg.substrate.request_timeout_sec,
    )

    async def upstream():
        nonlocal first_byte_at, chars
        try:
            async for part in client.chat_stream(
                text,
                tone=tone,
                conversation_id=state.conversation_id,
                session_id=state.session_id,
                is_start_of_session=is_start,
                custom_instructions=custom_instructions,
            ):
                if first_byte_at is None:
                    first_byte_at = time.time()
                chars += len(part)
                yield part
            state.sent_count = len(body.messages)
            pool.mark_success(account.id)
            log_done("ok")
        except Exception as exc:  # noqa: BLE001
            if is_transient_substrate_error(exc):
                pool.mark_soft_error(account.id)
            else:
                pool.mark_error(account.id)
            log_done("error", str(exc))
            raise

    if body.stream:
        async def gen():
            try:
                yield _sse(_chunk(completion_id, created, body.model, "", None))
                async for part in upstream():
                    if part:
                        yield _sse(_chunk(completion_id, created, body.model, part, None))
                yield _sse(_chunk(completion_id, created, body.model, "", "stop"))
                yield b"data: [DONE]\n\n"
            except SubstrateError as exc:
                err = {"error": {"message": str(exc), "type": "upstream_error", "code": "m365_substrate"}}
                yield ("data: " + json.dumps(err, ensure_ascii=False) + "\n\n").encode()
                yield b"data: [DONE]\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream")

    try:
        content = "".join([part async for part in upstream()])
    except SubstrateError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {
        "id": completion_id,
        "object": "chat.completion",
        "created": created,
        "model": body.model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "x_m365_tone": tone,
            "x_m365_account": account.id,
            "x_m365_ttfb_ms": int(((first_byte_at or time.time()) - started) * 1000),
            "x_m365_total_ms": int((time.time() - started) * 1000),
        },
    }
