"""Weekly self-review — the rules must cite evidence, not vibes.

Every suggestion in the 1:1 card traces back to sheet rows: fb_* annotations
in content_log, delivery outcomes in sent_log, and 30-min XAU moves in
calibration_log. These tests pin each rule's trigger condition and, just as
importantly, that a quiet week produces NO suggestions instead of filler.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src import content_review

NOW = datetime(2026, 8, 15, 3, 30, tzinfo=timezone.utc)


def _content(days_ago=1.0, decision="sent", fb_ok="", fb_type="", fb_fix="",
             flags="", en_title="US CPI hot", headline_th="เงินเฟ้อร้อน"):
    ts = (NOW + timedelta(hours=7) - timedelta(days=days_ago))
    return {
        "ts_ict": ts.strftime("%Y-%m-%d %H:%M:%S"),
        "route": "digest", "decision": decision, "event_id": f"e-{days_ago}-{decision}-{fb_type}",
        "topic_bucket": "inflation", "score": "1.5",
        "headline_th": headline_th, "en_title": en_title,
        "flags": flags, "fb_ok": fb_ok, "fb_type": fb_type, "fb_fix": fb_fix,
        "fb_note": "",
    }


def _sent(days_ago=1.0, route="digest", status="200", event_id=None):
    ts = NOW - timedelta(days=days_ago)
    return {"event_id": event_id or f"s-{days_ago}-{route}", "route_type": route,
            "sent_ts": ts.strftime("%Y-%m-%dT%H:%M:%S+00:00"),
            "line_status": status}


def _an(content=(), sent=(), calib=()):
    return content_review.analyze(list(content), list(sent), list(calib), now=NOW)


# ---- volume from sent_log ----

def test_week_volume_counts_delivered_cards_by_route():
    s = _an(sent=[_sent(1, "breaking"), _sent(2, "digest"), _sent(3, "digest")])

    assert s["by_route"] == {"breaking": 1, "digest": 2}
    assert (s["n_cards"], s["n_failed"]) == (3, 0)


def test_failures_and_round_markers_are_separated_out():
    s = _an(sent=[
        _sent(1, "digest", status="429"),
        _sent(1, "digest", event_id="digest:2026-08-14_08:30"),  # round marker
    ])

    assert s["n_cards"] == 0
    assert s["n_failed"] == 1


def test_rows_older_than_the_week_are_ignored():
    s = _an(sent=[_sent(10, "breaking")],
            content=[_content(days_ago=10, fb_ok="n", fb_type="typo")])

    assert s["n_cards"] == 0
    assert s["n_reviewed"] == 0


# ---- feedback parsing ----

def test_fb_ok_yes_and_no_are_both_counted_as_reviewed():
    s = _an(content=[_content(fb_ok="y"), _content(fb_ok="n", fb_type="typo")])

    assert s["n_reviewed"] == 2
    assert (s["n_fb_ok"], s["n_fb_issue"]) == (1, 1)
    assert s["issues_by_type"] == {"typo": 1}


def test_a_filled_fb_type_counts_as_a_review_even_without_fb_ok():
    s = _an(content=[_content(fb_type="translation")])

    assert s["n_fb_issue"] == 1


def test_unreviewed_rows_are_ledgered_but_not_reviewed():
    s = _an(content=[_content(), _content(decision="rejected",
                                          flags="rejected:not_relevant")])

    assert (s["n_logged_sent"], s["n_logged_rejected"]) == (1, 1)
    assert s["n_reviewed"] == 0
    assert s["reject_reasons"] == {"not_relevant": 1}


# ---- the suggestion rules ----

def test_missed_rows_produce_the_top_suggestion():
    s = _an(content=[
        _content(decision="rejected", fb_type="missed",
                 flags="rejected:not_gold_relevant"),
    ])

    assert s["n_missed"] == 1
    assert any("ไม่ควรตัด" in sg for sg in s["suggestions"])
    assert any("not_gold_relevant" in sg for sg in s["suggestions"])


def test_issue_flag_on_a_rejected_row_counts_as_missed_too():
    """The operator marking n on a rejected row can only mean one thing —
    it should have gone out."""
    s = _an(content=[_content(decision="rejected", fb_ok="n",
                              flags="rejected:low_relevance")])

    assert s["n_missed"] == 1


def test_repeated_language_issues_propose_glossary_from_fb_fix():
    s = _an(content=[
        _content(fb_ok="n", fb_type="translation", fb_fix="พาวเวลล์"),
        _content(fb_ok="n", fb_type="typo", fb_fix="อัตราผลตอบแทน"),
    ])

    sg = " ".join(s["suggestions"])
    assert "glossary" in sg
    assert "พาวเวลล์" in sg or "อัตราผลตอบแทน" in sg


def test_one_language_issue_is_not_enough_to_nag():
    s = _an(content=[_content(fb_ok="n", fb_type="typo", fb_fix="x")])

    assert not any("glossary" in sg for sg in s["suggestions"])


def test_rejected_events_the_tape_moved_on_raise_the_filter_question():
    content = [
        _content(days_ago=1, decision="rejected", flags="rejected:not_relevant"),
        _content(days_ago=2, decision="rejected", flags="rejected:not_relevant"),
    ]
    calib = [{"event_id": r["event_id"], "xau_return_30m": v}
             for r, v in zip(content, ("0.31", "-0.22"))]

    s = _an(content=content, calib=calib)

    assert len(s["high_move_rejects"]) == 2
    assert s["high_move_rejects"][0]["r30"] == 0.31          # largest first
    assert any("relevance gate" in sg for sg in s["suggestions"])


def test_small_moves_on_rejects_do_not_trigger_the_filter_rule():
    content = [_content(decision="rejected", flags="rejected:r")]
    calib = [{"event_id": content[0]["event_id"], "xau_return_30m": "0.05"}]

    s = _an(content=content, calib=calib)

    assert s["high_move_rejects"] == []


def test_degraded_cards_and_delivery_failures_each_get_a_line():
    s = _an(content=[_content(flags="fallback")],
            sent=[_sent(1, "digest", status="429")])

    sg = " ".join(s["suggestions"])
    assert "fallback" in sg
    assert "ส่งไม่สำเร็จ" in sg


def test_no_feedback_at_all_gets_the_nudge():
    s = _an(content=[_content(), _content(decision="rejected", flags="rejected:r")])

    assert any("ยังไม่มี feedback" in sg for sg in s["suggestions"])


def test_a_clean_reviewed_week_produces_zero_suggestions():
    """No filler advice: everything reviewed as ok, everything delivered."""
    s = _an(content=[_content(fb_ok="y")], sent=[_sent(1, "digest")])

    assert s["suggestions"] == []


def test_suggestions_are_capped():
    content = (
        [_content(days_ago=d, decision="rejected", fb_type="missed",
                  flags="rejected:r") for d in (1, 2)]
        + [_content(days_ago=3, fb_ok="n", fb_type="translation", fb_fix="a"),
           _content(days_ago=4, fb_ok="n", fb_type="typo", fb_fix="b"),
           _content(days_ago=5, fb_ok="n", fb_type="impact"),
           _content(days_ago=5.1, fb_ok="n", fb_type="impact"),
           _content(days_ago=5.2, fb_ok="n", fb_type="display"),
           _content(days_ago=5.3, fb_ok="n", fb_type="display"),
           _content(days_ago=6, flags="degraded")]
    )

    s = _an(content=content, sent=[_sent(1, "digest", status="0")])

    assert len(s["suggestions"]) <= content_review.MAX_SUGGESTIONS


# ---- bubble smoke ----

def test_bubble_renders_suggestions_and_week_label():
    from src.line_flex import content_review_bubble
    s = _an(content=[_content(decision="rejected", fb_type="missed",
                              flags="rejected:not_gold_relevant")])

    b = content_review_bubble(s, "09/08-15/08")

    texts = []

    def _walk(node):
        if isinstance(node, dict):
            if node.get("type") == "text":
                texts.append(node.get("text", ""))
            for c in node.get("contents", []) or []:
                _walk(c)
    _walk(b["header"])
    _walk(b["body"])
    assert any("09/08-15/08" in t for t in texts)
    assert any("ไม่ควรตัด" in t for t in texts)
