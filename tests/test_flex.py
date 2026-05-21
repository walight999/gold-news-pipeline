"""Structural sanity checks for Flex builders.

We don't validate against the full LINE schema — just confirm the shape is what
LINE accepts: type bubble/carousel, header+body present, contents non-empty,
url buttons capped, etc.
"""
from __future__ import annotations

from datetime import datetime, timezone

from src.dedup import Event
from src.line_flex import (
    alert_bubble,
    alt_text_for_event,
    breaking_bubble,
    digest_carousel,
    health_bubble,
)
from src.normalizer import Item


def _item(sid: str, title: str, url: str = "", summary: str = "") -> Item:
    ts = datetime(2026, 5, 20, 12, 0, 0, tzinfo=timezone.utc)
    return Item(source_id=sid, tier=1, role="macro", title=title, summary=summary,
                url=url or f"https://x/{sid}/{abs(hash(title))%9999}",
                published_ts=ts, first_seen_ts=ts)


def _ev(topic: str, direction: str, sources: list[str], title: str = "Hot CPI", summary: str = "summary") -> Event:
    items = [_item(s, f"{title} from {s}", summary=summary) for s in sources]
    return Event(event_id="e1", cluster_key="k", topic_bucket=topic,
                 entity="us", direction_label=direction, items=items)


def test_breaking_bubble_shape(kw_config):
    ev = _ev("inflation", "hawkish", ["forexlive", "bls"])
    b = breaking_bubble(ev, 5.0, kw_config)
    assert b["type"] == "bubble"
    assert "header" in b and "body" in b
    assert b["header"]["backgroundColor"] == "#DC2626"
    body_contents = b["body"]["contents"]
    assert len(body_contents) >= 3
    # Source link is anywhere in body — find any text component that's
    # clickable (has uri action). Emoji prefix was removed, so we just look
    # for the uri action.
    src_link = None
    def _walk(node):
        nonlocal src_link
        if src_link is not None:
            return
        if isinstance(node, dict):
            if node.get("type") == "text" and node.get("action", {}).get("type") == "uri":
                src_link = node
                return
            for c in node.get("contents", []) or []:
                _walk(c)
    for comp in body_contents:
        _walk(comp)
    assert src_link is not None, "expected clickable source link in body"
    assert src_link["action"]["uri"].startswith("https://")


def test_alert_bubble_shape(kw_config):
    ev = _ev("rate_policy", "dovish", ["fed"])
    b = alert_bubble(ev, 3.6, kw_config)
    assert b["type"] == "bubble"
    assert b["header"]["backgroundColor"] == "#D97706"


def test_digest_single_bubble_groups_by_topic(kw_config):
    evs = [
        _ev("inflation",   "hawkish", ["forexlive"]),
        _ev("inflation",   "neutral", ["marketwatch"]),
        _ev("geopolitics", "risk_off", ["bbc_world"]),
    ]
    for i, ev in enumerate(evs):
        ev.event_id = f"e{i}"
    scores = {ev.event_id: 3.0 + i * 0.1 for i, ev in enumerate(evs)}
    b = digest_carousel(evs, scores, "13:30", kw_config)
    assert b is not None
    # Single long bubble, not carousel
    assert b["type"] == "bubble"
    assert b["size"] == "giga"
    assert b["header"]["backgroundColor"] == "#2563EB"
    # Body should contain heading + rows for 2 distinct topics
    body_contents = b["body"]["contents"]
    headings = [c for c in body_contents
                if c.get("type") == "box" and c.get("layout") == "horizontal"
                and any(x.get("text", "").upper() in {"INFLATION", "GEOPOLITICS"}
                        for x in c.get("contents", []))]
    assert len(headings) == 2


def test_digest_splits_when_more_than_five(kw_config):
    """≤5 events stays single bubble; >5 splits into a 2-bubble carousel."""
    topics = ["inflation", "jobs", "rate_policy", "geopolitics", "usd_yields", "gold_flow", "other"]
    evs = []
    for i, t in enumerate(topics):
        ev = _ev(t, "neutral", ["forexlive"])
        ev.event_id = f"e{i}"
        evs.append(ev)
    scores = {ev.event_id: 3.0 for ev in evs}
    b = digest_carousel(evs, scores, "21:30", kw_config)
    assert b is not None
    # 7 events → carousel with 2 bubbles, 4/3 split
    assert b["type"] == "carousel"
    assert len(b["contents"]) == 2
    # First bubble carries the ceiling half
    # _digest_event_row is nested inside a topic-section box, so we count
    # the topic-section heading boxes that match topic names.
    def count_events(bubble):
        n = 0
        for sec in bubble["body"]["contents"]:
            if sec.get("type") == "box" and sec.get("layout") == "vertical":
                # this is _digest_event_row
                n += 1
        return n
    assert count_events(b["contents"][0]) == 4   # ceil(7/2)
    assert count_events(b["contents"][1]) == 3


def test_digest_no_split_at_five(kw_config):
    """Exactly 5 events stays single bubble."""
    evs = []
    for i in range(5):
        ev = _ev("inflation", "neutral", ["forexlive"])
        ev.event_id = f"e{i}"
        evs.append(ev)
    scores = {ev.event_id: 3.0 for ev in evs}
    b = digest_carousel(evs, scores, "13:30", kw_config)
    assert b["type"] == "bubble"


def test_health_bubble_shape():
    b = health_bubble([("forexlive", "tier2_no_item"), ("bls", "tier0_event_day_no_success")])
    assert b["type"] == "bubble"
    assert b["header"]["backgroundColor"] == "#6B7280"
    assert len(b["body"]["contents"]) == 2


def test_alt_text_truncates():
    ev = _ev("inflation", "hawkish", ["forexlive"], title="x" * 1000)
    t = alt_text_for_event("⚡ BREAKING", ev, 5.0)
    assert len(t) <= 380
