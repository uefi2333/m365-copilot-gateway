from __future__ import annotations

"""Model / tone catalog.

Tone strings validated against live M365 behavior in community research
(see cramt/m365-copilot-proxy model map). Unknown tones can fail server-side.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelInfo:
    id: str
    tone: str
    label: str
    family: str = "m365"


# Seed list — GET /v1/models may merge config + probe results.
DEFAULT_MODELS: list[ModelInfo] = [
    ModelInfo("m365-copilot", "Magic", "M365 Copilot (auto)", "auto"),
    ModelInfo("auto", "Magic", "Auto / Magic", "auto"),
    # Gpt_Quick often returns empty on enterprise substrate — map to Magic
    ModelInfo("quick", "Magic", "Quick (Magic)", "gpt"),
    ModelInfo("reasoning", "Gpt_Reasoning", "Think deeper", "gpt"),
    ModelInfo("think-deeper", "Gpt_Reasoning", "Think deeper", "gpt"),
    ModelInfo("claude", "Claude_Sonnet", "Claude Sonnet tone", "claude"),
    ModelInfo("claude-sonnet", "Claude_Sonnet", "Claude Sonnet (alias)", "claude"),
    ModelInfo("claude-sonnet-4.6", "Claude_Sonnet", "Claude Sonnet 4.6", "claude"),
    ModelInfo("claude-sonnet-think-deeper", "Claude_Sonnet_Reasoning", "Claude 3.5 Sonnet reasoning (old)", "claude"),
    ModelInfo("gpt-5.5", "Gpt_5_5_Chat", "GPT-5.5 chat", "gpt"),
    ModelInfo("gpt-5.5-think-deeper", "Gpt_5_5_Reasoning", "GPT-5.5 reasoning (alias)", "gpt"),
    ModelInfo("gpt-5.5-reasoning", "Gpt_5_5_Reasoning", "GPT-5.5 reasoning", "gpt"),
    ModelInfo("gpt-5.4", "Gpt_5_4_Reasoning", "GPT-5.4 reasoning (alias)", "gpt"),
    ModelInfo("gpt-5.4-reasoning", "Gpt_5_4_Reasoning", "GPT-5.4 reasoning", "gpt"),
    ModelInfo("gpt-5.4-quick", "Gpt_5_4_Quick", "GPT-5.4 quick", "gpt"),
    ModelInfo("gpt-5.3-quick", "Gpt_5_3_Quick", "GPT-5.3 quick", "gpt"),
    ModelInfo("gpt-5.3-think-deeper", "Gpt_5_3_Reasoning", "GPT-5.3 reasoning", "gpt"),
    ModelInfo("gpt-5.2-quick", "Gpt_5_2_Quick", "GPT-5.2 quick", "gpt"),
    ModelInfo("gpt-5.2-think-deeper", "Gpt_5_2_Reasoning", "GPT-5.2 reasoning", "gpt"),
]


def resolve_tone(model_id: str, catalog: list[ModelInfo] | None = None) -> str:
    cat = catalog or DEFAULT_MODELS
    for m in cat:
        if m.id == model_id:
            return m.tone
    if model_id.lower().startswith("claude"):
        return "Claude_Sonnet"
    return "Magic"


def tone_for_tools(tone: str, *, has_tools: bool) -> str:
    """Reasoning tones meta-refuse tool prompts — force Chat/Magic for tool turns.

    Connection tests + agent clients need tool_calls[], not analysis prose.
    """
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
    seen: dict[str, ModelInfo] = {m.id: m for m in DEFAULT_MODELS}
    for m in extra or []:
        seen[m.id] = m
    return list(seen.values())
