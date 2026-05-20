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
    # body must have a title + summary + chip row + source line
    body_contents = b["body"]["contents"]
    assert len(body_contents) >= 3
    # footer button must have URL
    assert "footer" in b
    btn = b["footer"]["contents"][0]
    assert btn["action"]["type"] == "uri"
    assert btn["action"]["uri"].startswith("https://")


def test_alert_bubble_shape(kw_config):
    ev = _ev("rate_policy", "dovish", ["fed"])
    b = alert_bubble(ev, 3.6, kw_config)
    assert b["type"] == "bubble"
    assert b["header"]["backgroundColor"] == "#D97706"


def test_digest_carousel_groups_by_topic(kw_config):
    evs = [
        _ev("inflation",   "hawkish", ["forexlive"]),
        _ev("inflation",   "neutral", ["marketwatch"]),
        _ev("geopolitics", "risk_off", ["bbc_world"]),
    ]
    # mutate event_ids so they're unique enough for scores map
    for i, ev in enumerate(evs):
        ev.event_id = f"e{i}"
    scores = {ev.event_id: 3.0 + i * 0.1 for i, ev in enumerate(evs)}
    c = digest_carousel(evs, scores, "13:30", kw_config)
    assert c is not None
    assert c["type"] == "carousel"
    # 2 unique topics -> 2 bubbles
    assert len(c["contents"]) == 2
    for bubble in c["contents"]:
        assert bubble["type"] == "bubble"
        assert bubble["header"]["backgroundColor"] == "#2563EB"


def test_digest_carousel_caps_bubbles(kw_config):
    # 7 different topics -> capped at CARRIER_MAX_BUBBLES (5)
    topics = ["inflation", "jobs", "rate_policy", "geopolitics", "usd_yields", "gold_flow", "other"]
    evs = []
    for i, t in enumerate(topics):
        ev = _ev(t, "neutral", ["forexlive"])
        ev.event_id = f"e{i}"
        evs.append(ev)
    scores = {ev.event_id: 3.0 for ev in evs}
    c = digest_carousel(evs, scores, "21:30", kw_config)
    assert c is not None
    assert len(c["contents"]) <= 5


def test_health_bubble_shape():
    b = health_bubble([("forexlive", "tier2_no_item"), ("bls", "tier0_event_day_no_success")])
    assert b["type"] == "bubble"
    assert b["header"]["backgroundColor"] == "#6B7280"
    assert len(b["body"]["contents"]) == 2


def test_alt_text_truncates():
    ev = _ev("inflation", "hawkish", ["forexlive"], title="x" * 1000)
    t = alt_text_for_event("⚡ BREAKING", ev, 5.0)
    assert len(t) <= 380
