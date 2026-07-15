from __future__ import annotations

import base64
import hashlib
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

_DATA_URL_RE = re.compile(
    r"^data:(?P<mime>[\w.+/-]+);base64,(?P<data>.+)$", re.DOTALL | re.IGNORECASE
)


@dataclass
class MultimodalPart:
    kind: str  # image | audio | file
    mime: str = ""
    url: str = ""
    b64: str = ""
    bytes_len: int = 0
    sha256: str = ""
    note: str = ""
    source: str = ""  # data_url | http | inline


@dataclass
class MultimodalBundle:
    text: str
    parts: list[MultimodalPart] = field(default_factory=list)

    @property
    def has_media(self) -> bool:
        return bool(self.parts)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


def _from_data_url(url: str, kind: str) -> MultimodalPart | None:
    m = _DATA_URL_RE.match(url.strip())
    if not m:
        return None
    mime = m.group("mime")
    raw_b64 = m.group("data").replace("\n", "").replace(" ", "")
    try:
        raw = base64.b64decode(raw_b64, validate=False)
    except Exception:
        return MultimodalPart(kind=kind, mime=mime, url=url[:80], note="invalid_base64")
    return MultimodalPart(
        kind=kind,
        mime=mime,
        b64=raw_b64[:80] + ("…" if len(raw_b64) > 80 else ""),
        bytes_len=len(raw),
        sha256=_sha(raw),
        note=f"inline {mime} {len(raw)} bytes",
        source="data_url",
        # keep full b64 for protocol attach (truncated in repr only above — store full)
    )


def _store_full_b64(part: MultimodalPart, url: str) -> MultimodalPart:
    m = _DATA_URL_RE.match(url.strip())
    if not m:
        return part
    part.b64 = m.group("data").replace("\n", "").replace(" ", "")
    return part


def _from_http_url(url: str, kind: str) -> MultimodalPart:
    path = urlparse(url).path
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    mime_guess = {
        "png": "image/png",
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "gif": "image/gif",
        "webp": "image/webp",
        "mp3": "audio/mpeg",
        "wav": "audio/wav",
        "ogg": "audio/ogg",
        "m4a": "audio/mp4",
    }.get(ext, "application/octet-stream")
    return MultimodalPart(
        kind=kind,
        mime=mime_guess,
        url=url,
        note=f"remote {kind} url",
        source="http",
    )


def parse_image_url_part(part: dict[str, Any]) -> MultimodalPart | None:
    img = part.get("image_url")
    if isinstance(img, str):
        url = img
    elif isinstance(img, dict):
        url = str(img.get("url") or "")
    else:
        url = ""
    if not url:
        return None
    if url.startswith("data:"):
        p = _from_data_url(url, "image")
        if p:
            return _store_full_b64(p, url)
        return None
    return _from_http_url(url, "image")


def parse_audio_part(part: dict[str, Any]) -> MultimodalPart | None:
    # OpenAI input_audio: {type, input_audio: {data, format}}
    audio = part.get("input_audio") or part.get("audio") or {}
    if isinstance(audio, dict):
        data = audio.get("data") or ""
        fmt = (audio.get("format") or "wav").lower()
        mime = f"audio/{fmt}" if "/" not in fmt else fmt
        if data:
            try:
                raw = base64.b64decode(data, validate=False)
            except Exception:
                raw = b""
            return MultimodalPart(
                kind="audio",
                mime=mime,
                b64=data if len(data) < 200_000 else data[:100] + "…",
                bytes_len=len(raw),
                sha256=_sha(raw) if raw else "",
                note=f"inline audio {mime} {len(raw)} bytes",
                source="inline",
            )
    url = part.get("url") or (part.get("audio_url") or {}).get("url")
    if isinstance(url, str) and url:
        if url.startswith("data:"):
            p = _from_data_url(url, "audio")
            return _store_full_b64(p, url) if p else None
        return _from_http_url(url, "audio")
    return None


def extract_from_content(content: Any) -> MultimodalBundle:
    """Parse OpenAI content (str | list parts) into text + media parts."""
    if content is None:
        return MultimodalBundle(text="")
    if isinstance(content, str):
        return MultimodalBundle(text=content)
    if not isinstance(content, list):
        return MultimodalBundle(text=str(content))

    texts: list[str] = []
    parts: list[MultimodalPart] = []
    for part in content:
        if not isinstance(part, dict):
            texts.append(str(part))
            continue
        ptype = part.get("type") or ""
        if ptype in ("text", "input_text") and part.get("text"):
            texts.append(str(part["text"]))
        elif ptype in ("image_url", "input_image", "image"):
            mp = parse_image_url_part(part)
            if mp:
                parts.append(mp)
        elif ptype in ("input_audio", "audio", "audio_url"):
            mp = parse_audio_part(part)
            if mp:
                parts.append(mp)
        elif ptype == "file" and part.get("file"):
            f = part["file"] if isinstance(part["file"], dict) else {}
            parts.append(
                MultimodalPart(
                    kind="file",
                    mime=str(f.get("mime") or "application/octet-stream"),
                    url=str(f.get("file_id") or f.get("url") or ""),
                    note="file ref",
                    source="inline",
                )
            )
        elif "text" in part and not ptype:
            texts.append(str(part["text"]))
    return MultimodalBundle(text="\n".join(t for t in texts if t), parts=parts)


def extract_multimodal(messages: list[dict[str, Any]]) -> list[MultimodalPart]:
    out: list[MultimodalPart] = []
    for m in messages:
        if m.get("role") not in ("user", "system"):
            continue
        bundle = extract_from_content(m.get("content"))
        out.extend(bundle.parts)
    return out


def render_multimodal_prompt(text: str, parts: list[MultimodalPart]) -> str:
    """Inject media descriptors into the Substrate text prompt.

    Substrate Bizchat accepts image-aware optionsSets (cwc_flux_image) but the
    public reverse-engineered invoke path is still primarily text. We:
    - keep user text
    - attach structured media blocks the model can reason over
    - for data-url images, include a short base64 head + hash so local tools /
      future protocol attach can match the same blob
    """
    if not parts:
        return text
    blocks: list[str] = []
    if text:
        blocks.append(text)
    blocks.append("[multimodal attachments for this turn]")
    for i, p in enumerate(parts, 1):
        line = (
            f"{i}. kind={p.kind} mime={p.mime or '?'} source={p.source} "
            f"bytes={p.bytes_len or '?'} sha256={p.sha256 or '-'}"
        )
        if p.url and p.source == "http":
            line += f" url={p.url}"
        if p.note:
            line += f" ({p.note})"
        blocks.append(line)
        if p.kind == "image" and p.source == "data_url" and p.b64:
            # Do not dump multi-MB blobs into prompt; small thumb-size OK
            if p.bytes_len and p.bytes_len <= 48_000:
                blocks.append(
                    f"   data_url=data:{p.mime};base64,{p.b64}"
                )
            else:
                blocks.append(
                    "   (image payload held server-side; describe from context if needed)"
                )
        if p.kind == "audio":
            blocks.append(
                "   (audio attached — if you cannot hear it, ask user to summarize "
                "or use a transcription tool if available)"
            )
    blocks.append(
        "If you can analyze the attached image/audio, do so. "
        "Otherwise acknowledge the attachment metadata and answer from text."
    )
    return "\n".join(blocks)


def substrate_message_extras(parts: list[MultimodalPart]) -> dict[str, Any]:
    """Optional fields merged into Substrate message object.

    Best-effort: some tenants ignore unknown keys; known image-friendly options
    are already enabled via DEFAULT_OPTIONS_SETS (cwc_flux_image).
    """
    images = [p for p in parts if p.kind == "image" and p.b64 and p.bytes_len <= 2_000_000]
    if not images:
        # remote URLs only
        urls = [p.url for p in parts if p.kind == "image" and p.url]
        if not urls:
            return {}
        return {
            "imageUrl": urls[0],
            "entityAnnotationTypes": ["People", "File", "Event", "Email", "TeamsMessage"],
        }
    # Prefer first inline image as base64 payload (community clients vary on key names)
    img = images[0]
    return {
        "imageBase64": img.b64,
        "imageContentType": img.mime or "image/png",
        "hiddenText": f"[user uploaded image {img.sha256}]",
    }
