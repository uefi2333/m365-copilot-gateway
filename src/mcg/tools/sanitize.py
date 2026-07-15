"""Strip reasoning / meta leakage from model text before client delivery."""

from __future__ import annotations

import re

# DeepLeo / reasoning models leak analysis as markdown sections
_SECTION_RE = re.compile(
    r"(?is)^\s*(?:\*\*)?(?:"
    r"clarifying tool[^.\n]*|"
    r"tool limitations?|"
    r"thinking(?: process)?|"
    r"reasoning|"
    r"analysis|"
    r"plan|"
    r"internal monologue"
    r")(?:\*\*)?\s*:?\s*\n.*?(?=\n\n|\Z)"
)

_HIDE_RE = re.compile(r"(?im)^\s*hide!?\s*$")
_LINE_NOISE = re.compile(
    r"(?im)^\s*(?:"
    r"i cannot call\b.*|"
    r"i can't call\b.*|"
    r"i am unable to call\b.*|"
    r"i don't have (?:access|the ability)\b.*|"
    r"i can only invoke\b.*|"
    r".*is not available\b.*|"
    r".*isn't available\b.*|"
    r"as an ai[,\s].*"
    r")\s*$"
)


def strip_reasoning_leak(text: str) -> str:
    if not text:
        return text
    t = _SECTION_RE.sub("", text)
    lines = []
    for line in t.splitlines():
        if _HIDE_RE.match(line):
            continue
        if _LINE_NOISE.match(line):
            continue
        lines.append(line)
    t = "\n".join(lines).strip()
    # collapse excessive blank lines
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t
