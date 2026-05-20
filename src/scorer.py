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
    # Freshness anchor: scheduled release time if present, else first_seen_ts.
    anchor = scheduled_release_ts or ev.first_seen_ts
    ff = freshness_factor(anchor)
    return base * ff
