"""Thai grammar / naturalness QC for rewritten card copy.

The classifier rewrite (news_alert) and the calendar explainer produce Thai that
occasionally reads like machine translation — literal English word order, leaked
English verbs/prepositions, or a sentence that ends mid-thought. This module is
the CHEAP, DETERMINISTIC first line: `grammar_warnings` scans a string for a
curated set of HIGH-PRECISION artifact patterns and returns short issue labels.

Design intent (2026-09-10 quality report — "ไวยากรณ์ไม่ไทยขนาดนั้น"):
- HIGH PRECISION over recall. Every pattern here is one that is almost always a
  literal-MT tell in finance copy, NOT a general heuristic — so a warning is
  actionable, not noise. We would rather miss a subtle case than cry wolf.
- Pure detection, ZERO cost, no LLM. The hybrid gate in news_alert uses these
  labels to decide whether to spend ONE LLM repair call on a flagged card.
- None of the flagged English words appear in the KEEP-IN-ENGLISH glossary
  (which is nouns / acronyms / set adjectives like hawkish, safe-haven), so an
  English-verb/preposition leak means an untranslated fragment slipped through.
"""
from __future__ import annotations

import re

# --- Known literal-MT Thai artifacts (verbatim tells; near-zero false positive) ---
_ARTIFACT_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"ดึงเอียง"), "mt:ดึงเอียง"),
    (re.compile(r"ในซื้อขาย"), "mt:ในซื้อขาย"),          # "ในซื้อขายวันศุกร์" / "ขึ้นในซื้อขาย"
    (re.compile(r"(?:ขึ้น|ลง|ปรับ)ในการ(?![ก-๙])"), "mt:...ในการ"),
    (re.compile(r"ผลกระทบของการเดินทาง"), "mt:การเดินทาง"),
    (re.compile(r"ตลาดเทพเจ้า|ตลาดทำนาย"), "mt:polymarket-mistranslated"),
]

# English function / motion verbs a literal translator leaves untranslated. NONE
# are in the keep-in-English glossary, so their presence = untranslated fragment.
_ENGLISH_LEAK = re.compile(
    r"\b(amid|amidst|weighing|weighs|weighed|dragging|dragged|drags|despite|"
    r"ahead\s+of|soaring|plunging|surging|tumbling|sparking|spurring|buoying|"
    r"denting|slipping|jumping|climbing|falling)\b", re.I)

# Buddhist-Era leak. Deliberately NOT a bare 25xx match — gold prices live in the
# 2500-4400 range, so "ทองแตะ 2563 ดอลลาร์" is a PRICE, not a year. Only flag the
# explicit era markers or an explicit "ปี 25xx".
_BUDDHIST_ERA = re.compile(r"พ\.ศ\.|\bBE\b|ปี\s?25[0-9]{2}")

# Sentence left dangling on a connector = a thought cut mid-way (the prompt bans
# mid-thought cut-offs). Only checked at end-of-string.
_DANGLING = re.compile(r"(?:และ|แต่|ที่|ของ|เพื่อ|กับ|จาก|โดย|ซึ่ง|หรือ)\s*$")


def grammar_warnings(text: str | None) -> list[str]:
    """Return distinct issue labels for `text`, or [] when it reads clean.
    Cheap + deterministic; safe to call on every card."""
    if not text:
        return []
    out: list[str] = []
    for pat, label in _ARTIFACT_PATTERNS:
        if pat.search(text):
            out.append(label)
    if _ENGLISH_LEAK.search(text):
        out.append("english_leak")
    if _BUDDHIST_ERA.search(text):
        out.append("buddhist_era")
    if _DANGLING.search(text.strip()):
        out.append("dangling")
    return out


def alert_grammar_warnings(headline: str | None, body: list[str] | None,
                           impact: str | None) -> list[str]:
    """Aggregate distinct warnings across every user-facing field of a rewrite.
    Preserves first-seen order so logs read consistently."""
    seen: dict[str, None] = {}
    for field in (headline, impact, *(body or [])):
        for w in grammar_warnings(field):
            seen.setdefault(w, None)
    return list(seen.keys())
