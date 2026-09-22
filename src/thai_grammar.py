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
    r"denting|slipping|jumping|climbing|falling|eyeing|spooking|roiling|"
    r"underpinning|bolstering|capping|fuelling|fueling|rattling|paring)\b", re.I)

# Buddhist-Era leak. Deliberately NOT a bare 25xx match — gold prices live in the
# 2500-4400 range, so "ทองแตะ 2563 ดอลลาร์" is a PRICE, not a year. Only flag the
# explicit era markers or an explicit "ปี 25xx".
_BUDDHIST_ERA = re.compile(r"พ\.ศ\.|\bBE\b|ปี\s?25[0-9]{2}")

# Sentence left dangling on a connector = a thought cut mid-way (the prompt bans
# mid-thought cut-offs). Only checked at end-of-string.
_DANGLING = re.compile(r"(?:และ|แต่|ที่|ของ|เพื่อ|กับ|จาก|โดย|ซึ่ง|หรือ)\s*$")

# --- Structural garble (rendering / token corruption; ~zero false positive) ---
#
# These catch the "ไม่แม่น" output that is BROKEN Thai at the character level —
# a class the semantic patterns above miss. They cannot catch a well-formed but
# WRONG word (a real non-word like "ละลัง" is orthographically legal, so only a
# dictionary or the LLM repair can catch that), but they reliably fire on the
# corruption modes an MT/LLM rewrite actually produces.
#
# 1. Orphan combining mark — a Thai tone/upper/below mark that has no base
#    consonant before it (it sits at string start or right after a space, digit,
#    or ASCII letter). Valid Thai ALWAYS attaches these marks to a preceding
#    consonant, so an orphan is a corruption. The spacing vowels เ แ โ ใ ไ ะ า ำ
#    are deliberately excluded — they are not combining marks.
_ORPHAN_MARK = re.compile(r"(?:^|[\s\dA-Za-z])[ัิ-ฺ็-๎]")

# 2. Stacked tone marks — two or more tone marks in a row (่ ้ ๊ ๋). A syllable
#    carries at most one tone mark, so a run is always garble.
_DOUBLE_TONE = re.compile(r"[่-๋]{2,}")

# 3. Consonant stutter — the same Thai consonant three+ times in a row. Real Thai
#    never triples a consonant (two can straddle a syllable boundary, e.g. นกกระ,
#    so the run must be ≥3 to stay high-precision). อ is EXCLUDED: it legitimately
#    triples across a word boundary (ต่อออนซ์ = ต่อ+ออนซ์, รอออก) because it doubles
#    as a vowel carrier — including it fired a false positive on real desk Thai.
_STUTTER = re.compile(r"((?!อ)[ก-ฮ])\1\1")

# 4. Latin embedded INSIDE a Thai word — a run of Latin letters with a Thai
#    character glued on BOTH sides and no space (e.g. "ทองassetไหล"). A desk
#    writer always spaces kept-English terms ("ทอง asset ไหล"), so a fully-wrapped
#    Latin island is a tokenisation leak. Adjacent forms ("ทอง CPI") keep their
#    space and never match.
_LATIN_IN_THAI = re.compile(r"[฀-๿][A-Za-z]{2,}[฀-๿]")


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
    if _ORPHAN_MARK.search(text) or _DOUBLE_TONE.search(text):
        out.append("malformed_thai")
    if _STUTTER.search(text):
        out.append("stutter")
    if _LATIN_IN_THAI.search(text):
        out.append("latin_in_thai")
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
