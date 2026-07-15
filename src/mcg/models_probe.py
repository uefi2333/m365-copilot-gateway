from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from typing import Any

from mcg.models_catalog import DEFAULT_MODELS, ModelInfo


@dataclass
class ProbeEntry:
    id: str
    tone: str
    label: str
    family: str
    status: str  # known | advertised | probed_ok | probed_fail | skipped
    detail: str = ""
    latency_ms: int | None = None


# Tones observed in community research / product UI — used as live catalog seed.
KNOWN_TONES: list[tuple[str, str, str, str]] = [
    ("m365-copilot", "Magic", "M365 Copilot (auto)", "auto"),
    ("auto", "Magic", "Auto / Magic", "auto"),
    ("quick", "Gpt_Quick", "Quick", "gpt"),
    ("reasoning", "Gpt_Reasoning", "Think deeper", "gpt"),
    ("think-deeper", "Gpt_Reasoning", "Think deeper", "gpt"),
    ("claude", "Claude_Sonnet", "Claude Sonnet tone", "claude"),
    ("claude-sonnet", "Claude_Sonnet", "Claude Sonnet tone", "claude"),
    ("claude-sonnet-think-deeper", "Claude_Sonnet_Reasoning", "Claude Sonnet reasoning", "claude"),
    ("claude-opus", "Claude_Opus", "Claude Opus tone", "claude"),
    ("gpt-5.5", "Gpt_5_5_Chat", "GPT-5.5 chat tone", "gpt"),
    ("gpt-5.5-think-deeper", "Gpt_5_5_Reasoning", "GPT-5.5 reasoning tone", "gpt"),
    ("gpt-5.4", "Gpt_5_4_Reasoning", "GPT-5.4", "gpt"),
    ("gpt-5.4-quick", "Gpt_5_4_Quick", "GPT-5.4 quick", "gpt"),
    ("gpt-5.3-quick", "Gpt_5_3_Quick", "GPT-5.3 quick", "gpt"),
    ("gpt-5.3-think-deeper", "Gpt_5_3_Reasoning", "GPT-5.3 reasoning", "gpt"),
    ("gpt-5.2-quick", "Gpt_5_2_Quick", "GPT-5.2 quick", "gpt"),
    ("gpt-5.2-think-deeper", "Gpt_5_2_Reasoning", "GPT-5.2 reasoning", "gpt"),
]


def catalog_snapshot(extra: list[ModelInfo] | None = None) -> list[ProbeEntry]:
    seen: dict[str, ProbeEntry] = {}
    for mid, tone, label, family in KNOWN_TONES:
        seen[mid] = ProbeEntry(mid, tone, label, family, "known")
    for m in DEFAULT_MODELS:
        seen.setdefault(
            m.id,
            ProbeEntry(m.id, m.tone, m.label, m.family, "known"),
        )
    for m in extra or []:
        seen[m.id] = ProbeEntry(m.id, m.tone, m.label or m.id, m.family, "advertised")
    return list(seen.values())


async def live_probe(
    *,
    client_factory,
    tones: list[str] | None = None,
    prompt: str = "Reply with exactly: PONG",
    max_tones: int = 3,
) -> list[ProbeEntry]:
    """Best-effort live probe: send a tiny chat with selected tones.

    ``client_factory(tone)`` must return an object with ``async chat(text, tone=...)``.
    Limits to ``max_tones`` to avoid burning quota.
    """
    pick = tones or ["Magic", "Gpt_Quick", "Claude_Sonnet"]
    pick = pick[:max_tones]
    out: list[ProbeEntry] = []
    for tone in pick:
        mid = next((e.id for e in catalog_snapshot() if e.tone == tone), tone.lower())
        t0 = time.time()
        try:
            client = client_factory(tone)
            text = await client.chat(prompt, tone=tone, is_start_of_session=True)
            ms = int((time.time() - t0) * 1000)
            ok = bool(text and text.strip())
            out.append(
                ProbeEntry(
                    id=mid,
                    tone=tone,
                    label=f"live:{tone}",
                    family="probed",
                    status="probed_ok" if ok else "probed_fail",
                    detail=(text or "")[:120],
                    latency_ms=ms,
                )
            )
        except Exception as exc:  # noqa: BLE001
            ms = int((time.time() - t0) * 1000)
            out.append(
                ProbeEntry(
                    id=mid,
                    tone=tone,
                    label=f"live:{tone}",
                    family="probed",
                    status="probed_fail",
                    detail=str(exc)[:200],
                    latency_ms=ms,
                )
            )
    return out


def entries_to_openai(entries: list[ProbeEntry]) -> list[dict[str, Any]]:
    return [
        {
            "id": e.id,
            "object": "model",
            "created": 0,
            "owned_by": "m365-copilot-gateway",
            "root": e.tone,
            "permission": [],
            "metadata": {
                "tone": e.tone,
                "label": e.label,
                "family": e.family,
                "status": e.status,
                "detail": e.detail,
                "latency_ms": e.latency_ms,
            },
        }
        for e in entries
    ]


def entry_dict(e: ProbeEntry) -> dict[str, Any]:
    return asdict(e)
