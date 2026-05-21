"""Thai translation for digest / breaking / alert content.

Backends (waterfall):
  1. Anthropic Claude Haiku — set ANTHROPIC_API_KEY to enable.
     Best quality for finance jargon ("patient stance" → "ท่าทีอดทน"
     instead of "ผู้ป่วยยืน").
  2. Google Translate via deep-translator — free, no key, used when the
     LLM call fails or the key isn't set. Good for long-form, mediocre
     on short finance titles.
  3. None — caller renders the English original.

Translation only — never AI summarisation or rephrasing.
"""
from __future__ import annotations

import logging
import os
import re

log = logging.getLogger(__name__)

_LLM_PROMPT = """Translate this English financial news to natural Thai. Rules:
- Faithful translation — preserve every fact, number, percentage, and entity.
- Use Thai financial journalism style, NOT literal word-for-word.
- Keep technical terms in English when standard (CPI, NFP, Fed, FOMC, ECB,
  BoJ, BoE, DXY, RSI, PMI, GDP, PCE, PPI etc.).
- Translate finance idioms naturally:
    "patient stance"        → "ท่าทีอดทน/รอดู"
    "soft landing"          → "เศรษฐกิจชะลอแบบนุ่มนวล"
    "hawkish"               → "ท่าทีเข้มงวด"
    "dovish"                → "ท่าทีผ่อนคลาย"
    "sticky inflation"      → "เงินเฟ้อที่อยู่ในระดับสูงต่อเนื่อง"
    "safe-haven bid"        → "แรงซื้อสินทรัพย์ปลอดภัย"
    "higher for longer"     → "ดอกเบี้ยสูงนานขึ้น"
    "rate cut/cuts"         → "ลดดอกเบี้ย"
    "rate hike/hikes"       → "ขึ้นดอกเบี้ย"
    "breaks support"        → "หลุดแนวรับ"
    "breaks resistance"     → "ทะลุแนวต้าน"
- Do NOT add commentary, preamble, or explanation.
- Output ONLY the Thai translation, nothing else.

English: {text}

Thai:"""


# Lazy clients — avoid the imports until we actually need them.
_anthropic_client = None
_google_instance = None


def _get_anthropic_client():
    global _anthropic_client
    if _anthropic_client is None:
        key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
        if not key:
            _anthropic_client = False
            return False
        try:
            import anthropic
            _anthropic_client = anthropic.Anthropic(api_key=key)
        except ImportError:
            log.warning("anthropic SDK not installed — falling back to Google")
            _anthropic_client = False
    return _anthropic_client


def _get_google():
    global _google_instance
    if _google_instance is None:
        try:
            from deep_translator import GoogleTranslator
            _google_instance = GoogleTranslator(source="en", target="th")
        except ImportError:
            log.warning("deep-translator not installed")
            _google_instance = False
    return _google_instance


def _has_cjk(text: str | None) -> bool:
    """True if `text` contains any Chinese / Japanese / Korean script
    characters. We want Thai-only output — Chinese leaks via Google
    Translate when the source RSS item carries Chinese names of officials
    or places (Reuters/Yahoo Finance does this a lot)."""
    if not text:
        return False
    for c in text:
        if "一" <= c <= "鿿":            # CJK Unified Ideographs
            return True
        if "぀" <= c <= "ヿ":            # Japanese Hiragana + Katakana
            return True
        if "가" <= c <= "힯":            # Korean Hangul
            return True
    return False


def _clean_translation(out: str | None) -> str | None:
    """Reject empty / CJK-leaked outputs. Caller falls back to next
    backend (or English original) when this returns None."""
    if not out:
        return None
    out = out.strip()
    if not out:
        return None
    if _has_cjk(out):
        log.info("translation rejected — CJK leak detected")
        return None
    return out


def _translate_claude(text: str) -> str | None:
    client = _get_anthropic_client()
    if not client:
        return None
    try:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            messages=[{"role": "user", "content": _LLM_PROMPT.format(text=text)}],
        )
        return _clean_translation(resp.content[0].text)
    except Exception as e:
        log.warning("claude translate failed: %s", e)
        return None


def _translate_google(text: str) -> str | None:
    g = _get_google()
    if not g:
        return None
    try:
        return _clean_translation(g.translate(text))
    except Exception as e:
        log.warning("google translate failed: %s", e)
        return None


def to_thai(text: str | None, max_len: int = 600) -> str | None:
    """Translate English to Thai. Tries Claude first (when key is set),
    falls back to Google. Returns None if both fail so callers can show
    the English original.

    Output is validated to ensure no Chinese / Japanese / Korean script
    leaks through (Google Translate passes through Chinese names from
    Reuters-style sources; Claude transliterates them properly to Thai)."""
    if not text:
        return None
    text = re.sub(r"\s+", " ", text).strip()[:max_len]
    if not text:
        return None
    return _translate_claude(text) or _translate_google(text)
