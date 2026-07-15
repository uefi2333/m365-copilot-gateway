from __future__ import annotations

"""Model / tone catalog.

Public catalog prefers config models.advertise when present.
DEFAULT_MODELS is the slim seed + resolve fallback for unknown ids.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelInfo:
    id: str
    tone: str
    label: str
    family: str = "m365"


# Slim public seed — no alias explosion.
DEFAULT_MODELS: list[ModelInfo] = [
    ModelInfo("m365-copilot", "Magic", "M365 Copilot（自动）", "auto"),
    ModelInfo("gpt-5.5-reasoning", "Gpt_5_5_Reasoning", "GPT-5.5 深度推理", "gpt"),
    ModelInfo("gpt-5.5", "Gpt_5_5_Chat", "GPT-5.5 对话", "gpt"),
    ModelInfo("gpt-5.4-reasoning", "Gpt_5_4_Reasoning", "GPT-5.4 深度推理", "gpt"),
    ModelInfo("claude-sonnet-4.6", "Claude_Sonnet", "Claude Sonnet 4.6", "claude"),
    ModelInfo("claude-sonnet", "Claude_Sonnet", "Claude Sonnet", "claude"),
]


def resolve_tone(model_id: str, catalog: list[ModelInfo] | None = None) -> str:
    for pool in (catalog or [], DEFAULT_MODELS):
        for m in pool:
            if m.id == model_id:
                return m.tone
    low = model_id.lower()
    if low.startswith("claude"):
        return "Claude_Sonnet"
    if "5.5" in low and ("reason" in low or "think" in low or "deeper" in low):
        return "Gpt_5_5_Reasoning"
    if "5.4" in low and ("reason" in low or "think" in low or "deeper" in low):
        return "Gpt_5_4_Reasoning"
    if "5.5" in low:
        return "Gpt_5_5_Chat"
    if "reason" in low or "think" in low or "deeper" in low:
        return "Gpt_Reasoning"
    return "Magic"


def tone_for_tools(tone: str, *, has_tools: bool) -> str:
    """Reasoning tones meta-refuse tool prompts — force Chat/Magic for tool turns."""
    if not has_tools:
        return tone if tone != "Gpt_Quick" else "Magic"
    if "Reasoning" in tone or tone.endswith("_Reasoning"):
        if tone.startswith("Gpt_5_5"):
            return "Gpt_5_5_Chat"
        if tone.startswith("Claude"):
            return "Claude_Sonnet"
        return "Magic"
    if tone in ("Gpt_Quick",):
        return "Magic"
    return tone


def list_models(extra: list[ModelInfo] | None = None) -> list[ModelInfo]:
    """Public model list.

    When config advertise is non-empty, that list is the only public catalog
    (order preserved). Otherwise fall back to DEFAULT_MODELS.
    """
    if extra:
        return list(extra)
    return list(DEFAULT_MODELS)
