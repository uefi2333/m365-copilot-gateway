from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from mcg.auth.deps import require_api_key
from mcg.compat.anthropic_messages import (
    AnthropicMessagesRequest,
    anthropic_sse_events,
    final_anthropic_response,
    to_canonical,
)
from mcg.models_catalog import resolve_tone
from mcg.multimodal.adapter import extract_from_content, render_multimodal_prompt, substrate_message_extras
from mcg.substrate.client import SubstrateClient, SubstrateError

router = APIRouter()


def _sticky_key(body: AnthropicMessagesRequest, account_id: str) -> str:
    if body.conversation_id:
        return f"c:{body.conversation_id}"
    if body.user:
        return f"u:{body.user}:{account_id}"
    meta_uid = (body.metadata or {}).get("user_id") if body.metadata else None
    if meta_uid:
        return f"u:{meta_uid}:{account_id}"
    return f"a:{account_id}:anthropic"


@router.post("/v1/messages")
async def anthropic_messages(
    body: AnthropicMessagesRequest,
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
    # merge anthropic image parts into prompt if present
    media = canon.extra.get("anthropic_media_parts") or []
    if media:
        fake_msg = {"role": "user", "content": media}
        # re-render last user with media
        bundle_parts = []
        for part in media:
            b = extract_from_content([part])
            bundle_parts.extend(b.parts)
        if bundle_parts and canon.messages:
            # append media render to last user text
            for i in range(len(canon.messages) - 1, -1, -1):
                if canon.messages[i].role == "user":
                    canon.messages[i].content = render_multimodal_prompt(
                        canon.messages[i].content, bundle_parts
                    )
                    canon.extra["multimodal_parts"] = bundle_parts
                    break

    tone = resolve_tone(canon.model, models)
    prompt = tool_loop.augment_prompt(canon)
    mm_parts = canon.extra.get("multimodal_parts") or []
    msg_extras = substrate_message_extras(mm_parts) if mm_parts else None

    sticky = _sticky_key(body, account.id)
    force_new = bool(body.reset_conversation)
    sess = sessions.get_or_create(sticky, account_id=account.id, force_new=force_new)
    is_start = sess.turns == 0 or force_new

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

        full = await client.chat(prompt, **stream_kwargs)
        parsed = tool_loop.parse(full, canon.tools)

        # optional local tool loop (same as OpenAI path)
        if (
            parsed.tool_calls
            and local_runner
            and getattr(cfg.tools, "execution", "client") == "local"
            and not body.stream
        ):
            from mcg.compat.canonical import CanonicalMessage

            rounds = 0
            while parsed.tool_calls and rounds < cfg.tools.max_rounds:
                rounds += 1
                results = await local_runner.run_all(parsed.tool_calls)
                tool_msgs = local_runner.as_tool_messages(results)
                # fold into canon and re-prompt
                canon.messages.append(
                    CanonicalMessage(
                        role="assistant",
                        content=parsed.text,
                        tool_calls=parsed.tool_calls,
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
                prompt = tool_loop.augment_prompt(canon)
                full = await client.chat(
                    prompt,
                    tone=tone,
                    conversation_id=sess.conversation_id,
                    session_id=sess.session_id,
                    is_start_of_session=False,
                    message_extras=None,
                )
                parsed = tool_loop.parse(full, canon.tools)

        pool.mark_success(account.id)
        sessions.touch(sticky, success=True)

        content = parsed.text if parsed.tool_calls else full
        tools = parsed.tool_calls or None

        if body.stream:

            async def gen():
                for ev in anthropic_sse_events(
                    model=canon.model,
                    text=content or "",
                    tool_calls=tools,
                    conversation_id=sess.conversation_id,
                ):
                    yield ev

            return StreamingResponse(gen(), media_type="text/event-stream")

        return JSONResponse(
            final_anthropic_response(
                model=canon.model,
                content=content or "",
                tool_calls=tools,
                conversation_id=sess.conversation_id,
            )
        )
    except SubstrateError as exc:
        pool.mark_error(account.id, cooldown=True)
        raise HTTPException(status_code=502, detail=f"substrate: {exc}") from exc
    except Exception as exc:  # noqa: BLE001
        pool.mark_error(account.id, cooldown=True)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
