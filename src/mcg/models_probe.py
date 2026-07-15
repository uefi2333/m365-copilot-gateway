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


# Keep aligned with DEFAULT_MODELS / config advertise.
KNOWN_TONES: list[tuple[str, str, str, str]] = [
    (m.id, m.tone, m.label, m.family) for m in DEFAULT_MODELS
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
    """Best-effort live probe: send a tiny chat with selected tones."""
    pick = tones or ["Magic", "Gpt_5_5_Reasoning", "Claude_Sonnet"]
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
                    family="probe",
                    status="probed_ok" if ok else "probed_fail",
                    detail=(text or "")[:80],
                    latency_ms=ms,
                )
            )
        except Exception as e:  # noqa: BLE001
            ms = int((time.time() - t0) * 1000)
            out.append(
                ProbeEntry(
                    id=mid,
                    tone=tone,
                    label=f"live:{tone}",
                    family="probe",
                    status="probed_fail",
                    detail=f"{type(e).__name__}:{e}",
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


def entry_dicts(entries: list[ProbeEntry]) -> list[dict[str, Any]]:
    return [asdict(e) for e in entries]
