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


def _count_event_cards(bubble):
    """Each digest event is a top-level vertical box in the bubble body."""
    return sum(1 for sec in bubble["body"]["contents"]
               if sec.get("type") == "box" and sec.get("layout") == "vertical")


def test_digest_single_bubble_up_to_three(kw_config):
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
    # 3 events → a single readable bubble (3 cards, no carousel)
    assert b["type"] == "bubble"
    assert b["size"] == "giga"
    assert b["header"]["backgroundColor"] == "#2563EB"
    assert _count_event_cards(b) == 3


def test_digest_paginates_at_three(kw_config):
    """>3 events split into a carousel of 3-card pages."""
    topics = ["inflation", "jobs", "rate_policy", "geopolitics", "usd_yields", "gold_flow", "other"]
    evs = []
    for i, t in enumerate(topics):
        ev = _ev(t, "neutral", ["forexlive"])
        ev.event_id = f"e{i}"
        evs.append(ev)
    scores = {ev.event_id: 3.0 + i * 0.01 for i, ev in enumerate(evs)}
    b = digest_carousel(evs, scores, "21:30", kw_config)
    assert b is not None
    # 7 events → 3 pages of 3/3/1
    assert b["type"] == "carousel"
    assert len(b["contents"]) == 3
    assert _count_event_cards(b["contents"][0]) == 3
    assert _count_event_cards(b["contents"][1]) == 3
    assert _count_event_cards(b["contents"][2]) == 1


def test_digest_caps_total_pages(kw_config):
    """More than PER_PAGE*MAX_PAGES events are capped to the max pages."""
    evs = []
    for i in range(40):
        ev = _ev("inflation", "neutral", ["forexlive"])
        ev.event_id = f"e{i}"
        evs.append(ev)
    scores = {ev.event_id: 3.0 for ev in evs}
    b = digest_carousel(evs, scores, "13:30", kw_config)
    assert b["type"] == "carousel"
    assert len(b["contents"]) == 5   # DIGEST_MAX_PAGES


def test_health_bubble_shape():
    b = health_bubble([("forexlive", "tier2_no_item"), ("bls", "tier0_event_day_no_success")])
    assert b["type"] == "bubble"
    assert b["header"]["backgroundColor"] == "#6B7280"
    assert len(b["body"]["contents"]) == 2


def test_alt_text_truncates():
    ev = _ev("inflation", "hawkish", ["forexlive"], title="x" * 1000)
    t = alt_text_for_event("⚡ BREAKING", ev, 5.0)
    assert len(t) <= 380
