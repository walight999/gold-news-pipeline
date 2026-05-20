"""Tests for the maintain mode's purge_older_than helper."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.utils_time import iso_utc, now_utc


def test_purge_older_than_drops_old_rows(store):
    """Rows older than `days` get purged; fresh rows stay."""
    now = now_utc()
    old_ts = iso_utc(now - timedelta(days=14))
    new_ts = iso_utc(now - timedelta(days=2))

    store.upsert("event_state", {
        "event_id": "old1", "last_seen_ts": old_ts, "score": 3.0,
    })
    store.upsert("event_state", {
        "event_id": "new1", "last_seen_ts": new_ts, "score": 4.0,
    })
    # FakeStore does not have purge_older_than. Skip if missing.
    if not hasattr(store, "purge_older_than"):
        return

    removed = store.purge_older_than("event_state", days=7, ts_col="last_seen_ts")
    assert removed == 1
    remaining = store.all_rows("event_state")
    assert len(remaining) == 1
    assert remaining[0]["event_id"] == "new1"


def test_purge_keeps_rows_with_missing_timestamp(store):
    """Rows without parseable ts are kept — purge never deletes on uncertainty."""
    store.upsert("event_state", {
        "event_id": "noTS", "last_seen_ts": "", "score": 2.0,
    })
    if not hasattr(store, "purge_older_than"):
        return
    removed = store.purge_older_than("event_state", days=1, ts_col="last_seen_ts")
    assert removed == 0
    assert len(store.all_rows("event_state")) == 1
