from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ChatSession:
    key: str
    conversation_id: str
    session_id: str
    account_id: str | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    turns: int = 0
    meta: dict[str, Any] = field(default_factory=dict)


class SessionStore:
    """In-memory sticky Substrate conversation map.

    Key sources (first non-empty wins at call site):
      - explicit conversation_id from client
      - OpenAI `user` field
      - account sticky meta
    """

    def __init__(self, ttl_sec: int = 6 * 3600, max_entries: int = 2000) -> None:
        self.ttl_sec = ttl_sec
        self.max_entries = max_entries
        self._items: dict[str, ChatSession] = {}

    def _purge(self) -> None:
        now = time.time()
        dead = [k for k, s in self._items.items() if now - s.updated_at > self.ttl_sec]
        for k in dead:
            del self._items[k]
        if len(self._items) <= self.max_entries:
            return
        # drop oldest
        ordered = sorted(self._items.items(), key=lambda kv: kv[1].updated_at)
        for k, _ in ordered[: max(0, len(self._items) - self.max_entries)]:
            del self._items[k]

    def get(self, key: str | None) -> ChatSession | None:
        if not key:
            return None
        self._purge()
        s = self._items.get(key)
        if not s:
            return None
        if time.time() - s.updated_at > self.ttl_sec:
            del self._items[key]
            return None
        return s

    def get_or_create(
        self,
        key: str | None,
        *,
        account_id: str | None = None,
        force_new: bool = False,
    ) -> ChatSession:
        self._purge()
        if key and not force_new:
            existing = self._items.get(key)
            if existing:
                existing.updated_at = time.time()
                if account_id:
                    existing.account_id = account_id
                return existing
        use_key = key or f"anon-{uuid.uuid4().hex[:12]}"
        sess = ChatSession(
            key=use_key,
            conversation_id=str(uuid.uuid4()),
            session_id=str(uuid.uuid4()),
            account_id=account_id,
        )
        self._items[use_key] = sess
        return sess

    def touch(self, key: str, *, success: bool = True) -> None:
        s = self._items.get(key)
        if not s:
            return
        s.updated_at = time.time()
        if success:
            s.turns += 1

    def drop(self, key: str) -> None:
        self._items.pop(key, None)
