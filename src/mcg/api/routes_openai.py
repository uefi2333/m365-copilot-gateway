from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from mcg.auth.deps import require_api_key
from mcg.compat.canonical import CanonicalMessage
from mcg.compat.openai_chat import (
    OpenAIChatRequest,
    final_openai_response,
    stream_openai_chunks,
    to_canonical,
)
from mcg.models_catalog import resolve_tone
from mcg.models_probe import catalog_snapshot, entries_to_openai, live_probe
from mcg.multimodal.adapter import substrate_message_extras
from mcg.substrate.client import SubstrateClient, SubstrateError
from mcg.tools.stream_filter import StreamToolAccumulator, iter_filtered_stream
from mcg.tools.repair import maybe_repair_tool_call

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


@router.get("/v1/models/probe")
async def probe_models_catalog(request: Request, _key: str = Depends(require_api_key)):
    """Static + advertised capability catalog (no live Substrate calls)."""
    extra = request.app.state.models
    entries = catalog_snapshot(extra)
    return {
        "object": "list",
        "mode": "catalog",
        "data": entries_to_openai(entries),
    }


@router.post("/v1/models/probe")
async def probe_models_live(
    request: Request,
    _key: str = Depends(require_api_key),
    max_tones: int = 2,
):
    """Live tone probe — burns a tiny chat per tone. Default 2 tones."""
    cfg = request.app.state.config
    pool = request.app.state.pool
    try:
        account = pool.acquire()
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

    def factory(_tone: str):
        return SubstrateClient(
            account.token,
            origin=cfg.substrate.origin,
            time_zone=cfg.substrate.time_zone,
            timeout_sec=min(60, cfg.substrate.request_timeout_sec),
        )

    try:
        entries = await live_probe(client_factory=factory, max_tones=max(1, min(max_tones, 5)))
        pool.mark_success(account.id)
        return {"object": "list", "mode": "live", "data": entries_to_openai(entries)}
    except Exception as exc:  # noqa: BLE001
        pool.mark_error(account.id, cooldown=True)
        raise HTTPException(status_code=502, detail=str(exc)) from exc


async def _local_tool_rounds(
    *,
    client: SubstrateClient,
    tool_loop,
    local_runner,
    canon,
    stream_kwargs: dict[str, Any],
    max_rounds: int,
    tone: str,
) -> tuple[str, list]:
    """Run model → local shell tools → model until stop or max_rounds.

    Non-shell tool_calls are returned to the client (OpenAI protocol) instead of
    inventing a denial and re-prompting the model.
    """
    full = await client.chat(tool_loop.augment_prompt(canon), **stream_kwargs)
    parsed = tool_loop.parse(full, canon.tools)
    rounds = 0
    while parsed.tool_calls and rounds < max_rounds:
        localable = [
            tc
            for tc in parsed.tool_calls
            if local_runner._name_allowed((tc.get("function") or {}).get("name") or "")
        ]
        if not localable:
            # hand off to client (get_current_time / weather / etc.)
            return parsed.text, parsed.tool_calls
        rounds += 1
        results = await local_runner.run_all(localable)
        tool_msgs = local_runner.as_tool_messages(results)
        canon.messages.append(
            CanonicalMessage(
                role="assistant",
                content=parsed.text,
                tool_calls=localable,
            )
        )
        for tm in tool_msgs:
            canon.messages.append(
                CanonicalMessage(
                    role="tool",
                    content=tm["content"],
                    name=tm.get("name"),
                    tool_call_id=tm.get("tool_call_id"),
                )
            )
        next_kwargs = dict(stream_kwargs)
        next_kwargs["is_start_of_session"] = False
        next_kwargs["message_extras"] = None
        full = await client.chat(tool_loop.augment_prompt(canon), **next_kwargs)
        parsed = tool_loop.parse(full, canon.tools)
    content = parsed.text if parsed.tool_calls else full
    return content, parsed.tool_calls


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
    local_runner = getattr(request.app.state, "local_runner", None)

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
    mm_parts = canon.extra.get("multimodal_parts") or []
    msg_extras = substrate_message_extras(mm_parts) if mm_parts else None

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
            "multimodal": len(mm_parts),
            "tool_exec": cfg.tools.execution,
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
            message_extras=msg_extras,
        )

        # local tool loop only for non-stream + shell-capable tools
        use_local = (
            cfg.tools.execution == "local"
            and local_runner is not None
            and local_runner.enabled
            and bool(canon.tools)
            and not body.stream
        )

        if use_local:
            content, tool_calls = await _local_tool_rounds(
                client=client,
                tool_loop=tool_loop,
                local_runner=local_runner,
                canon=canon,
                stream_kwargs=stream_kwargs,
                max_rounds=cfg.tools.max_rounds,
                tone=tone,
            )
            pool.mark_success(account.id)
            sessions.touch(sticky, success=True)
            return JSONResponse(
                final_openai_response(
                    model=canon.model,
                    content=content,
                    tool_calls=tool_calls or None,
                    conversation_id=sess.conversation_id,
                )
            )

        if body.stream:

            async def gen():
                try:
                    raw_stream = client.chat_stream(prompt, **stream_kwargs)
                    if canon.tools:
                        # Buffer while tools are declared so we can:
                        # 1) strip fences / run repair
                        # 2) emit EITHER content OR tool_calls (not narrate then tools)
                        acc = StreamToolAccumulator(t.name for t in canon.tools)
                        async for piece in raw_stream:
                            acc.feed(piece)  # accumulate; ignore safe content for now
                        acc.flush()
                        parsed = await maybe_repair_tool_call(
                            client=client,
                            tool_loop=tool_loop,
                            canon=canon,
                            stream_kwargs=stream_kwargs,
                            full_text=acc.full,
                            repair_rounds=cfg.tools.repair_rounds,
                        )
                        tool_holder: list = list(parsed.tool_calls)

                        async def text_out():
                            if tool_holder:
                                return
                            # no tool call — stream residual clean text as one chunk
                            text = (parsed.text or acc.full or "").strip()
                            if text:
                                yield text

                        async for sse in stream_openai_chunks(
                            model=canon.model,
                            text_iter=text_out(),
                            tool_calls_holder=tool_holder,
                            conversation_id=sess.conversation_id,
                        ):
                            yield sse
                    else:

                        async def text_iter():
                            async for piece in raw_stream:
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
        parsed = await maybe_repair_tool_call(
            client=client,
            tool_loop=tool_loop,
            canon=canon,
            stream_kwargs=stream_kwargs,
            full_text=full,
            repair_rounds=cfg.tools.repair_rounds,
        )
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
