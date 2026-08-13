"""Automatic QC + the zero-touch weekly loop.

White's ask: the loop must not depend on him sitting down to review. auto_qc
encodes the defects a human keeps catching by eye (English headline, CJK leak,
raw glossary name, em-dash, wire-caps, empty body) as deterministic checks
over the week's sent copy — findings flow into the self-review card even in a
week with zero manual feedback. The Saturday delivery likewise needs no extra
scheduler: it rides the weekend heartbeat cron.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import yaml

from src import content_review
from src import main as main_mod

NOW = datetime(2026, 8, 15, 3, 30, tzinfo=timezone.utc)


def _sent_row(headline_th="ทองพุ่งหลังเงินเฟ้อต่ำคาด", body_th="ประเด็นแรก | ประเด็นสอง",
              impact_th="หนุนทองระยะสั้น"):
    ts = NOW + timedelta(hours=7) - timedelta(days=1)
    return {"ts_ict": ts.strftime("%Y-%m-%d %H:%M:%S"), "route": "digest",
            "decision": "sent", "event_id": "e1", "headline_th": headline_th,
            "body_th": body_th, "impact_th": impact_th,
            "fb_ok": "", "fb_type": "", "fb_fix": "", "fb_note": "", "flags": ""}


# ---- individual checks ----

def test_clean_thai_copy_raises_nothing():
    assert content_review.auto_qc([_sent_row()]) == {}


def test_english_headline_is_flagged_untranslated():
    hits = content_review.auto_qc(
        [_sent_row(headline_th="Gold surges after CPI comes in below forecast")])

    assert hits["untranslated"]["count"] == 1


def test_thai_headline_with_a_ticker_is_not_flagged():
    """CPI/FOMC abbreviations inside Thai text are normal, not 'English'."""
    hits = content_review.auto_qc(
        [_sent_row(headline_th="FOMC คงดอกเบี้ยตามคาด ตลาดจับตา CPI")])

    assert "untranslated" not in hits


def test_cjk_leak_is_flagged():
    hits = content_review.auto_qc([_sent_row(body_th="ตลาด避險กลับมา")])

    assert hits["cjk_leak"]["count"] == 1


def test_raw_glossary_name_is_flagged():
    hits = content_review.auto_qc(
        [_sent_row(body_th="ถ้อยแถลงของ Powell กดดันทอง")])

    assert hits["raw_name"]["count"] == 1
    assert "Powell" in hits["raw_name"]["examples"][0]


def test_translated_name_is_not_flagged():
    hits = content_review.auto_qc(
        [_sent_row(body_th="ถ้อยแถลงของพาวเวลล์กดดันทอง")])

    assert "raw_name" not in hits


def test_em_dash_is_flagged():
    hits = content_review.auto_qc([_sent_row(impact_th="กดดันทอง — ระยะสั้น")])

    assert hits["em_dash"]["count"] == 1


def test_wire_caps_headline_is_flagged():
    hits = content_review.auto_qc(
        [_sent_row(headline_th="EXPEDIA GROUP Q2 EPS $4.14 BEATS ESTIMATE")])

    assert hits["wire_caps"]["count"] == 1


def test_empty_body_is_flagged():
    hits = content_review.auto_qc([_sent_row(body_th="")])

    assert hits["empty_body"]["count"] == 1


def test_counts_accumulate_and_examples_are_capped_at_two():
    rows = [_sent_row(headline_th=f"Gold headline number {i} still in English")
            for i in range(4)]

    hits = content_review.auto_qc(rows)

    assert hits["untranslated"]["count"] == 4
    assert len(hits["untranslated"]["examples"]) == 2


# ---- integration into the weekly summary ----

def test_qc_findings_become_suggestions_without_any_human_feedback():
    s = content_review.analyze(
        [_sent_row(headline_th="Gold surges after hot CPI print today")],
        [], [], now=NOW)

    assert s["auto_qc"]["untranslated"]["count"] == 1
    assert any("QC อัตโนมัติ" in sg for sg in s["suggestions"])


def test_rejected_rows_are_not_qc_scanned():
    """Rejected candidates never shipped — their raw EN titles are expected."""
    row = _sent_row(headline_th="")
    row.update({"decision": "rejected", "en_title": "EXPEDIA GROUP Q2",
                "flags": "rejected:not_relevant"})

    s = content_review.analyze([row], [], [], now=NOW)

    assert s["auto_qc"] == {}


# ---- the Saturday piggyback gate ----

class _SentStore:
    def __init__(self, already=False):
        self.already = already

    def get(self, tab, key):
        return {"event_id": key[0]} if self.already else None


def _at(monkeypatch, dt_ict):
    monkeypatch.setattr(main_mod, "now_ict", lambda: dt_ict)


ICT = timezone(timedelta(hours=7))


def test_due_on_saturday_morning_after_10():
    wk = None

    class _M:
        pass
    import pytest  # noqa: F401
    # Sat 2026-08-15 10:05 ICT
    sat = datetime(2026, 8, 15, 10, 5, tzinfo=ICT)
    orig = main_mod.now_ict
    main_mod.now_ict = lambda: sat
    try:
        wk = main_mod._weekly_self_review_due(_SentStore())
    finally:
        main_mod.now_ict = orig

    assert wk == "2026-W33"


def test_not_due_saturday_before_10_or_on_a_weekday():
    orig = main_mod.now_ict
    try:
        main_mod.now_ict = lambda: datetime(2026, 8, 15, 9, 55, tzinfo=ICT)
        assert main_mod._weekly_self_review_due(_SentStore()) is None
        main_mod.now_ict = lambda: datetime(2026, 8, 14, 12, 0, tzinfo=ICT)  # Friday
        assert main_mod._weekly_self_review_due(_SentStore()) is None
    finally:
        main_mod.now_ict = orig


def test_sunday_catches_up_but_a_sent_week_does_not_repeat():
    orig = main_mod.now_ict
    try:
        sun = datetime(2026, 8, 16, 8, 0, tzinfo=ICT)
        main_mod.now_ict = lambda: sun
        assert main_mod._weekly_self_review_due(_SentStore()) == "2026-W33"
        assert main_mod._weekly_self_review_due(_SentStore(already=True)) is None
    finally:
        main_mod.now_ict = orig


# ---- shipped config ----

def test_social_drafts_are_restricted_to_breaking_and_alert():
    """867 drafts / 2 approvals (measured 2026-08-13): digest drafts were the
    bulk of the tweet_writer waste. The config must carry the restriction."""
    cfg = yaml.safe_load(open("config/schedule.yaml", encoding="utf-8"))

    assert cfg["social"]["draft_routes"] == ["breaking", "alert"]
