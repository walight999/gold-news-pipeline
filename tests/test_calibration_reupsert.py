"""A live cluster's re-upsert must not erase an already-measured XAU return.

`dedup.cluster_key_for` buckets on a 60-min window, so a story that keeps
attracting items holds the SAME event_id across many 5-min cron runs. Because
`Store.upsert` REPLACES the row (it rebuilds from SCHEMAS, absent keys become
""), the persist step used to write "" into xau_return_5m/15m/30m on every one
of those runs. Any backfill that had already priced the row was silently
undone — and past the 5-day yfinance intraday window that loss is permanent.
"""
from __future__ import annotations

from src.store import SCHEMAS, Store


def _row(event_id="ev1", **over):
    r = {c: "" for c in SCHEMAS["calibration_log"]}
    r.update({"event_id": event_id, "first_seen_ts": "2026-08-13T10:00:00Z",
              "topic_bucket": "inflation", "entity": "us", "score": "4.2",
              "routed_as": "alert", "source_count": "2"})
    r.update(over)
    return r


def _store_with(row):
    """A real Store (never connected) preloaded with one calibration row — the
    no-op guard in upsert is part of what's under test, so a fake won't do."""
    s = Store(sheet_id="x", creds_json="{}")
    s.data["calibration_log"] = {row["event_id"]: dict(row)}
    return s


def _persist(store, row):
    """The persist step from main.run_once, minus the pipeline around it."""
    prev = store.get("calibration_log", (row["event_id"],)) or {}
    return store.upsert("calibration_log", {
        **row,
        **{c: prev.get(c, "") for c in
           ("xau_return_5m", "xau_return_15m", "xau_return_30m",
            "xau_base_price")},
    })


def test_reupsert_preserves_a_backfilled_return():
    store = _store_with(_row(xau_return_5m="0.11", xau_return_15m="0.22",
                             xau_return_30m="0.33", xau_base_price="3400.5"))

    # Same event, one more source has since confirmed it.
    _persist(store, _row(source_count="3", score="4.8"))

    saved = store.get("calibration_log", ("ev1",))
    assert saved["xau_return_5m"] == "0.11"
    assert saved["xau_return_15m"] == "0.22"
    assert saved["xau_return_30m"] == "0.33"
    assert saved["xau_base_price"] == "3400.5"
    # ...while the fields that legitimately move DID update.
    assert saved["source_count"] == "3"
    assert saved["score"] == "4.8"


def test_first_write_still_leaves_the_returns_empty():
    """A brand-new event has nothing to preserve — the backfill must see it as
    due (`_backfill_due` keys off an empty xau_return_30m)."""
    store = Store(sheet_id="x", creds_json="{}")

    _persist(store, _row())

    saved = store.get("calibration_log", ("ev1",))
    assert saved["xau_return_30m"] == ""
    assert saved["xau_base_price"] == ""


def test_partial_backfill_is_preserved_field_by_field():
    """Off-hours releases get a 5m bar but no 30m bar. The 5m value must
    survive even though the row is still backfill-due."""
    store = _store_with(_row(xau_return_5m="0.07"))

    _persist(store, _row(source_count="4"))

    saved = store.get("calibration_log", ("ev1",))
    assert saved["xau_return_5m"] == "0.07"
    assert saved["xau_return_30m"] == ""


def test_unchanged_reupsert_is_still_a_noop():
    """The preserve must not defeat the no-op guard — an identical re-upsert
    still has to leave the tab clean, or every cron run rewrites the whole
    ~8000-row tab again."""
    store = _store_with(_row(xau_return_30m="0.33"))
    store.dirty.clear()

    _persist(store, _row())

    assert store.dirty.get("calibration_log") in (None, set())


def test_a_changed_row_is_still_marked_dirty():
    store = _store_with(_row(xau_return_30m="0.33"))
    store.dirty.clear()

    _persist(store, _row(score="9.9"))

    assert "ev1" in store.dirty["calibration_log"]


def test_a_different_event_does_not_inherit_returns():
    """Preservation is keyed on event_id — a new story must not pick up the
    neighbouring row's measured move."""
    store = _store_with(_row("ev1", xau_return_30m="0.33"))

    _persist(store, _row("ev2"))

    assert store.get("calibration_log", ("ev2",))["xau_return_30m"] == ""
    assert store.get("calibration_log", ("ev1",))["xau_return_30m"] == "0.33"
