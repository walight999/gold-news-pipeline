"""Acceptance §6.2 + §6.3 + deterministic event_id across restarts."""
from __future__ import annotations

from datetime import datetime, timezone

from src.dedup import cluster, cluster_key_for
from src.normalizer import Item


def _item(source_id, title, summary="", tier=1, role="macro", url=None):
    ts = datetime(2026, 5, 20, 12, 0, 0, tzinfo=timezone.utc)
    return Item(
        source_id=source_id, tier=tier, role=role,
        title=title, summary=summary,
        url=url or f"https://x/{source_id}/{abs(hash(title)) % 99999}",
        published_ts=ts, first_seen_ts=ts,
    )


def test_cross_source_same_story_one_event(kw_config):
    # 3 different feeds, clearly the same inflation story, different wording, 15m window.
    # Phase-1 keyword clustering: topic, entity, direction must agree.
    items = [
        _item("cnbc",       "US CPI hotter than expected; inflation remains elevated"),
        _item("forexlive",  "CPI prints above forecast on sticky inflation"),
        _item("marketwatch","Hot CPI report shows inflation upside risks"),
    ]
    events = cluster(items, kw_config)
    assert len(events) == 1, events
    ev = events[0]
    assert ev.source_count == 3
    assert ev.topic_bucket == "inflation"
    assert ev.direction_label == "hawkish"


def test_powell_three_directions_in_same_window_split(kw_config):
    # Powell speaks 3 things in 15m, different direction labels → 3 events.
    items = [
        _item("cnbc",      "Powell: higher for longer needed"),                 # hawkish
        _item("cnbc",      "Powell flags soft landing increasingly likely"),    # dovish
        _item("cnbc",      "Powell: ceasefire de-escalation lowering risks"),   # risk_on
    ]
    events = cluster(items, kw_config)
    directions = sorted(e.direction_label for e in events)
    assert directions == sorted({"hawkish", "dovish", "risk_on"}), directions


def test_event_id_deterministic_across_runs(kw_config):
    a = _item("cnbc", "Fed hikes 25bp on sticky inflation")
    b = _item("cnbc", "Fed hikes 25bp on sticky inflation")
    ck_a, *_ = cluster_key_for(a, kw_config)
    ck_b, *_ = cluster_key_for(b, kw_config)
    assert ck_a == ck_b
    ev_a = cluster([a], kw_config)[0]
    ev_b = cluster([b], kw_config)[0]
    assert ev_a.event_id == ev_b.event_id
