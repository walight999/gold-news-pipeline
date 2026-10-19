"""Scoring v1: base_impact(topic) * freshness_factor.

No source_weight, no dynamic confirmation_factor — Phase 1 routing handles confirmation.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from .dedup import Event
from .utils_time import freshness_factor, now_utc

_GRACE_FLOOR_MIN = 4   # graced items score at most like a 4-min-old item (0.6)


def _graced_anchor(item, now: datetime) -> datetime:
    from datetime import timedelta
    pub = item.published_ts
    grace = int(getattr(item, "freshness_grace_min", 0) or 0)
    if not grace or now - pub <= timedelta(minutes=_GRACE_FLOOR_MIN):
        return pub
    return min(pub + timedelta(minutes=grace), now - timedelta(minutes=_GRACE_FLOOR_MIN))


def score_event(ev: Event, kw_config: dict[str, Any], scheduled_release_ts: datetime | None = None) -> float:
    topic_cfg = kw_config["topics"].get(ev.topic_bucket)
    base = float(topic_cfg["base_impact"]) if topic_cfg else 1.0
    # Freshness anchor (spec §4.4):
    #   scheduled -> release_time
    #   unscheduled -> earliest published_ts when present (so we don't treat
    #     hours-old feed items as fresh on first run / cold start), else first_seen_ts.
    if scheduled_release_ts is not None:
        anchor = scheduled_release_ts
    else:
        # A slow-polled feed (apify_recover, every 15-22 min) can't see an item
        # before it is 10-30 min old, which alone drops freshness to 0.3-0.1.
        # `freshness_grace_min` shifts that item's anchor forward by the feed's
        # poll latency so it scores like a 5-min-polled feed would have.
        # Capped at the 3-10 min band (0.6): grace may lift a late item to a
        # digest-grade score but never to a breaking one — an item we only saw
        # 15+ min after publication is not breaking news. A genuinely fresh
        # item (≤ _GRACE_FLOOR_MIN old) keeps its real age.
        now = now_utc()
        pubs = [_graced_anchor(i, now) for i in ev.items if i.published_ts is not None]
        anchor = min(pubs) if pubs else ev.first_seen_ts
    ff = freshness_factor(anchor)
    return base * ff
