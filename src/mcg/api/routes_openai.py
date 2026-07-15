from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from mcg.auth.deps import require_api_key
from mcg.compat.openai_chat import (
    OpenAIChatRequest,
    final_openai_response,
    stream_openai_chunks,
    to_canonical,
)
from mcg.models_catalog import resolve_tone
from mcg.substrate.client import SubstrateClient, SubstrateError

router = APIRouter()


def _log(request: Request, entry: dict[str, Any]) -> None:
    buf = request.app.state.request_log
    buf.append(entry)
    if len(buf) > 200:
        del buf[: len(buf) - 200]


def _sticky_key(body: OpenAIChatRequest, account_id: str) -> str:
    if body.conversation_id:
        return f"c:{body.conversation_id}"
    if body.user:
        return f"u:{body.user}:{account_id}"
    return f"a:{account_id}"


@router.get("/v1/models")
async def list_models(request: Request, _key: str = Depends(require_api_key)):
    models = request.app.state.models
    return {
        "object": "list",
        "data": [
            {
                "id": m.id,
                "object": "model",
                "created": 0,
                "owned_by": "m365-copilot-gateway",
                "root": m.tone,
                "permission": [],
                "metadata": {"tone": m.tone, "label": m.label, "family": m.family},
            }
            for m in models
        ],
    }


@router.post("/v1/chat/completions")
async def chat_completions(
    body: OpenAIChatRequest,
    request: Request,
    _key: str = Depends(require_api_key),
):
    cfg = request.app.state.config
    pool = request.app.state.pool
    tool_loop = request.app.state.tool_loop
    models = request.app.state.models
    sessions = request.app.state.sessions

    try:
        account = pool.acquire(sticky_key=body.user)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    fabric = request.app.state.fabric
    try:
        live = await fabric.ensure(
            account.id,
            fallback_token=account.token,
            allow_cdp=cfg.token.prefer_cdp,
            profile_path=account.profile_path or None,
        )
        if live != account.token:
            pool.refresh_token(account.id, live)
            account = pool.accounts[account.id]
    except RuntimeError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc

    canon = to_canonical(body)
    tone = resolve_tone(canon.model, models)
    prompt = tool_loop.augment_prompt(canon)

    sticky = _sticky_key(body, account.id)
    force_new = bool(body.reset_conversation or canon.extra.get("reset_conversation"))
    sess = sessions.get_or_create(
        sticky,
        account_id=account.id,
        force_new=force_new,
    )
    is_start = sess.turns == 0 or force_new

    t0 = time.time()
    _log(
        request,
        {
            "ts": t0,
            "account": account.id,
            "model": canon.model,
            "tone": tone,
            "stream": body.stream,
            "tools": len(canon.tools),
            "sticky": sticky,
            "conversation_id": sess.conversation_id,
            "is_start": is_start,
        },
    )

    try:
        client = SubstrateClient(
            account.token,
            origin=cfg.substrate.origin,
            time_zone=cfg.substrate.time_zone,
            timeout_sec=cfg.substrate.request_timeout_sec,
        )

        stream_kwargs = dict(
            tone=tone,
            conversation_id=sess.conversation_id,
            session_id=sess.session_id,
            is_start_of_session=is_start,
        )

        if body.stream:

            async def gen():
                try:
                    if canon.tools:
                        # Buffer full reply so we can strip fences and emit clean
                        # content + OpenAI tool_call deltas (no dual fence+tools).
                        chunks: list[str] = []
                        async for piece in client.chat_stream(prompt, **stream_kwargs):
                            chunks.append(piece)
                        full = "".join(chunks)
                        parsed = tool_loop.parse(full, canon.tools)
                        tool_holder = list(parsed.tool_calls)
                        content_out = (
                            parsed.text
                            if parsed.tool_calls
                            else full
                        )

                        async def text_iter():
                            if content_out:
                                yield content_out

                        async for sse in stream_openai_chunks(
                            model=canon.model,
                            text_iter=text_iter(),
                            tool_calls_holder=tool_holder,
                            conversation_id=sess.conversation_id,
                        ):
                            yield sse
                    else:
                        # Live token stream when no tools to parse
                        async def text_iter():
                            async for piece in client.chat_stream(prompt, **stream_kwargs):
                                yield piece

                        async for sse in stream_openai_chunks(
                            model=canon.model,
                            text_iter=text_iter(),
                            tool_calls_holder=None,
                            conversation_id=sess.conversation_id,
                        ):
                            yield sse

                    pool.mark_success(account.id)
                    sessions.touch(sticky, success=True)
                except Exception:
                    pool.mark_error(account.id, cooldown=True)
                    raise

            return StreamingResponse(gen(), media_type="text/event-stream")

        full = await client.chat(prompt, **stream_kwargs)
        parsed = tool_loop.parse(full, canon.tools)
        pool.mark_success(account.id)
        sessions.touch(sticky, success=True)
        return JSONResponse(
            final_openai_response(
                model=canon.model,
                content=parsed.text if parsed.tool_calls else full,
                tool_calls=parsed.tool_calls or None,
                conversation_id=sess.conversation_id,
            )
        )
    except SubstrateError as exc:
        pool.mark_error(account.id, cooldown=True)
        raise HTTPException(status_code=502, detail=f"substrate: {exc}") from exc
    except Exception as exc:  # noqa: BLE001
        pool.mark_error(account.id, cooldown=True)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
