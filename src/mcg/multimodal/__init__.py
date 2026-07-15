"""Image/audio adapters for OpenAI-style multimodal content."""

from .adapter import MultimodalPart, extract_multimodal, render_multimodal_prompt

__all__ = [
    "MultimodalPart",
    "extract_multimodal",
    "render_multimodal_prompt",
]
