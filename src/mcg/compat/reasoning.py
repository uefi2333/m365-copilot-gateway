from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ReasoningSplit:
    reasoning: str = ""
    content: str = ""


class ReasoningSplitter:
    """Incrementally split explicit <thinking> blocks from answer text.

    Only an explicit upstream marker is treated as reasoning. Ordinary prose
    containing words such as "thinking" or "analysis" remains answer content.
    """

    OPEN = "<thinking>"
    CLOSE = "</thinking>"

    def __init__(self) -> None:
        self._buf = ""
        self._inside = False
        self.reasoning = ""

    def feed(self, piece: str) -> ReasoningSplit:
        if not piece:
            return ReasoningSplit()
        self._buf += piece
        out_reasoning: list[str] = []
        out_content: list[str] = []
        while self._buf:
            if self._inside:
                end = self._buf.find(self.CLOSE)
                if end < 0:
                    keep = len(self.CLOSE) - 1
                    if len(self._buf) > keep:
                        part, self._buf = self._buf[:-keep], self._buf[-keep:]
                        out_reasoning.append(part)
                    break
                out_reasoning.append(self._buf[:end])
                self._buf = self._buf[end + len(self.CLOSE):]
                self._inside = False
                continue
            start = self._buf.find(self.OPEN)
            if start < 0:
                keep = len(self.OPEN) - 1
                if len(self._buf) > keep:
                    part, self._buf = self._buf[:-keep], self._buf[-keep:]
                    out_content.append(part)
                break
            if start:
                out_content.append(self._buf[:start])
            self._buf = self._buf[start + len(self.OPEN):]
            self._inside = True
        r = "".join(out_reasoning)
        c = "".join(out_content)
        self.reasoning += r
        return ReasoningSplit(r, c)

    def flush(self) -> ReasoningSplit:
        if self._inside:
            r = self._buf
            self._buf = ""
            self.reasoning += r
            return ReasoningSplit(r, "")
        c = self._buf
        self._buf = ""
        return ReasoningSplit("", c)


def split_explicit_reasoning(text: str) -> ReasoningSplit:
    splitter = ReasoningSplitter()
    first = splitter.feed(text)
    last = splitter.flush()
    return ReasoningSplit(
        reasoning=first.reasoning + last.reasoning,
        content=first.content + last.content,
    )
