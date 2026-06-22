"""Tests for the 6-window full-detail news round (2026-06-22).

Covers:
  - digest.collect_window_events window/score/status/sent_log filtering + rank
  - dedup.serialize_event_for_store carrying title/summary/url
  - news_alert._cal_detail_from_text parsing + CJK guard
  - post_release_bubble rendering the Thai detail block
"""
from __future__ import annotations

from datetime import timedelta, timezone

from src import digest
from src.dedup import Event, serialize_event_for_store
from src.line_flex import post_release_bubble
from src.news_alert import _cal_detail_from_text
from src.normalizer import Item
from src.utils_time import iso_utc, now_utc


def _put_event(store, *, event_id, score, status="digest", title="CPI hot",
               minutes_ago=30):
    ts = now_utc() - timedelta(minutes=minutes_ago)
    store.upsert("event_state", {
        "event_id": event_id, "cluster_key": "k", "topic_bucket": "inflation",
        "entity": "us", "direction_label": "hawkish",
        "first_seen_ts": iso_utc(ts), "last_seen_ts": iso_utc(ts),
        "source_list": "forexlive,bls", "source_count": 2,
        "score": score, "status": status,
        "title": title, "summary": "summary text", "url": "https://x/a",
    })


def test_collect_window_basic_rank_and_score_gate(store):
    _put_event(store, event_id="lo", score=0.3)         # below floor
    _put_event(store, event_id="mid", score=1.0)
    _put_event(store, event_id="hi", score=2.5)
    rows = digest.collect_window_events(store, now_utc(), window_hours=4, min_score=0.5)
    ids = [r["event_id"] for r in rows]
    assert ids == ["hi", "mid"]            # score-ranked, "lo" gated out


def test_collect_window_excludes_breaking_and_alert(store):
    _put_event(store, event_id="b", score=5.0, status="breaking")
    _put_event(store, event_id="a", score=4.0, status="alert")
    _put_event(store, event_id="d", score=1.0, status="digest")
    rows = digest.collect_window_events(store, now_utc(), 4, 0.5)
    assert [r["event_id"] for r in rows] == ["d"]


def test_collect_window_time_window(store):
    _put_event(store, event_id="fresh", score=1.0, minutes_ago=30)
    _put_event(store, event_id="stale", score=2.0, minutes_ago=5 * 60)   # >4h
    rows = digest.collect_window_events(store, now_utc(), 4, 0.5)
    assert [r["event_id"] for r in rows] == ["fresh"]


def test_collect_window_dedup_against_sent_log(store):
    _put_event(store, event_id="seen", score=2.0)
    _put_event(store, event_id="new", score=1.0)
    # "seen" already went out in a prior round.
    store.upsert("sent_log", {"event_id": "seen", "route_type": "digest",
                              "sent_ts": iso_utc(now_utc()), "line_status": 200})
    rows = digest.collect_window_events(store, now_utc(), 4, 0.5)
    assert [r["event_id"] for r in rows] == ["new"]


def test_collect_window_requires_title(store):
    _put_event(store, event_id="notitle", score=2.0, title="")
    rows = digest.collect_window_events(store, now_utc(), 4, 0.5)
    assert rows == []


def test_serialize_event_carries_content():
    ts = now_utc()
    it = Item(source_id="bls", tier=1, role="macro",
              title="US CPI rises 3.4% y/y", summary="hotter than expected",
              url="https://example.com/cpi", published_ts=ts, first_seen_ts=ts)
    ev = Event(event_id="e1", cluster_key="k", topic_bucket="inflation",
               entity="us", direction_label="hawkish", items=[it])
    row = serialize_event_for_store(ev, 2.5, "digest")
    assert row["title"] == "US CPI rises 3.4% y/y"
    assert row["summary"] == "hotter than expected"
    assert row["url"] == "https://example.com/cpi"


def test_cal_detail_parse_and_cjk_guard():
    good = '{"detail_th": ["FOMC คงดอกเบี้ย", "หนุนทองระยะสั้น"]}'
    assert _cal_detail_from_text(good) == ["FOMC คงดอกเบี้ย", "หนุนทองระยะสั้น"]
    # CJK leak is dropped
    mixed = '{"detail_th": ["ปกติ", "避險 resume"]}'
    assert _cal_detail_from_text(mixed) == ["ปกติ"]
    # empty list → None so the card degrades gracefully
    assert _cal_detail_from_text('{"detail_th": []}') is None
    assert _cal_detail_from_text("not json") is None


class _Cal:
    title = "FOMC Rate Decision"
    country = "USD"
    impact = "High"
    forecast = "5.50%"
    previous = "5.50%"
    event_id = "fomc1"


def test_post_release_bubble_renders_detail():
    detail = ["Fed คงดอกเบี้ยที่ 5.50% ตามคาด", "ท่าทียัง hawkish กดดันทองระยะสั้น"]
    b = post_release_bubble(_Cal(), {"direction": "neutral"},
                            actual_text="5.50%", surprise="in-line",
                            detail_th=detail)
    texts = []

    def _walk(node):
        if isinstance(node, dict):
            if node.get("type") == "text":
                texts.append(node.get("text", ""))
            for c in node.get("contents", []) or []:
                _walk(c)
    for c in b["body"]["contents"]:
        _walk(c)
    assert any("คงดอกเบี้ยที่ 5.50%" in t for t in texts)
    assert any("hawkish" in t for t in texts)
