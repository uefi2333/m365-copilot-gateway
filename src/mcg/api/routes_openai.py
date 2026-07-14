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

    try:
        account = pool.acquire(sticky_key=body.user)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    canon = to_canonical(body)
    tone = resolve_tone(canon.model, models)
    prompt = tool_loop.augment_prompt(canon)

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
        },
    )

    try:
        client = SubstrateClient(
            account.token,
            origin=cfg.substrate.origin,
            time_zone=cfg.substrate.time_zone,
            timeout_sec=cfg.substrate.request_timeout_sec,
        )

        if body.stream:

            async def gen():
                chunks: list[str] = []

                async def text_iter():
                    async for piece in client.chat_stream(
                        prompt,
                        tone=tone,
                        is_start_of_session=True,
                    ):
                        chunks.append(piece)
                        yield piece

                try:
                    async for sse in stream_openai_chunks(model=canon.model, text_iter=text_iter()):
                        yield sse
                    # post-parse tools for non-streaming tool path is better; for stream we
                    # only stream text. Full tool stream merge comes in next iteration.
                    full = "".join(chunks)
                    parsed = tool_loop.parse(full, canon.tools)
                    if parsed.tool_calls:
                        # clients that only read stream text won't see tools; dual-path later
                        pass
                    pool.mark_success(account.id)
                except Exception:
                    pool.mark_error(account.id, cooldown=True)
                    raise

            return StreamingResponse(gen(), media_type="text/event-stream")

        # non-stream
        full = await client.chat(prompt, tone=tone, is_start_of_session=True)
        parsed = tool_loop.parse(full, canon.tools)
        pool.mark_success(account.id)
        return JSONResponse(
            final_openai_response(
                model=canon.model,
                content=parsed.text if parsed.tool_calls else full,
                tool_calls=parsed.tool_calls or None,
            )
        )
    except SubstrateError as exc:
        pool.mark_error(account.id, cooldown=True)
        raise HTTPException(status_code=502, detail=f"substrate: {exc}") from exc
    except Exception as exc:  # noqa: BLE001
        pool.mark_error(account.id, cooldown=True)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
