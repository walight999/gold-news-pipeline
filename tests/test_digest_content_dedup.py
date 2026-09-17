"""Content-level dedup for the 6-window digest.

Root cause (confirmed on the live sheet 2026-09-17): event_id keys on a 60-min
time bucket (dedup.CLUSTER_WINDOW_MIN), so the SAME wire headline re-posted
across a bucket boundary — e.g. "Trump hopes Iran war nearing end…" first seen
08:45 then 09:35, cluster_keys identical except T0800 vs T0900 — gets a FRESH
event_id. The digest dedup was event_id-only, so it shipped the identical story
as two cards. These tests pin the content backstop that closes that gap without
merging genuinely different headlines.
"""
from __future__ import annotations

from datetime import timedelta

from src import delivery_stats, digest
from src.utils_time import iso_utc, now_utc

_TRUMP = "Trump hopes Iran war nearing end as Houthi-Saudi fighting escalates"


def _put_event(store, *, event_id, title, score=1.0, status="digest",
               minutes_ago=30, bucket="geopolitics"):
    ts = now_utc() - timedelta(minutes=minutes_ago)
    store.upsert("event_state", {
        "event_id": event_id, "cluster_key": "k", "topic_bucket": bucket,
        "entity": "mideast", "direction_label": "risk_off",
        "first_seen_ts": iso_utc(ts), "last_seen_ts": iso_utc(ts),
        "source_list": "investing_commodities", "source_count": 1,
        "score": score, "status": status,
        "title": title, "summary": "s", "url": "https://x/a",
    })


def _mark_content_sent(store, title):
    store.upsert("sent_log", {
        "event_id": f"content:{digest.content_sig(title)}",
        "route_type": "content", "sent_ts": iso_utc(now_utc()), "line_status": 200,
    })


# --- content_sig ----------------------------------------------------------

def test_content_sig_collapses_case_and_punctuation():
    assert digest.content_sig(_TRUMP) == digest.content_sig(_TRUMP.lower() + "!!!")
    assert digest.content_sig("  TRUMP   HOPES ") == digest.content_sig("trump hopes")


def test_content_sig_keeps_genuinely_different_headlines_distinct():
    # Different refineries / different releases must NOT collapse to one sig.
    syzran = "ZELENSKYY ANNOUNCES UKRAINE HIT RUSSIA'S SYZRAN OIL REFINERY."
    yaroslavl = "ZELENSKYY ANNOUNCES UKRAINE ATTACKED RUSSIA'S YAROSLAVL REFINERY."
    assert digest.content_sig(syzran) != digest.content_sig(yaroslavl)
    assert digest.content_sig("UK PPI INPUT PRICES MOM 0.3%") != \
        digest.content_sig("UK PPI OUTPUT PRICES MOM 0.7%")


def test_content_sig_empty_is_no_dedup():
    assert digest.content_sig("") == ""
    assert digest.content_sig("   ") == ""


# --- cross-round dedup in collect_window_events ---------------------------

def test_same_headline_under_a_new_event_id_is_dropped(store):
    """The exact bug: identical headline, DIFFERENT event_id (bucket boundary),
    the first copy already sent → the second must not resurface."""
    _mark_content_sent(store, _TRUMP)
    _put_event(store, event_id="b016dec477", title=_TRUMP, score=2.0)

    rows = digest.collect_window_events(store, now_utc(), 4, 0.5)

    assert rows == []


def test_a_different_headline_is_still_kept(store):
    """A similar-but-different story (different refinery) must NOT be swept up by
    another headline's content marker — no over-merge."""
    _mark_content_sent(store, "ZELENSKYY ANNOUNCES UKRAINE HIT RUSSIA'S SYZRAN OIL REFINERY.")
    _put_event(store, event_id="new1",
               title="ZELENSKYY ANNOUNCES UKRAINE ATTACKED RUSSIA'S YAROSLAVL REFINERY.")

    rows = digest.collect_window_events(store, now_utc(), 4, 0.5)

    assert [r["event_id"] for r in rows] == ["new1"]


def test_no_marker_means_no_dedup(store):
    """Without a prior send, the event flows through unchanged (regression guard
    that the content check never drops a first-time story)."""
    _put_event(store, event_id="first", title=_TRUMP)

    rows = digest.collect_window_events(store, now_utc(), 4, 0.5)

    assert [r["event_id"] for r in rows] == ["first"]


# --- delivery metrics untouched by the marker -----------------------------

def test_delivery_stats_ignores_content_markers():
    """The content marker is a dedup key, not a delivery — counting it would
    double every digest event in n_sent."""
    ts = iso_utc(now_utc())
    rows = [
        {"event_id": "e1", "route_type": "digest", "sent_ts": ts, "line_status": "200"},
        {"event_id": "content:abc", "route_type": "content", "sent_ts": ts, "line_status": "200"},
    ]
    cutoff = now_utc() - timedelta(days=30)
    out = delivery_stats.aggregate(rows, cutoff)

    assert len(out) == 1
    assert out[0]["n_sent"] == 1          # digest counted, content ignored
    assert "content" not in out[0]["by_route"]
