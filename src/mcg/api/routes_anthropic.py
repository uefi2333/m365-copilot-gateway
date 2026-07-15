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
from mcg.models_catalog import resolve_tone, tone_for_tools
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

    # Fingerprint (Claude Code almost always hits this path)
    try:
        from mcg.tools.agents import detect_agent, rewrite_tool_call_ids
        _prof = detect_agent(
            tools=canon.tools,
            headers={k: v for k, v in request.headers.items()},
            user_agent=request.headers.get("user-agent"),
            path=str(request.url.path),
            model=canon.model,
        )
        canon.extra["agent_id"] = _prof.id
        canon.extra["agent_label"] = _prof.label
        print(f"[mcg.agent] id={_prof.id} wire=anthropic", flush=True)
    except Exception as _exc:  # noqa: BLE001
        print(f"[mcg.agent] detect failed: {_exc}", flush=True)
        _prof = None

    tone = tone_for_tools(resolve_tone(canon.model, models), has_tools=bool(canon.tools))

    # Slash / skill short-circuit (same policy as OpenAI path)
    from mcg.tools.platform_adapt import should_short_circuit
    from mcg.tools.repair import force_tool_call

    last_user = ""
    last_user_idx = -1
    for i in range(len(canon.messages) - 1, -1, -1):
        m = canon.messages[i]
        if m.role == "user" and m.content:
            last_user = m.content
            last_user_idx = i
            break
    answered = any(
        m.role in ("assistant", "tool")
        for m in canon.messages[last_user_idx + 1 :]
    ) if last_user_idx >= 0 else False
    if canon.tools and last_user and not answered:
        sc = should_short_circuit(canon.tools, last_user)
        if sc is None and last_user.strip().startswith("/"):
            forced = force_tool_call(canon.tools, user_text=last_user)
            sc = forced[0] if forced else None
        if sc is not None:
            calls = sc if isinstance(sc, list) else [sc]
            try:
                from mcg.tools.agents import rewrite_tool_call_ids, PROFILES
                aid = (canon.extra or {}).get("agent_id")
                pr = PROFILES.get(aid) if aid else None
                if pr:
                    calls = rewrite_tool_call_ids(calls, pr) or calls
            except Exception:
                pass
            sticky_sc = _sticky_key(body, account.id)
            sess_sc = sessions.get_or_create(
                sticky_sc, account_id=account.id, force_new=False
            )
            print(
                f"[mcg.tools] ANTHROPIC SHORT-CIRCUIT → "
                f"{[c.get('function', {}).get('name') for c in calls]}",
                flush=True,
            )
            if body.stream:
                frames = anthropic_sse_events(
                    model=canon.model,
                    text="",
                    tool_calls=calls,
                    conversation_id=sess_sc.conversation_id,
                )
                async def _sc():
                    for fr in frames:
                        yield fr
                return StreamingResponse(_sc(), media_type="text/event-stream")
            return JSONResponse(
                final_anthropic_response(
                    model=canon.model,
                    content="",
                    tool_calls=calls,
                    conversation_id=sess_sc.conversation_id,
                )
            )

    # Hop2 skill passthrough / chain (shared policy)
    if canon.tools and answered:
        from mcg.tools.inject import request_has_tool_results
        from mcg.tools.platform_adapt import family_of
        from mcg.tools.repair import needs_tool_chain, force_chain_tool_call
        if request_has_tool_results(canon):
            last_tool_content = ""
            last_call_name = ""
            for m in reversed(canon.messages):
                if m.role == "tool" and m.content and not last_tool_content:
                    last_tool_content = m.content
                if m.role == "assistant" and m.tool_calls and not last_call_name:
                    for tc in m.tool_calls:
                        fn = (tc.get("function") or {}) if isinstance(tc, dict) else {}
                        last_call_name = fn.get("name") or ""
            skill_result = "skill" in (last_call_name or "").lower() or any(
                k in (last_tool_content or "").lower()
                for k in ("story-setup", "skill.md", "执行铁律", ".story-deployed", "phase 1")
            )
            has_file = any(family_of(t.name) in ("write", "shell", "edit") for t in canon.tools)
            if skill_result:
                sticky_sc = _sticky_key(body, account.id)
                sess_sc = sessions.get_or_create(
                    sticky_sc, account_id=account.id, force_new=False
                )
                if has_file and needs_tool_chain(canon):
                    calls = [
                        c for c in force_chain_tool_call(canon)
                        if "skill" not in ((c.get("function") or {}).get("name") or "").lower()
                    ]
                    if calls:
                        try:
                            from mcg.tools.agents import rewrite_tool_call_ids, PROFILES
                            aid = (canon.extra or {}).get("agent_id")
                            pr = PROFILES.get(aid) if aid else None
                            if pr:
                                calls = rewrite_tool_call_ids(calls, pr) or calls
                        except Exception:
                            pass
                        print(f"[mcg.tools] ANTHROPIC HOP2 CHAIN → {[c.get('function',{}).get('name') for c in calls]}", flush=True)
                        if body.stream:
                            frames = anthropic_sse_events(
                                model=canon.model, text="", tool_calls=calls,
                                conversation_id=sess_sc.conversation_id,
                            )
                            async def _h2():
                                for fr in frames:
                                    yield fr
                            return StreamingResponse(_h2(), media_type="text/event-stream")
                        return JSONResponse(
                            final_anthropic_response(
                                model=canon.model, content="", tool_calls=calls,
                                conversation_id=sess_sc.conversation_id,
                            )
                        )
                elif not has_file:
                    body_txt = (last_tool_content or "").strip()[:12000]
                    content = (
                        "已加载技能正文（当前 Anthropic 客户端工具集无 Write/Bash）。\n\n---\n\n"
                        + body_txt
                    )
                    print("[mcg.tools] ANTHROPIC HOP2 PASSTHROUGH", flush=True)
                    if body.stream:
                        frames = anthropic_sse_events(
                            model=canon.model, text=content, tool_calls=None,
                            conversation_id=sess_sc.conversation_id,
                        )
                        async def _pt():
                            for fr in frames:
                                yield fr
                        return StreamingResponse(_pt(), media_type="text/event-stream")
                    return JSONResponse(
                        final_anthropic_response(
                            model=canon.model, content=content, tool_calls=None,
                            conversation_id=sess_sc.conversation_id,
                        )
                    )

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
        pool.mark_soft_error(account.id)
        raise HTTPException(status_code=502, detail=f"substrate: {exc}") from exc
    except Exception as exc:  # noqa: BLE001
        pool.mark_soft_error(account.id)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
