"""OpenAI image generation for post artwork (Twitter / Facebook).

Generates branded editorial artwork (e.g. a daily economic-calendar poster) to
attach to social posts. Uses the OpenAI Images API over raw HTTP (no extra SDK).
Env-gated on OPENAI_API_KEY, best-effort — returns None on any failure.

BRAND GUARDRAIL: the brand's no-ai-slop rule bans AI-generated human faces. Every
prompt appends BRAND_STYLE, which forbids realistic faces/people, real logos, and
gibberish text — artwork is illustrative/infographic, never a fake person.
"""
from __future__ import annotations

import base64
import logging
import os
from typing import Any

log = logging.getLogger("image_gen")

API_URL = "https://api.openai.com/v1/images/generations"
DEFAULT_MODEL = "gpt-image-1"          # override IMAGE_MODEL (e.g. dall-e-3)
DEFAULT_SIZE = "1024x1024"             # override IMAGE_SIZE (both models support this)

# Appended to every prompt. Editorial/infographic, brand palette, NO faces.
BRAND_STYLE = (
    " Style: clean modern editorial financial infographic poster, deep navy "
    "(#0B1F3A) and gold (#FFD34D) palette, subtle geometric line-art, high "
    "contrast, premium and minimal, social-media ready. Absolutely NO realistic "
    "human faces or people, NO real company logos or trademarks, NO gibberish "
    "text. Any labels must be short, correct English or numbers only."
)


def calendar_artwork_prompt(events: list[Any], date_label: str,
                            cap: int = 6) -> str:
    """Prompt for a 'daily economic calendar' artwork from the day's high-impact
    events. `events` are calendar.CalEvent (or anything with .hhmm_ict/.country/
    .title/.impact); falls back gracefully to dict-like items."""
    lines = []
    for e in events[:cap]:
        hhmm = getattr(e, "hhmm_ict", None) or (e.get("hhmm_ict") if isinstance(e, dict) else "")
        country = getattr(e, "country", None) or (e.get("country") if isinstance(e, dict) else "")
        title = getattr(e, "title", None) or (e.get("title") if isinstance(e, dict) else "")
        lines.append(f"{hhmm} {country} {title}".strip())
    body = "; ".join(x for x in lines if x) or "no major economic releases"
    return (
        f"A polished 'Economic Calendar — {date_label}' poster artwork for a "
        f"gold (XAU/USD) trading brand. Lay out the day's key macro releases as a "
        f"clean timeline/agenda: {body}. Use simple iconography (central-bank "
        f"columns, candlestick-free abstract charts, clocks, flag color-blocks) "
        f"and a clear title band." + BRAND_STYLE)


def brief_artwork_prompt(theme: str) -> str:
    """Prompt for a daily macro-theme artwork from the brief's theme sentence."""
    return (
        f"A daily gold-market macro artwork poster illustrating this theme: "
        f"{theme}. Convey it with abstract financial motifs (yield curves as "
        f"line-art, central-bank columns, arrows, gold bars as simple shapes)."
        + BRAND_STYLE)


def generate(prompt: str, *, api_key: str, size: str | None = None,
             model: str | None = None) -> bytes | None:
    """Generate one image, returning PNG bytes (or None). Handles both response
    shapes: gpt-image-1 returns b64_json; dall-e-3 may return a url (downloaded)."""
    if not (prompt and api_key):
        return None
    import httpx

    model = model or os.environ.get("IMAGE_MODEL", DEFAULT_MODEL)
    size = size or os.environ.get("IMAGE_SIZE", DEFAULT_SIZE)
    payload = {"model": model, "prompt": prompt, "size": size, "n": 1}
    try:
        with httpx.Client(timeout=120) as c:
            r = c.post(API_URL, headers={"Authorization": f"Bearer {api_key}"},
                       json=payload)
            r.raise_for_status()
            item = (r.json().get("data") or [{}])[0]
            if item.get("b64_json"):
                return base64.b64decode(item["b64_json"])
            if item.get("url"):
                img = c.get(item["url"])
                img.raise_for_status()
                return img.content
            log.warning("image_gen: no b64_json/url in response")
            return None
    except Exception:  # noqa: BLE001 — artwork is best-effort
        log.exception("image_gen: generation failed")
        return None


def save_png(data: bytes, path: str) -> bool:
    try:
        with open(path, "wb") as f:
            f.write(data)
        return True
    except Exception:  # noqa: BLE001
        log.exception("image_gen: save failed")
        return False
