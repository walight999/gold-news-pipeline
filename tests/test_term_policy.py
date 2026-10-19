"""2026-10-19 term policy (owner-approved): which finance terms stay English,
which get a Thai gloss, which are written in Thai — and Fed is always "Fed"."""
from __future__ import annotations

from src.news_alert import _GRAMMAR_REPAIR_PROMPT, _SYSTEM_PROMPT
from src.thai_grammar import grammar_warnings
from src.translator import _patch_places


def test_prompt_carries_the_term_policy():
    static = _SYSTEM_PROMPT.split("INPUT:")[0]          # must sit in the cached block
    assert "TERM POLICY" in static
    for must in ("ยีลด์ (ผลตอบแทนพันธบัตร)", "ดัชนีดอลลาร์ (DXY)", "inflation → เงินเฟ้อ",
                 "risk-on → ตลาดกล้าเสี่ยง", "sentiment → บรรยากาศตลาด", "\"25 bps\" → \"0.25%\"",
                 "never \"เฟด\""):
        assert must in static, must
    # terms moved OUT of the keep-in-English list
    sentiment_line = next(l for l in static.splitlines() if l.startswith("Sentiment:"))
    for gone in ("risk-on", "yields", "forward guidance", "dot plot"):
        assert gone not in sentiment_line


def test_repair_prompt_matches_policy():
    assert "never \"เฟด\"" in _GRAMMAR_REPAIR_PROMPT
    assert "inflation (เงินเฟ้อ)" in _GRAMMAR_REPAIR_PROMPT
    assert "DXY, hawkish" not in _GRAMMAR_REPAIR_PROMPT      # old keep-list wording


def test_fed_transliteration_becomes_fed():
    cases = {
        "มุมมองจากเฟดอาจหนุนดอลลาร์": "มุมมองจาก Fed อาจหนุนดอลลาร์",
        "เฟดส่งสัญญาณ": "Fed ส่งสัญญาณ",
        "ท่าที (เฟด) ชัด": "ท่าที (Fed) ชัด",
        "ประธานเฟด": "ประธาน Fed",
        "เฟด": "Fed",
        "Fed ขึ้นดอกเบี้ย": "Fed ขึ้นดอกเบี้ย",          # already right: untouched
        "เฟดเอ็กซ์รายงานกำไร": "เฟดเอ็กซ์รายงานกำไร",      # FedEx stays
    }
    for src, want in cases.items():
        assert _patch_places(src) == want, src
        assert _patch_places(want) == want                # idempotent


def test_fed_swap_does_not_trip_latin_in_thai():
    assert "latin_in_thai" not in grammar_warnings(_patch_places("มุมมองจากเฟดอาจหนุนดอลลาร์"))
