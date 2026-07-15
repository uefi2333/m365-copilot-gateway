from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

import websockets
from websockets.asyncio.client import ClientConnection

from mcg.token.jwtutil import decode_jwt_payload, is_substrate_token

from .protocol import (
    SIGNALR_SEP,
    build_chat_invoke,
    build_hub_url,
    handshake_frame,
    metrics_frame,
)


class SubstrateError(RuntimeError):
    pass


def is_transient_substrate_error(exc: BaseException) -> bool:
    """Upstream blips that deserve one retry, not an account cooldown."""
    msg = str(exc).lower()
    needles = (
        "timed out",
        "timeout",
        "temporarily",
        "connection closed",
        "connection reset",
        "broken pipe",
        "server disconnected",
        "try again",
        "503",
        "502",
        "429",
        "overloaded",
        "going away",
        "keepalive",
        "network",
        "eof",
        "ssl",
        "disengaged",
        "throttl",
        "rate limit",
        "too many",
        "unavailable",
        "handshake",
        "1006",
        "1001",
        "1011",
    )
    return any(n in msg for n in needles)


def is_session_reset_error(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return "disengaged" in msg or "conversation" in msg and "not found" in msg



def extract_image_urls(obj: Any) -> list[str]:
    """Pull Designer/DALL·E image URLs out of Substrate frames.

    Image gen arrives as messageType=Progress, contentType=GraphicArt with
    contentGenerationProgressList[].ImageReferenceUrls (status==2 when ready).
    Plain text path previously skipped all Progress frames → empty reply.
    """
    out: list[str] = []
    seen: set[str] = set()

    def add(u: str) -> None:
        u = (u or "").strip()
        if not u or u in seen:
            return
        if not (u.startswith("http://") or u.startswith("https://") or u.startswith("data:image")):
            return
        seen.add(u)
        out.append(u)

    def walk(o: Any) -> None:
        if isinstance(o, dict):
            for key in ("ImageReferenceUrls", "imageReferenceUrls", "imageUrls", "ImageUrls"):
                val = o.get(key)
                if isinstance(val, list):
                    for u in val:
                        if isinstance(u, str):
                            add(u)
                elif isinstance(val, str):
                    add(val)
            # single fields
            for key in ("imageUrl", "ImageUrl", "url", "sourceUrl", "contentUrl"):
                val = o.get(key)
                if isinstance(val, str) and ("designerapp" in val or "DallE" in val or val.startswith("data:image")):
                    add(val)
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(obj)
    return out


def format_image_markdown(urls: list[str], *, proxy_prefix: str | None = None) -> str:
    """Render image URLs as markdown the OpenAI client can display.

    Designer URLs often need auth; include raw URL plus optional gateway proxy.
    Clients that cannot render remote auth URLs can hit /v1/images/proxy.
    """
    if not urls:
        return ""
    import urllib.parse
    lines = ["[generated image]"]
    for i, u in enumerate(urls, 1):
        lines.append(f"![generated image {i}]({u})")
        lines.append(u)
        if proxy_prefix:
            view = f"{proxy_prefix}?url={urllib.parse.quote(u, safe='')}"
            lines.append(f"proxy: {view}")
    return "\n".join(lines)


def fold_stream_text(answer: str, next_text: str) -> tuple[str, str | None]:
    """Merge delta/snapshot text without duplicating prefixes.

    Inspired by cramt foldStreamText: M365 mixes token deltas with full snapshots.
    """
    if len(next_text) <= len(answer):
        return answer, None
    if next_text.startswith(answer):
        return next_text, next_text[len(answer) :]
    # Divergent longer snapshot: adopt for buffer, do not emit non-prefix.
    return next_text, None


class SubstrateClient:
    def __init__(
        self,
        access_token: str,
        *,
        origin: str = "https://m365.cloud.microsoft",
        time_zone: str = "Asia/Shanghai",
        timeout_sec: float = 120.0,
        open_timeout_sec: float | None = None,
    ) -> None:
        if not access_token:
            raise SubstrateError("access_token is empty")
        try:
            claims = decode_jwt_payload(access_token)
        except Exception as exc:  # noqa: BLE001
            raise SubstrateError(f"invalid JWT: {exc}") from exc
        if not is_substrate_token(claims):
            raise SubstrateError("token aud is not substrate.office.com")
        self.token = access_token
        self.oid = str(claims.get("oid") or "")
        self.tid = str(claims.get("tid") or "")
        if not self.oid or not self.tid:
            raise SubstrateError("token missing oid/tid")
        self.origin = origin
        self.time_zone = time_zone
        self.timeout_sec = timeout_sec
        self.open_timeout_sec = open_timeout_sec if open_timeout_sec is not None else min(15.0, timeout_sec)

    async def chat_stream(
        self,
        text: str,
        *,
        tone: str = "Magic",
        conversation_id: str | None = None,
        session_id: str | None = None,
        is_start_of_session: bool = True,
        message_history: list[dict[str, Any]] | None = None,
        message_extras: dict[str, Any] | None = None,
        agent_id: str | None = None,
    ) -> AsyncIterator[str]:
        conv = conversation_id or str(uuid.uuid4())
        sess = session_id or str(uuid.uuid4())
        req_id = str(uuid.uuid4())
        url = build_hub_url(
            oid=self.oid,
            tid=self.tid,
            access_token=self.token,
            conversation_id=conv,
            session_id=sess,
            request_id=req_id,
        )
        try:
            async with websockets.connect(
                url,
                additional_headers={"Origin": self.origin},
                open_timeout=self.open_timeout_sec,
                close_timeout=10,
                max_size=50 * 1024 * 1024,
            ) as ws:
                await ws.send(handshake_frame())
                await asyncio.wait_for(ws.recv(), timeout=min(15.0, self.timeout_sec))
                invoke = build_chat_invoke(
                    text=text,
                    session_id=sess,
                    request_id=req_id,
                    tone=tone,
                    is_start_of_session=is_start_of_session,
                    time_zone=self.time_zone,
                    message_history=message_history,
                    message_extras=message_extras,
                    agent_id=agent_id,
                )
                # cramt §4: Metrics must share the same WS send as chat
                await ws.send(invoke + metrics_frame())
                async for chunk in self._read_stream(ws):
                    yield chunk
        except SubstrateError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise SubstrateError(str(exc)) from exc

    async def chat(self, text: str, **kwargs: Any) -> str:
        parts: list[str] = []
        async for c in self.chat_stream(text, **kwargs):
            parts.append(c)
        return "".join(parts)

    async def _read_stream(self, ws: ClientConnection) -> AsyncIterator[str]:
        answer = ""
        image_urls: list[str] = []
        images_emitted = False

        def _collect_images(obj: Any) -> None:
            for u in extract_image_urls(obj):
                if u not in image_urls:
                    image_urls.append(u)

        def _maybe_emit_images() -> str | None:
            nonlocal images_emitted
            if images_emitted or not image_urls:
                return None
            # Prefer completed refs only; extract_image_urls already filters http(s)
            md = format_image_markdown(image_urls, proxy_prefix="/v1/images/proxy")
            if not md:
                return None
            images_emitted = True
            return md

        while True:
            raw = await asyncio.wait_for(ws.recv(), timeout=self.timeout_sec)
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", errors="ignore")
            for part in raw.split(SIGNALR_SEP):
                part = part.strip()
                if not part:
                    continue
                try:
                    msg = json.loads(part)
                except json.JSONDecodeError:
                    continue
                t = msg.get("type")
                if t == 6:
                    continue
                if t == 7:
                    raise SubstrateError(str(msg.get("error") or msg)[:500])
                if t == 1 and msg.get("target") == "update":
                    for arg in msg.get("arguments") or []:
                        if not isinstance(arg, dict):
                            continue
                        _collect_images(arg)
                        delta = arg.get("writeAtCursor")
                        if isinstance(delta, str) and delta:
                            # skip pure "Loading image" progress noise
                            if delta.strip().lower() in {"loading image", "loading image…", "loading image..."}:
                                pass
                            else:
                                answer, emit = fold_stream_text(answer, answer + delta)
                                if emit:
                                    yield emit
                        messages = arg.get("messages")
                        if messages:
                            entries = messages if isinstance(messages, list) else [messages]
                            for entry in reversed(entries):
                                if not isinstance(entry, dict):
                                    continue
                                if entry.get("author") == "user":
                                    continue
                                _collect_images(entry)
                                mt = entry.get("messageType")
                                if mt:
                                    if mt == "Disengaged":
                                        raise SubstrateError("disengaged")
                                    # GraphicArt Progress carries the real image URLs
                                    if (
                                        mt == "Progress"
                                        or entry.get("contentType") == "GraphicArt"
                                        or entry.get("contentGenerationProgressList")
                                    ):
                                        # emit images as soon as ready (status 2 frames)
                                        md = _maybe_emit_images()
                                        if md and md not in answer:
                                            answer, emit = fold_stream_text(answer, (answer + "\n" + md).strip())
                                            if emit:
                                                yield emit
                                    continue
                                text = entry.get("text")
                                if isinstance(text, str) and text:
                                    if text.strip().lower() in {
                                        "loading image",
                                        "loading image…",
                                        "loading image...",
                                    }:
                                        continue
                                    answer, emit = fold_stream_text(answer, text)
                                    if emit:
                                        yield emit
                                    break
                if t == 2:
                    # Completion payload — fold final text + any late image URLs.
                    item = msg.get("item") or {}
                    _collect_images(item)
                    item_msgs = item.get("messages") or []
                    for entry in reversed(item_msgs):
                        if not isinstance(entry, dict) or entry.get("author") == "user":
                            continue
                        _collect_images(entry)
                        if entry.get("messageType"):
                            if entry.get("messageType") == "Disengaged":
                                raise SubstrateError("disengaged")
                            continue
                        text = entry.get("text")
                        if isinstance(text, str) and text:
                            if text.strip().lower() not in {
                                "loading image",
                                "loading image…",
                                "loading image...",
                            }:
                                answer, emit = fold_stream_text(answer, text)
                                if emit:
                                    yield emit
                        break
                    # Always try to surface images at end if not yet emitted
                    md = _maybe_emit_images()
                    if md:
                        # if answer empty or images not included
                        if md not in answer:
                            piece = (("\n" if answer else "") + md)
                            answer = (answer + piece).strip()
                            yield piece if piece.startswith("\n") else md
                    return
                if t == 3:
                    md = _maybe_emit_images()
                    if md and md not in answer:
                        yield (("\n" if answer else "") + md)
                    return
