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
    ModelInfo("quick", "Gpt_Quick", "Quick", "gpt"),
    ModelInfo("reasoning", "Gpt_Reasoning", "Think deeper", "gpt"),
    ModelInfo("think-deeper", "Gpt_Reasoning", "Think deeper", "gpt"),
    ModelInfo("claude", "Claude_Sonnet", "Claude Sonnet tone", "claude"),
    ModelInfo("claude-sonnet", "Claude_Sonnet", "Claude Sonnet tone", "claude"),
    ModelInfo("claude-sonnet-think-deeper", "Claude_Sonnet_Reasoning", "Claude Sonnet reasoning", "claude"),
    ModelInfo("claude-opus", "Claude_Opus", "Claude Opus tone", "claude"),
    ModelInfo("gpt-5.5", "Gpt_5_5_Chat", "GPT-5.5 chat tone", "gpt"),
    ModelInfo("gpt-5.5-think-deeper", "Gpt_5_5_Reasoning", "GPT-5.5 reasoning tone", "gpt"),
    ModelInfo("gpt-5.4", "Gpt_5_4_Reasoning", "GPT-5.4", "gpt"),
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


def list_models(extra: list[ModelInfo] | None = None) -> list[ModelInfo]:
    seen: dict[str, ModelInfo] = {m.id: m for m in DEFAULT_MODELS}
    for m in extra or []:
        seen[m.id] = m
    return list(seen.values())
