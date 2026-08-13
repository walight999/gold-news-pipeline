"""content_log — the reviewable ledger of what the channel actually said.

sent_log keeps only ids and statuses; social_feed keeps Thai copy but only for
delivered cards, in the X publishing queue, with no correction columns. This
tab is where the operator's quality feedback (typos, translation, tone,
display, wrong impact, wrongly-rejected) accumulates — so it must be
append-only (flush() never touches it) and must never fail the news run.
"""
from __future__ import annotations

from src import content_log


class _FeedStore:
    def __init__(self, fail: bool = False):
        self.fail = fail
        self.appended: list[tuple[str, list[str], list[list]]] = []

    def append_feed(self, tab, headers, rows):
        if self.fail:
            raise RuntimeError("sheets down")
        self.appended.append((tab, headers, rows))


def test_sent_record_carries_the_full_thai_copy():
    rec = content_log.record_card(
        route="breaking", decision="sent", event_id="e1",
        topic_bucket="inflation", score=4.7,
        category="Fed", tone="hawkish", impact_level="HIGH",
        headline_th="เงินเฟ้อสหรัฐพุ่งเกินคาด",
        body_th=["CPI +3.9% y/y", "ตลาดลดคาดการณ์ลดดอกเบี้ย"],
        impact_th="กดดันทองระยะสั้น",
        en_title="US CPI comes in hot", source="forexlive,bls")

    assert rec["decision"] == "sent"
    assert rec["headline_th"] == "เงินเฟ้อสหรัฐพุ่งเกินคาด"
    assert rec["body_th"] == "CPI +3.9% y/y | ตลาดลดคาดการณ์ลดดอกเบี้ย"
    assert rec["en_title"] == "US CPI comes in hot"


def test_feedback_columns_start_blank_for_the_operator():
    rec = content_log.record_card(route="digest", decision="sent", event_id="e")

    assert (rec["fb_ok"], rec["fb_type"], rec["fb_fix"], rec["fb_note"]) == ("", "", "", "")


def test_rejected_record_keeps_the_reason_in_flags():
    rec = content_log.record_card(
        route="digest", decision="rejected", event_id="e2",
        en_title="EXPEDIA GROUP Q2 EPS $4.14",
        flags="rejected:not_gold_relevant relevance=none")

    assert rec["decision"] == "rejected"
    assert "not_gold_relevant" in rec["flags"]
    assert rec["headline_th"] == ""      # nothing was composed for it


def test_every_record_key_has_a_header_column():
    """A field without a column would silently vanish on append (rows are
    built by indexing CONTENT_HEADERS) — the store-schema gotcha, again."""
    rec = content_log.record_card(route="alert", decision="sent", event_id="e")

    assert set(rec) == set(content_log.CONTENT_HEADERS)


def test_flush_appends_rows_in_header_order():
    store = _FeedStore()
    rec = content_log.record_card(
        route="breaking", decision="sent", event_id="e1",
        headline_th="หัวข่าว")

    n = content_log.flush(store, [rec])

    assert n == 1
    tab, headers, rows = store.appended[0]
    assert tab == "content_log"
    assert headers == content_log.CONTENT_HEADERS
    assert rows[0][headers.index("headline_th")] == "หัวข่าว"
    assert rows[0][headers.index("event_id")] == "e1"


def test_flush_swallows_sheet_errors_because_the_push_already_happened():
    """The card is already on LINE when this runs — raising here would fail a
    run whose actual work succeeded. Returns 0 and logs instead."""
    n = content_log.flush(_FeedStore(fail=True), [
        content_log.record_card(route="digest", decision="sent", event_id="e")])

    assert n == 0


def test_flush_with_nothing_to_write_is_a_no_op():
    store = _FeedStore()

    assert content_log.flush(store, []) == 0
    assert store.appended == []


def test_empty_bullets_are_dropped_from_the_join():
    rec = content_log.record_card(
        route="alert", decision="sent", event_id="e",
        body_th=["จุดแรก", "", "  ", "จุดสอง"])

    assert rec["body_th"] == "จุดแรก | จุดสอง"
