"""Sliding-window in-memory rate limiter — Zero Redis, zero external deps.

Per-bucket strategy: API key (preferred) → client IP fallback.
Window = 1 min, configurable via config.yaml ``rate_limit`` section.
"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict, deque
from typing import Awaitable, Callable

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

from mcg.config import RateLimitConfig


class _SlidingWindow:
    __slots__ = ("_max_requests", "_window_sec", "_buckets", "_lock")

    def __init__(self, cfg: RateLimitConfig) -> None:
        self._max_requests = cfg.requests_per_minute
        self._window_sec = 60.0
        self._buckets: dict[str, deque[float]] = defaultdict(
            lambda: deque(maxlen=cfg.burst or cfg.requests_per_minute * 2)
        )
        self._lock = asyncio.Lock()

    async def allow(self, key: str) -> tuple[bool, int, int]:
        """(allowed, remaining, retry_after_sec)"""
        now = time.monotonic()
        cutoff = now - self._window_sec
        async with self._lock:
            bucket = self._buckets[key]
            # Prune expired stamps
            while bucket and bucket[0] < cutoff:
                bucket.popleft()
            remaining = self._max_requests - len(bucket)
            if remaining <= 0:
                retry_after = int(bucket[0] - cutoff + 1) if bucket else 60
                return False, 0, retry_after
            bucket.append(now)
            return True, remaining - 1, 0

    def _evict_stale(self) -> None:
        now = time.monotonic()
        cutoff = now - self._window_sec
        stale_keys = [
            k for k, v in self._buckets.items()
            if not v or v[-1] < cutoff
        ]
        for k in stale_keys:
            del self._buckets[k]


_SKIP_PREFIXES = frozenset({"/health", "/ui", "/static", "/v1/metrics", "/favicon"})


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Starlette middleware that rejects excessive requests with OpenAI-format 429."""

    def __init__(self, app: ASGIApp, cfg: RateLimitConfig) -> None:
        super().__init__(app)
        self._window = _SlidingWindow(cfg) if cfg.enabled else None
        self._cfg = cfg

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        if self._window is None:
            return await call_next(request)

        path = request.url.path
        if any(path.startswith(p) for p in _SKIP_PREFIXES):
            return await call_next(request)

        # Bucket key: API key if present, else client IP
        api_key = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
        key = api_key or request.client.host if request.client else "unknown"

        allowed, remaining, retry_after = await self._window.allow(key)

        if not allowed:
            body = (
                '{"error":{"message":"Rate limit exceeded. Try again later.",'
                '"type":"rate_limit_error","code":429}}'
            )
            return Response(
                status_code=429,
                content=body,
                media_type="application/json",
                headers={
                    "Retry-After": str(retry_after),
                    "X-RateLimit-Remaining": "0",
                    "X-RateLimit-Reset": str(int(time.time() + retry_after)),
                },
            )

        resp = await call_next(request)
        resp.headers["X-RateLimit-Remaining"] = str(remaining)
        return resp
