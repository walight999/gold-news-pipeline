"""Scoring v1: base_impact(topic) * freshness_factor.

No source_weight, no dynamic confirmation_factor — Phase 1 routing handles confirmation.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from .dedup import Event
from .utils_time import freshness_factor


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
        pubs = [i.published_ts for i in ev.items if i.published_ts is not None]
        anchor = min(pubs) if pubs else ev.first_seen_ts
    ff = freshness_factor(anchor)
    return base * ff
