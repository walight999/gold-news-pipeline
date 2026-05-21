"""Thai translation for digest event content.

Default backend: Google Translate via deep-translator (free, no API key).
Translation only — preserves original meaning. No AI rewriting / summary
generation; if the source has terse copy, the Thai version will be terse
too. Falls back to English original on any rate-limit / network error.

The 'patient stance' / 'cuts' style finance jargon does have known
translation quality issues. If quality becomes a blocker, swap the
backend to an LLM-with-explicit-translate-prompt — change `to_thai`
internals; the rest of the pipeline doesn't care.
"""
from __future__ import annotations

import logging
import re

log = logging.getLogger(__name__)

_instance = None


def _get_translator():
    global _instance
    if _instance is not None:
        return _instance
    try:
        from deep_translator import GoogleTranslator
        _instance = GoogleTranslator(source="en", target="th")
    except ImportError:
        log.warning("deep-translator not installed — Thai translation disabled")
        _instance = False
    return _instance


def to_thai(text: str | None, max_len: int = 600) -> str | None:
    """Translate English text to Thai. Returns None on empty input or any
    failure so callers can render the English original as fallback.

    `max_len` caps the request size (Google Translate truncates above ~5k
    chars; 600 is enough for a news summary and keeps the round-trip fast).
    """
    if not text:
        return None
    text = re.sub(r"\s+", " ", text).strip()[:max_len]
    if not text:
        return None
    inst = _get_translator()
    if not inst:
        return None
    try:
        out = inst.translate(text)
        return out.strip() if out else None
    except Exception as e:
        log.warning("translate_to_th failed (%s) — using EN fallback", e)
        return None
