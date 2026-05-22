"""Stale filter at normalize stage — drops items >48h old before they
hit the Claude classifier. This was added after observing 744d-old
articles leaking into News Update bubbles."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.normalizer import STALE_DROP_HOURS, normalize


def _entry(url: str, hours_old: float) -> dict:
    """Helper — minimal RSS entry with a published_ts N hours in the past."""
    return {
        "source_id": "test",
        "tier": 1,
        "role": "macro",
        "title": f"item @ {hours_old}h",
        "summary": "",
        "url": url,
        "published_ts": datetime.now(timezone.utc) - timedelta(hours=hours_old),
        "source_class": "aggregator",
    }


def test_keeps_fresh_items():
    """Items <= cutoff pass through untouched."""
    items = normalize([_entry("u1", 1), _entry("u2", 24)])
    assert len(items) == 2


def test_drops_items_older_than_default_cutoff():
    """Default cutoff is 48h — 49h-old item gets dropped."""
    items = normalize([_entry("u1", 1), _entry("u2", 49)])
    assert len(items) == 1
    assert items[0].url == "u1"


def test_drops_744d_evergreen_article():
    """The original bug — a 744-day-old 'how to protect savings' article
    was leaking into the digest. Stale filter must catch this."""
    items = normalize([_entry("u1", 1), _entry("u2", 24 * 744)])
    assert len(items) == 1


def test_passes_through_items_with_no_published_ts():
    """RSS feeds that don't publish a timestamp are treated as fresh —
    safer than dropping them on uncertainty."""
    entry = {
        "source_id": "test", "tier": 1, "role": "macro",
        "title": "no date", "summary": "", "url": "u1",
        "published_ts": None,
    }
    items = normalize([entry])
    assert len(items) == 1


def test_custom_cutoff():
    """Configurable cutoff for testing / future tightening."""
    items = normalize([_entry("u1", 12), _entry("u2", 30)], stale_drop_hours=24)
    assert len(items) == 1
    assert items[0].url == "u1"


def test_skips_entries_with_no_url():
    """Existing behavior — items without a URL are skipped."""
    entries = [
        {"source_id": "t", "tier": 1, "role": "macro",
         "title": "no url", "summary": "", "url": "",
         "published_ts": None},
    ]
    assert normalize(entries) == []
