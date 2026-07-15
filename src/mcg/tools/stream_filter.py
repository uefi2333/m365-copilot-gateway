"""Streaming holdback so tool fences are not dumped as content mid-stream."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Iterable


def _looks_like_tool_fence(block: str, tool_names: set[str], shell_langs: set[str]) -> bool:
    """block includes opening ``` and closing ```."""
    body = block.strip()
    if not body.startswith("```"):
        return False
    # strip fences
    inner = body[3:]
    if inner.endswith("```"):
        inner = inner[:-3]
    first_nl = inner.find("\n")
    if first_nl < 0:
        lang = inner.strip()
    else:
        lang = inner[:first_nl].strip()
    if not lang:
        return False
    if lang in tool_names:
        return True
    if lang.lower() in shell_langs and any(
        any(k in n.lower() for k in ("bash", "shell", "run", "exec", "cmd")) for n in tool_names
    ):
        return True
    return False


async def filter_stream_for_tools(
    pieces: AsyncIterator[str],
    *,
    tool_names: Iterable[str],
) -> AsyncIterator[str]:
    """Yield safe content deltas; withhold complete tool fences until parse at end.

    Accumulated full text is available via ``filter_stream_for_tools.full`` after
    the generator is exhausted — callers should use a StreamToolAccumulator.
    """
    raise NotImplementedError


class StreamToolAccumulator:
    """True-stream content while holding back tool fences for final parse."""

    _SHELL_LANGS = frozenset({"bash", "sh", "shell", "zsh", "cmd", "powershell", "ps1"})

    def __init__(self, tool_names: Iterable[str]) -> None:
        self.tool_names = set(tool_names)
        self.full_parts: list[str] = []
        self._buf = ""
        self._in_fence = False

    @property
    def full(self) -> str:
        return "".join(self.full_parts)

    def feed(self, piece: str) -> list[str]:
        """Ingest a raw delta; return zero or more content pieces safe to emit."""
        if not piece:
            return []
        self.full_parts.append(piece)
        self._buf += piece
        out: list[str] = []

        while self._buf:
            if not self._in_fence:
                idx = self._buf.find("```")
                if idx < 0:
                    # keep tail that might be partial fence open
                    keep = 2
                    if len(self._buf) > keep:
                        out.append(self._buf[:-keep])
                        self._buf = self._buf[-keep:]
                    break
                if idx > 0:
                    out.append(self._buf[:idx])
                self._buf = self._buf[idx:]
                self._in_fence = True
            else:
                # opened with ``` — find closer after the opening marker
                end = self._buf.find("```", 3)
                if end < 0:
                    break
                block = self._buf[: end + 3]
                rest = self._buf[end + 3 :]
                if self.tool_names and _looks_like_tool_fence(
                    block, self.tool_names, self._SHELL_LANGS
                ):
                    # withhold tool fence from content stream
                    self._buf = rest
                    self._in_fence = False
                    continue
                # not a tool fence — emit as normal content
                out.append(block)
                self._buf = rest
                self._in_fence = False

        return [p for p in out if p]

    def flush(self) -> list[str]:
        """End of stream: emit any residual non-withheld buffer."""
        if not self._buf:
            return []
        # incomplete fence at EOF — still emit (parser may not treat as tool)
        leftover = self._buf
        self._buf = ""
        self._in_fence = False
        return [leftover] if leftover else []


async def iter_filtered_stream(
    pieces: AsyncIterator[str],
    acc: StreamToolAccumulator,
) -> AsyncIterator[str]:
    async for piece in pieces:
        for safe in acc.feed(piece):
            yield safe
    for safe in acc.flush():
        yield safe
