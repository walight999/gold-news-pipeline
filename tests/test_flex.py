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
    # Source link is anywhere in body — find any horizontal box containing a
    # clickable source text (📡 + uri action).
    src_link = None
    for comp in body_contents:
        if comp.get("type") != "box" or comp.get("layout") != "horizontal":
            continue
        for c in comp.get("contents", []):
            if c.get("type") == "text" and "📡" in c.get("text", "") and c.get("action", {}).get("type") == "uri":
                src_link = c
                break
        if src_link:
            break
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


def test_digest_single_bubble_handles_many_topics(kw_config):
    # 7 topics — single bubble shows them all (no carousel cap)
    topics = ["inflation", "jobs", "rate_policy", "geopolitics", "usd_yields", "gold_flow", "other"]
    evs = []
    for i, t in enumerate(topics):
        ev = _ev(t, "neutral", ["forexlive"])
        ev.event_id = f"e{i}"
        evs.append(ev)
    scores = {ev.event_id: 3.0 for ev in evs}
    b = digest_carousel(evs, scores, "21:30", kw_config)
    assert b is not None
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
