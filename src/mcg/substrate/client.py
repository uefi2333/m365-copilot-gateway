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
                open_timeout=self.timeout_sec,
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
                        delta = arg.get("writeAtCursor")
                        if isinstance(delta, str) and delta:
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
                                # Control frames (Disengaged, Progress, …) must not become content
                                if entry.get("messageType"):
                                    if entry.get("messageType") == "Disengaged":
                                        raise SubstrateError("disengaged")
                                    continue
                                text = entry.get("text")
                                if isinstance(text, str) and text:
                                    answer, emit = fold_stream_text(answer, text)
                                    if emit:
                                        yield emit
                                    break
                if t == 2:
                    # Completion payload — fold final text then end immediately.
                    item = msg.get("item") or {}
                    item_msgs = item.get("messages") or []
                    for entry in reversed(item_msgs):
                        if not isinstance(entry, dict) or entry.get("author") == "user":
                            continue
                        if entry.get("messageType"):
                            if entry.get("messageType") == "Disengaged":
                                raise SubstrateError("disengaged")
                            continue
                        text = entry.get("text")
                        if isinstance(text, str) and text:
                            answer, emit = fold_stream_text(answer, text)
                            if emit:
                                yield emit
                        break
                    return
                if t == 3:
                    return
