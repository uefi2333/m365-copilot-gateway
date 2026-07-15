from __future__ import annotations

"""Model / tone catalog.

Public IDs use provider official product names only.
Substrate `tone` is the private wire field.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelInfo:
    id: str
    tone: str
    label: str
    family: str = "m365"


# Official product surface only — no alias spam.
# Verified live 2026-07-15 on enterprise Substrate:
#   Gpt_5_6_Reasoning OK; Gpt_5_6_Chat / Gpt_5_6_Quick empty
#   Gpt_5_5_Reasoning / Gpt_5_5_Chat / Gpt_5_4_Reasoning / Claude_Sonnet / Magic OK
DEFAULT_MODELS: list[ModelInfo] = [
    ModelInfo("m365-copilot", "Magic", "M365 Copilot", "auto"),
    ModelInfo("gpt-5.6", "Gpt_5_6_Reasoning", "GPT-5.6", "gpt"),
    ModelInfo("gpt-5.5-think-deeper", "Gpt_5_5_Reasoning", "GPT-5.5 Think deeper", "gpt"),
    ModelInfo("gpt-5.5-quick", "Gpt_5_5_Chat", "GPT-5.5 Quick response", "gpt"),
    ModelInfo("gpt-5.4-think-deeper", "Gpt_5_4_Reasoning", "GPT-5.4 Think deeper", "gpt"),
    ModelInfo("claude-sonnet-4.6", "Claude_Sonnet", "Claude Sonnet 4.6", "claude"),
]


def resolve_tone(model_id: str, catalog: list[ModelInfo] | None = None) -> str:
    for pool in (catalog or [], DEFAULT_MODELS):
        for m in pool:
            if m.id == model_id:
                return m.tone
    low = model_id.lower().replace("_", "-")
    # legacy aliases still route, but are not advertised
    if low in ("gpt-5.6-reasoning", "gpt-5.6-think-deeper"):
        return "Gpt_5_6_Reasoning"
    if low in ("gpt-5.5-reasoning", "gpt-5.5-think-deeper", "think-deeper", "reasoning"):
        return "Gpt_5_5_Reasoning"
    if low in ("gpt-5.5", "gpt-5.5-chat", "gpt-5.5-quick", "quick"):
        return "Gpt_5_5_Chat"
    if low in ("gpt-5.4", "gpt-5.4-reasoning", "gpt-5.4-think-deeper"):
        return "Gpt_5_4_Reasoning"
    if low.startswith("claude"):
        return "Claude_Sonnet"
    if "5.6" in low:
        return "Gpt_5_6_Reasoning"
    if "reason" in low or "think" in low or "deeper" in low:
        return "Gpt_5_5_Reasoning"
    return "Magic"


def tone_for_tools(tone: str, *, has_tools: bool) -> str:
    """Reasoning tones meta-refuse tool prompts — force Chat/Magic for tool turns."""
    if not has_tools:
        return tone if tone != "Gpt_Quick" else "Magic"
    if "Reasoning" in tone or tone.endswith("_Reasoning"):
        if tone.startswith("Gpt_5_6"):
            # Chat/Quick empty on current tenant — keep reasoning or fall to Magic
            return "Magic"
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

    When config advertise is non-empty, that list is the only public catalog.
    """
    if extra:
        return list(extra)
    return list(DEFAULT_MODELS)
