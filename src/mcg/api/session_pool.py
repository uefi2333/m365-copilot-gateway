from __future__ import annotations

import hashlib
import time
import uuid
from dataclasses import dataclass
from typing import Any

from .chat_format import message_text


@dataclass
class ConversationState:
    session_id: str
    conversation_id: str
    sent_count: int = 0
    last_access: float = 0.0


class SessionPool:
    def __init__(self, ttl_sec: int = 1800) -> None:
        self.ttl_sec = ttl_sec
        self._items: dict[str, ConversationState] = {}

    def resolve(self, messages: list[dict[str, Any]], explicit_id: str | None = None) -> ConversationState:
        self._evict()
        key = explicit_id or self._fingerprint(messages)
        now = time.time()
        st = self._items.get(key)
        if not st or len(messages) < st.sent_count:
            st = ConversationState(str(uuid.uuid4()), str(uuid.uuid4()), 0, now)
            self._items[key] = st
        st.last_access = now
        return st

    def _evict(self) -> None:
        now = time.time()
        for key, st in list(self._items.items()):
            if now - st.last_access > self.ttl_sec:
                del self._items[key]

    @staticmethod
    def _fingerprint(messages: list[dict[str, Any]]) -> str:
        first = next((m for m in messages if m.get("role") == "user"), messages[0] if messages else {})
        return hashlib.sha256(message_text(first.get("content")).encode()).hexdigest()[:24]
