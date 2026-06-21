"""Acceptance §6.2 + §6.3 + deterministic event_id across restarts."""
from __future__ import annotations

from datetime import datetime, timezone

from src.dedup import Event, cluster, cluster_key_for
from src.normalizer import Item


def _event(items, topic="macro", direction="neutral"):
    return Event(event_id="e", cluster_key="k", topic_bucket=topic,
                 entity="us", direction_label=direction, items=items)


def test_representative_summary_skips_empty_top_source():
    # Top-priority item (tier 0, e.g. an X tweet) has no summary; a lower
    # RSS source carries the real text → that text is used, not "".
    items = [
        _item("x_firstsquawk", "Breaking", summary="", tier=0),
        _item("reuters", "Same story", summary="Full RSS body with figures.", tier=1),
    ]
    assert _event(items).representative_summary == "Full RSS body with figures."


def test_representative_summary_empty_when_no_source_has_one():
    items = [_item("x_a", "t1", summary="", tier=2),
             _item("x_b", "t2", summary="", tier=2)]
    assert _event(items).representative_summary == ""


def test_classify_summary_combines_distinct_dedups_and_skips_empty():
    items = [
        _item("reuters", "t1", summary="First source body.", tier=1),
        _item("cnbc", "t2", summary="Second distinct angle.", tier=1),
        _item("x_a", "t3", summary="", tier=2),                 # empty → skipped
        _item("forexlive", "t4", summary="First source body.", tier=1),  # dup → skipped
    ]
    cs = _event(items).classify_summary
    assert "First source body." in cs and "Second distinct angle." in cs
    assert cs.count("First source body.") == 1


def test_classify_summary_caps_at_three_parts():
    items = [_item(f"s{i}", f"t{i}", summary=f"body number {i}.", tier=1) for i in range(5)]
    cs = _event(items).classify_summary
    assert "body number 0." in cs and "body number 2." in cs
    assert "body number 3." not in cs


def test_classify_summary_stops_at_char_budget():
    big = "A" * 500
    # Distinct prefixes so the dedup (first-120-char) doesn't merge them.
    items = [_item(f"s{i}", f"t{i}", summary=f"UNIQUE{i} {big}", tier=1) for i in range(5)]
    cs = _event(items).classify_summary
    assert "UNIQUE0" in cs and "UNIQUE1" in cs   # ~508 + ~1016>800 → stop after 2
    assert "UNIQUE2" not in cs


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
