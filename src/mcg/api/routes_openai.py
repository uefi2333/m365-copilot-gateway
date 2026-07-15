from __future__ import annotations

import logging

import time
from pathlib import Path
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
from mcg.models_catalog import resolve_tone, tone_for_tools
from mcg.models_probe import catalog_snapshot, entries_to_openai, live_probe
from mcg.multimodal.adapter import substrate_message_extras
from mcg.substrate.client import (
    SubstrateClient,
    SubstrateError,
    is_transient_substrate_error,
    is_session_reset_error,
)

log = logging.getLogger("mcg.openai")
from mcg.tools.stream_filter import StreamToolAccumulator, iter_filtered_stream
from mcg.tools.loop import try_early_tool_calls
from mcg.tools.repair import maybe_repair_tool_call, force_tool_call, is_plain_chat
from mcg.tools.platform_adapt import should_short_circuit
from mcg.tools.sanitize import strip_reasoning_leak

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
    # No client sticky id → fresh conversation every request.
    # Sharing a single account-wide session causes concurrent Disengaged storms.
    import uuid as _uuid
    return f"a:{account_id}:{_uuid.uuid4().hex[:12]}"


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

    # --- FAST PATH: pure tool short-circuit / hop2 before token+substrate ---
    # Slash skill, hop2 chain, hop2 passthrough never need live JWT or WS.
    canon = to_canonical(body)
    try:
        from mcg.tools.agents import detect_agent
        _prof = detect_agent(
            tools=canon.tools,
            headers={k: v for k, v in request.headers.items()},
            user_agent=request.headers.get("user-agent"),
            path=str(request.url.path),
            model=canon.model,
        )
        canon.extra["agent_id"] = _prof.id
        canon.extra["agent_label"] = _prof.label
    except Exception:
        pass

    # cheap sticky account id without full ensure (pool may still pick account)
    try:
        account = pool.acquire(sticky_key=body.user)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    # Deterministic tool short-circuit for slash/skill intents on the *latest user turn*.
    # Only short-circuit when nothing (assistant/tool) has answered that latest user msg yet.
    # Prevents: (a) prose refusals on hop1, (b) infinite use_skill after tool results.
    if True:  # always inspect; tools may be empty (log + legacy functions handled upstream)
        last_user = ""
        last_user_idx = -1
        for i in range(len(canon.messages) - 1, -1, -1):
            m = canon.messages[i]
            if m.role == "user" and m.content:
                last_user = m.content
                last_user_idx = i
                break
        answered_after_user = any(
            m.role in ("assistant", "tool")
            for m in canon.messages[last_user_idx + 1 :]
        ) if last_user_idx >= 0 else False
        tool_names = [t.name for t in canon.tools]
        # Always print so uvicorn captures even if logging level is WARNING
        print(
            f"[mcg.tools] n={len(canon.tools)} names={tool_names[:24]} "
            f"last_user={last_user[:100]!r} answered={answered_after_user} "
            f"model={canon.model} stream={body.stream}",
            flush=True,
        )
        if canon.tools and last_user and not answered_after_user:
            sc = should_short_circuit(canon.tools, last_user)
            if sc is None and last_user.strip().startswith("/"):
                forced = force_tool_call(canon.tools, user_text=last_user)
                sc = forced[0] if forced else None
            if sc is not None:
                calls = sc if isinstance(sc, list) else [sc]
                sticky_sc = _sticky_key(body, account.id)
                sess_sc = sessions.get_or_create(
                    sticky_sc, account_id=account.id, force_new=False
                )
                print(
                    f"[mcg.tools] SHORT-CIRCUIT → "
                    f"{[c.get('function', {}).get('name') for c in calls]}",
                    flush=True,
                )
                if body.stream:
                    async def _sc_gen():
                        async def _empty():
                            if False:
                                yield ""
                        async for sse in stream_openai_chunks(
                            model=canon.model,
                            text_iter=_empty(),
                            tool_calls_holder=calls,
                            conversation_id=sess_sc.conversation_id,
                        ):
                            yield sse
                    return StreamingResponse(_sc_gen(), media_type="text/event-stream")
                return JSONResponse(
                    final_openai_response(
                        model=canon.model,
                        content=None,
                        tool_calls=calls,
                        conversation_id=sess_sc.conversation_id,
                    )
                )

    # Hop-2: skill result already in messages. Never let model prose-refuse "not a skill".
    # - If Write/Bash present → force deploy chain (no substrate).
    # - Else → return skill body as assistant content (client has no file tools).
    if canon.tools:
        from mcg.tools.inject import request_has_tool_results
        from mcg.tools.platform_adapt import family_of, is_skill_router
        from mcg.tools.repair import needs_tool_chain, force_chain_tool_call

        last_user = ""
        last_user_idx = -1
        for i in range(len(canon.messages) - 1, -1, -1):
            m = canon.messages[i]
            if m.role == "user" and m.content:
                last_user = m.content
                last_user_idx = i
                break
        answered_after_user = any(
            m.role in ("assistant", "tool")
            for m in canon.messages[last_user_idx + 1 :]
        ) if last_user_idx >= 0 else False

        if answered_after_user and request_has_tool_results(canon):
            # last tool result content
            last_tool_content = ""
            last_tool_name = ""
            for m in reversed(canon.messages):
                if m.role == "tool" and m.content:
                    last_tool_content = m.content
                    last_tool_name = (m.name or "")
                    break
            # last assistant tool call name
            last_call_name = ""
            for m in reversed(canon.messages):
                if m.role == "assistant" and m.tool_calls:
                    for tc in m.tool_calls:
                        fn = (tc.get("function") or {}) if isinstance(tc, dict) else {}
                        last_call_name = (fn.get("name") or "")
                    break
            skill_result = (
                "skill" in (last_call_name or "").lower()
                or "skill" in (last_tool_name or "").lower()
                or any(
                    k in (last_tool_content or "").lower()
                    for k in (
                        "story-setup",
                        "skill.md",
                        "# story-setup",
                        "执行铁律",
                        "phase 1",
                        "phase 2",
                        ".story-deployed",
                        "claude.md",
                    )
                )
            )
            # already finished deploy?
            last_lower = (last_tool_content or "").lower()
            deploy_done = (
                any(k in last_lower for k in ("deployed", "部署完成", "已部署", "setup complete"))
                and "skill" not in (last_call_name or "").lower()
            )
            has_file_tools = any(
                family_of(t.name) in ("write", "shell", "edit") for t in canon.tools
            )
            # agent profile may disable chain (phone_lite) or force passthrough
            _aid = (canon.extra or {}).get("agent_id") or "generic"
            _hop2_chain = True
            _hop2_pass = True
            try:
                from mcg.tools.agents import PROFILES
                _pr = PROFILES.get(_aid)
                if _pr:
                    _hop2_chain = _pr.hop2_chain
                    _hop2_pass = _pr.hop2_passthrough
            except Exception:
                pass
            if skill_result and not deploy_done:
                sticky_sc = _sticky_key(body, account.id)
                sess_sc = sessions.get_or_create(
                    sticky_sc, account_id=account.id, force_new=False
                )
                # hop2 chain disabled by default: no gateway-invented mkdir/Write.
                # Client has its own shell/write tools and will follow skill text.
                if _hop2_chain and has_file_tools and needs_tool_chain(canon):
                    calls = force_chain_tool_call(canon)
                    calls = [
                        c for c in calls
                        if "skill" not in ((c.get("function") or {}).get("name") or "").lower()
                    ]
                    if calls:
                        print(
                            f"[mcg.tools] HOP2 CHAIN → "
                            f"{[c.get('function',{}).get('name') for c in calls]}",
                            flush=True,
                        )
                        if body.stream:
                            async def _h2_gen():
                                async def _empty():
                                    if False:
                                        yield ""
                                async for sse in stream_openai_chunks(
                                    model=canon.model,
                                    text_iter=_empty(),
                                    tool_calls_holder=calls,
                                    conversation_id=sess_sc.conversation_id,
                                ):
                                    yield sse
                            return StreamingResponse(_h2_gen(), media_type="text/event-stream")
                        return JSONResponse(
                            final_openai_response(
                                model=canon.model,
                                content=None,
                                tool_calls=calls,
                                conversation_id=sess_sc.conversation_id,
                            )
                        )
                # After skill load: pass skill markdown through so the client
                # (or next model turn) can follow real instructions — not a stub.
                if _hop2_pass:
                    body_txt = (last_tool_content or "").strip()
                    if len(body_txt) > 12000:
                        body_txt = body_txt[:12000] + "\n\n…(skill truncated)"
                    skill_name = "skill"
                    for m in reversed(canon.messages):
                        if m.role == "assistant" and m.tool_calls:
                            for tc in m.tool_calls:
                                fn = (tc.get("function") or {}) if isinstance(tc, dict) else {}
                                if "skill" in (fn.get("name") or "").lower():
                                    try:
                                        import json as _json
                                        args = _json.loads(fn.get("arguments") or "{}")
                                        skill_name = (
                                            args.get("name")
                                            or args.get("skill")
                                            or skill_name
                                        )
                                    except Exception:
                                        pass
                            break
                    header = (
                        f"已加载技能 **{skill_name}**。\n"
                        f"当前会话工具列表无 Write/Bash，网关无法代写文件；"
                        f"技能正文如下，可按步骤在项目目录执行，"
                        f"或给客户端挂上 Write/Bash 后再发一次 `/story-setup`。\n\n---\n\n"
                    )
                    print(
                        f"[mcg.tools] HOP2 PASSTHROUGH skill={skill_name!r} "
                        f"bytes={len(body_txt)} agent={_aid}",
                        flush=True,
                    )
                    if body.stream:
                        async def _pt_gen():
                            async def _text():
                                yield header + body_txt
                            async for sse in stream_openai_chunks(
                                model=canon.model,
                                text_iter=_text(),
                                tool_calls_holder=None,
                                conversation_id=sess_sc.conversation_id,
                            ):
                                yield sse
                        return StreamingResponse(
                            _pt_gen(), media_type="text/event-stream"
                        )
                    return JSONResponse(
                        final_openai_response(
                            model=canon.model,
                            content=header + body_txt,
                            tool_calls=None,
                            conversation_id=sess_sc.conversation_id,
                        )
                    )

    # No tools registered but user typed /skill — still emit use_skill tool_call.
    # Many skill-capable clients only inject tools for Claude; for OpenAI path they
    # still execute tool_calls named use_skill / Skill. Without this, model prose-refuses.
    if not canon.tools:
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
        slash = None
        import re as _re
        mslash = _re.search(r"(?m)^\s*/([A-Za-z0-9_\-\u4e00-\u9fff]+)", last_user or "")
        if mslash and not answered:
            slash = mslash.group(1)
        if slash:
            import uuid as _uuid, json as _json
            call = {
                "id": f"call_{_uuid.uuid4().hex[:20]}",
                "type": "function",
                "function": {
                    "name": "use_skill",
                    "arguments": _json.dumps({"name": slash}, ensure_ascii=False),
                },
            }
            sticky_sc = _sticky_key(body, account.id)
            sess_sc = sessions.get_or_create(
                sticky_sc, account_id=account.id, force_new=False
            )
            print(
                f"[mcg.tools] SYNTHETIC use_skill name={slash!r} (client sent 0 tools)",
                flush=True,
            )
            if body.stream:
                async def _syn_gen():
                    async def _empty():
                        if False:
                            yield ""
                    async for sse in stream_openai_chunks(
                        model=canon.model,
                        text_iter=_empty(),
                        tool_calls_holder=[call],
                        conversation_id=sess_sc.conversation_id,
                    ):
                        yield sse
                return StreamingResponse(_syn_gen(), media_type="text/event-stream")
            return JSONResponse(
                final_openai_response(
                    model=canon.model,
                    content=None,
                    tool_calls=[call],
                    conversation_id=sess_sc.conversation_id,
                )
            )

    # Need substrate from here — ensure token only when not short-circuited
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

    tone = tone_for_tools(resolve_tone(canon.model, models), has_tools=bool(canon.tools))
    # Skip fat preamble on non-tool chat; keep compact for tools
    if canon.tools:
        prompt = tool_loop.augment_prompt(canon)
    else:
        prompt = canon.prompt_text()

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
            # tools: fail-fast handshake (15s default); plain chat keeps same
            open_timeout_sec=12.0 if canon.tools else min(20.0, cfg.substrate.request_timeout_sec),
        )

        # Optional Copilot Studio agent — only when tools present (cramt §5/§10:
        # agent forces GPT and breaks Claude/reasoning tones for plain chat).
        agent_id = None
        if canon.tools and cfg.tools.studio_agent_enabled:
            mgr = getattr(request.app.state, "studio_agent", None)
            if mgr is None:
                from mcg.agent.studio import StudioAgentManager

                cache = cfg.tools.studio_agent_cache or str(
                    Path(cfg.gateway.data_dir) / "studio_agent.json"
                )
                # tokens optional; manager returns None if missing
                bap = getattr(fabric, "get_scope_token", None)
                bap_tok = pp_tok = None
                # best-effort: reuse account sydney token won't work for PP/BAP
                mgr = StudioAgentManager(
                    cache_path=cache,
                    bap_token=None,
                    pp_token=None,
                )
                request.app.state.studio_agent = mgr
            try:
                agent_id = await mgr.get_agent_id()
            except Exception:  # noqa: BLE001
                agent_id = None

        stream_kwargs = dict(
            tone=tone,
            conversation_id=sess.conversation_id,
            session_id=sess.session_id,
            is_start_of_session=is_start,
            message_extras=msg_extras,
            agent_id=agent_id,
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
                # capture outer sess into a local that we can rebind safely
                cur_sess = sess
                try:
                    full_buf = ""
                    last_err: Exception | None = None
                    for attempt in range(3):
                        try:
                            raw_stream = client.chat_stream(prompt, **stream_kwargs)
                            if canon.tools:
                                acc = StreamToolAccumulator(t.name for t in canon.tools)
                                early_calls = None
                                async for piece in raw_stream:
                                    acc.feed(piece)
                                    # Abort Substrate stream as soon as a full tool fence is in hand
                                    if acc.got_tool_fence:
                                        early_calls = try_early_tool_calls(
                                            acc.full, canon.tools
                                        )
                                        if early_calls:
                                            break
                                acc.flush()
                                full_buf = acc.full
                                if early_calls:
                                    # skip waiting for type-2/3 tail + repair hop
                                    parsed_early = type("P", (), {})()
                                    # use early path below via full_buf still parseable
                                    full_buf = acc.full
                            else:
                                parts: list[str] = []
                                async for piece in raw_stream:
                                    parts.append(piece)
                                full_buf = "".join(parts)
                            last_err = None
                            break
                        except Exception as exc:  # noqa: BLE001
                            last_err = exc
                            log.warning(
                                "stream attempt=%s err=%s sticky=%s tools=%s",
                                attempt,
                                exc,
                                sticky,
                                len(canon.tools),
                            )
                            if attempt >= 2:
                                break
                            if is_session_reset_error(exc) or is_transient_substrate_error(exc):
                                cur_sess = sessions.get_or_create(
                                    sticky, account_id=account.id, force_new=True
                                )
                                stream_kwargs["conversation_id"] = cur_sess.conversation_id
                                stream_kwargs["session_id"] = cur_sess.session_id
                                stream_kwargs["is_start_of_session"] = True
                                continue
                            break

                    if last_err is not None and not full_buf:
                        # tools: degrade to synthetic tool_call instead of HTML 500
                        if canon.tools:
                            
                            user_text = "\n".join(
                                m.content
                                for m in canon.messages
                                if m.role == "user" and m.content
                            )
                            tool_holder = force_tool_call(
                                canon.tools, user_text=user_text
                            )
                            log.error(
                                "stream disengage fallback tools err=%s force=%s",
                                last_err,
                                [c.get("function", {}).get("name") for c in tool_holder],
                            )
                            # disengage is upstream flake — do NOT burn pool errors when
                            # we already have a clean synthetic path or plain chat
                            if not tool_holder and not is_plain_chat(user_text):
                                pool.mark_soft_error(account.id)

                            async def _text_or_empty():
                                if not tool_holder:
                                    # plain chat or unclear intent: surface soft error text
                                    yield f"(upstream busy: {str(last_err)[:120]})"
                                    return
                                if False:
                                    yield ""

                            async for sse in stream_openai_chunks(
                                model=canon.model,
                                text_iter=_text_or_empty(),
                                tool_calls_holder=tool_holder or None,
                                conversation_id=cur_sess.conversation_id,
                            ):
                                yield sse
                            return
                        pool.mark_soft_error(account.id)
                        raise last_err

                    if canon.tools:
                        parsed = await maybe_repair_tool_call(
                            client=client,
                            tool_loop=tool_loop,
                            canon=canon,
                            stream_kwargs=stream_kwargs,
                            full_text=full_buf,
                            repair_rounds=0,
                        )
                        tool_holder: list = list(parsed.tool_calls)

                        async def text_out():
                            if tool_holder:
                                return
                            text = strip_reasoning_leak(
                                (parsed.text or full_buf or "").strip()
                            )
                            if text:
                                yield text

                        async for sse in stream_openai_chunks(
                            model=canon.model,
                            text_iter=text_out(),
                            tool_calls_holder=tool_holder,
                            conversation_id=cur_sess.conversation_id,
                        ):
                            yield sse
                    else:

                        async def text_iter():
                            if full_buf:
                                yield full_buf

                        async for sse in stream_openai_chunks(
                            model=canon.model,
                            text_iter=text_iter(),
                            tool_calls_holder=None,
                            conversation_id=cur_sess.conversation_id,
                        ):
                            yield sse

                    pool.mark_success(account.id)
                    sessions.touch(sticky, success=True)
                except Exception as exc:
                    log.error("stream fail: %s", exc)
                    pool.mark_soft_error(account.id)
                    raise

            return StreamingResponse(gen(), media_type="text/event-stream")

        full = ""
        last_err: Exception | None = None
        for attempt in range(3):
            try:
                if canon.tools:
                    # Early-exit when tool fence complete (don't wait model epilogue)
                    acc = StreamToolAccumulator(t.name for t in canon.tools)
                    async for piece in client.chat_stream(prompt, **stream_kwargs):
                        acc.feed(piece)
                        if acc.got_tool_fence and try_early_tool_calls(acc.full, canon.tools):
                            break
                    acc.flush()
                    full = acc.full
                else:
                    full = await client.chat(prompt, **stream_kwargs)
                last_err = None
                break
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                log.warning(
                    "chat attempt=%s err=%s sticky=%s tools=%s",
                    attempt,
                    exc,
                    sticky,
                    len(canon.tools),
                )
                if attempt >= 2:
                    break
                if is_session_reset_error(exc) or is_transient_substrate_error(exc):
                    sess = sessions.get_or_create(
                        sticky, account_id=account.id, force_new=True
                    )
                    stream_kwargs["conversation_id"] = sess.conversation_id
                    stream_kwargs["session_id"] = sess.session_id
                    stream_kwargs["is_start_of_session"] = True
                    continue
                break

        if last_err is not None and not full:
            # tool clients: never hard-fail with 502 if we can synthesize a call
            if canon.tools:
                
                user_text = "\n".join(
                    m.content for m in canon.messages if m.role == "user" and m.content
                )
                forced = force_tool_call(canon.tools, user_text=user_text)
                log.error(
                    "chat disengage fallback tools err=%s force=%s",
                    last_err,
                    [c.get("function", {}).get("name") for c in forced],
                )
                if not forced and not is_plain_chat(user_text):
                    pool.mark_soft_error(account.id)
                return JSONResponse(
                    final_openai_response(
                        model=canon.model,
                        content=None if forced else f"(upstream busy: {str(last_err)[:120]})",
                        tool_calls=forced or None,
                        conversation_id=sess.conversation_id,
                    )
                )
            pool.mark_soft_error(account.id)
            raise last_err if isinstance(last_err, SubstrateError) else SubstrateError(str(last_err))

        parsed = await maybe_repair_tool_call(
            client=client,
            tool_loop=tool_loop,
            canon=canon,
            stream_kwargs=stream_kwargs,
            full_text=full,
            repair_rounds=0 if canon.tools else 0,
        )
        pool.mark_success(account.id)
        sessions.touch(sticky, success=True)
        content = (
            strip_reasoning_leak(parsed.text)
            if parsed.tool_calls
            else strip_reasoning_leak(parsed.text or full)
        )
        return JSONResponse(
            final_openai_response(
                model=canon.model,
                content=content,
                tool_calls=parsed.tool_calls or None,
                conversation_id=sess.conversation_id,
            )
        )
    except SubstrateError as exc:
        log.error("substrate 502: %s", exc)
        pool.mark_soft_error(account.id)
        raise HTTPException(status_code=502, detail=f"substrate: {exc}") from exc
    except Exception as exc:  # noqa: BLE001
        log.exception("chat 500: %s", exc)
        pool.mark_soft_error(account.id)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
