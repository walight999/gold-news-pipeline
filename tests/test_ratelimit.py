"""§6.5 rate-limit: 8 breaking in 10m → 5 sent, 3 downgrade to digest, always-pass uncapped."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.dedup import Event
from src.normalizer import Item
from src.router import Route, decide


def _make_event(eid: str, topic: str, sources: list[str], age_min: int = 1) -> tuple[Event, float]:
    anchor = datetime.now(timezone.utc) - timedelta(minutes=age_min)
    items = [Item(source_id=s, tier=1, role="macro", title=f"hot {topic} from {s}",
                  summary="", url=f"https://x/{s}/{eid}", published_ts=anchor,
                  first_seen_ts=anchor) for s in sources]
    ev = Event(event_id=eid, cluster_key=f"k{eid}", topic_bucket=topic,
               entity="us", direction_label="neutral", items=items)
    return ev, 5.0  # forced score for routing logic


def test_overflow_downgrades_to_digest(store):
    events = []
    scores = {}
    for i in range(8):
        ev, sc = _make_event(eid=f"e{i}", topic="rate_policy", sources=[f"feed{i}", f"alt{i}"])
        events.append(ev)
        scores[ev.event_id] = 4.6  # breaking band, NOT score 5 confirmed scheduled
    decisions = decide(events, scores, store, rate_limit_window_min=15, rate_limit_max=5)
    breaking = [d for d in decisions if d.route == Route.BREAKING]
    digest_overflow = [d for d in decisions if d.route == Route.DIGEST and "overflow" in d.reason]
    assert len(breaking) == 5, [d.reason for d in decisions]
    assert len(digest_overflow) == 3


def test_always_pass_uncapped(store):
    # Fill rate-limit first with non-confirmed breaking
    fillers = []
    scores = {}
    for i in range(5):
        ev, _ = _make_event(eid=f"f{i}", topic="rate_policy", sources=[f"feed{i}", f"alt{i}"])
        fillers.append(ev)
        scores[ev.event_id] = 4.6
    # Then add an always-pass: Tier 0 official scheduled (CPI from BLS)
    ev_ap, _ = _make_event(eid="ap1", topic="inflation", sources=["bls"])
    scores[ev_ap.event_id] = 5.0
    all_events = fillers + [ev_ap]
    decisions = decide(all_events, scores, store, rate_limit_window_min=15, rate_limit_max=5)
    by_id = {d.event.event_id: d for d in decisions}
    assert by_id["ap1"].route == Route.BREAKING
    assert by_id["ap1"].always_pass is True
