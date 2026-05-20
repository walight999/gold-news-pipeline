"""§4.4 scoring + freshness gates §6.7 (no false breaking for >10m old)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.dedup import Event
from src.normalizer import Item
from src.scorer import score_event


def _ev(topic_bucket: str, age_min: float, base_items=1) -> Event:
    anchor = datetime.now(timezone.utc) - timedelta(minutes=age_min)
    items = []
    for i in range(base_items):
        items.append(Item(
            source_id=f"s{i}", tier=1, role="macro",
            title="x", summary="", url=f"https://x/{i}",
            published_ts=anchor, first_seen_ts=anchor,
        ))
    return Event(
        event_id="e", cluster_key="k",
        topic_bucket=topic_bucket, entity="us",
        direction_label="neutral", items=items,
    )


def test_inflation_fresh_breaking(kw_config):
    ev = _ev("inflation", age_min=1)
    s = score_event(ev, kw_config)
    assert s >= 4.5, s


def test_inflation_stale_not_breaking(kw_config):
    ev = _ev("inflation", age_min=15)   # 10-30m bucket → 0.3
    s = score_event(ev, kw_config)
    assert s < 2.5, s


def test_jobs_fresh_breaking(kw_config):
    ev = _ev("jobs", age_min=2)
    assert score_event(ev, kw_config) >= 4.5


def test_geopol_old_archive(kw_config):
    # >30m → 0.1 freshness; geopolitics base 4 → 0.4
    ev = _ev("geopolitics", age_min=45)
    s = score_event(ev, kw_config)
    assert s < 1.5, s
