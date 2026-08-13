"""sent_log's 30-day retention must not take the delivery history with it.

sent_log is an idempotency ledger, so "how much did we send in June, and how
much of it landed?" expired every month. delivery_stats rolls it up into one
bounded row per ICT day before maintain purges the detail.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from src import delivery_stats

NOW = datetime(2026, 8, 13, 12, 0, tzinfo=timezone.utc)
CUTOFF = NOW - timedelta(days=30)


def _sent(ts: str, route: str = "breaking", status: str = "200"):
    return {"event_id": f"e-{ts}-{route}", "route_type": route,
            "sent_ts": ts, "line_status": status}


def _by_day(rows):
    return {r["date_ict"]: r for r in rows}


# ---- ICT day attribution ----

def test_a_push_after_17_utc_belongs_to_the_next_ict_day():
    """ICT is UTC+7, so 17:00Z is already tomorrow in Bangkok. Getting this
    wrong would smear every evening push into the previous day's totals."""
    assert delivery_stats.ict_day("2026-08-12T17:30:00Z") == "2026-08-13"
    assert delivery_stats.ict_day("2026-08-12T16:59:00Z") == "2026-08-12"


def test_unparseable_timestamp_is_dropped_not_bucketed():
    assert delivery_stats.ict_day("") is None
    assert delivery_stats.ict_day("not-a-date") is None


def test_rows_with_no_timestamp_are_skipped():
    out = delivery_stats.aggregate([_sent("")], CUTOFF)

    assert out == []


# ---- aggregation ----

def test_counts_are_split_by_route_and_by_delivery():
    rows = [
        _sent("2026-08-12T02:00:00Z", "breaking"),
        _sent("2026-08-12T03:00:00Z", "breaking"),
        _sent("2026-08-12T04:00:00Z", "digest"),
        _sent("2026-08-12T05:00:00Z", "digest", status="429"),
    ]

    day = _by_day(delivery_stats.aggregate(rows, CUTOFF))["2026-08-12"]

    assert day["n_sent"] == 3
    assert day["n_failed"] == 1
    assert json.loads(day["by_route"]) == {"breaking": 2, "digest": 2}


def test_by_route_counts_attempts_while_n_sent_counts_deliveries():
    """The 2026-07 quota exhaustion shape: volume steady, deliveries collapse.
    by_route must keep counting the attempt or the drop is invisible."""
    rows = [_sent(f"2026-08-12T0{i}:00:00Z", "alert", status="429")
            for i in range(4)]

    day = _by_day(delivery_stats.aggregate(rows, CUTOFF))["2026-08-12"]

    assert (day["n_sent"], day["n_failed"]) == (0, 4)
    assert json.loads(day["by_route"]) == {"alert": 4}


def test_a_never_attempted_push_counts_as_failed():
    """line_status 0 = the quota gate or quiet hours shed it before the API."""
    day = _by_day(delivery_stats.aggregate(
        [_sent("2026-08-12T02:00:00Z", status="0")], CUTOFF))["2026-08-12"]

    assert (day["n_sent"], day["n_failed"]) == (0, 1)


def test_missing_route_type_is_bucketed_as_unknown():
    out = delivery_stats.aggregate(
        [{"sent_ts": "2026-08-12T02:00:00Z", "line_status": "200"}], CUTOFF)

    assert json.loads(out[0]["by_route"]) == {"unknown": 1}


def test_days_are_returned_in_order():
    rows = [_sent("2026-08-12T02:00:00Z"), _sent("2026-08-10T02:00:00Z"),
            _sent("2026-08-11T02:00:00Z")]

    assert [r["date_ict"] for r in delivery_stats.aggregate(rows, CUTOFF)] == [
        "2026-08-10", "2026-08-11", "2026-08-12"]


# ---- the partial-day guard ----

def test_a_day_straddling_the_cutoff_is_skipped():
    """Its surviving rows are only a fraction of what that day really sent.
    Writing that fraction would overwrite the complete figure an earlier run
    already archived — silently rewriting history downward."""
    # Cutoff lands mid-way through ICT day 2026-07-14 (which spans
    # 07-13T17:00Z → 07-14T17:00Z), so that day is half-purged already.
    cutoff = datetime(2026, 7, 14, 6, 0, tzinfo=timezone.utc)

    out = delivery_stats.aggregate([_sent("2026-07-14T08:00:00Z")], cutoff)

    assert [r["date_ict"] for r in out] == []


def test_a_day_fully_inside_the_window_is_archived():
    cutoff = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)

    out = delivery_stats.aggregate([_sent("2026-07-20T02:00:00Z")], cutoff)

    assert [r["date_ict"] for r in out] == ["2026-07-20"]


def test_skipping_loses_nothing_because_the_day_was_archived_while_young():
    """Same day, two runs: a fresh run captures it whole; a later run whose
    cutoff has moved past it declines to touch it."""
    rows = [_sent("2026-07-20T02:00:00Z"), _sent("2026-07-20T03:00:00Z")]
    # Real cutoffs: now - 30d, for a run the next day and one a month later.
    early = datetime(2026, 7, 21, 0, 0, tzinfo=timezone.utc) - timedelta(days=30)
    late = datetime(2026, 8, 20, 0, 0, tzinfo=timezone.utc) - timedelta(days=30)

    assert delivery_stats.aggregate(rows, early)[0]["n_sent"] == 2
    assert delivery_stats.aggregate(rows, late) == []


# ---- summarize ----

def test_summarize_totals_the_recent_window():
    rows = [
        {"date_ict": "2026-08-13", "n_sent": "5", "n_failed": "1",
         "by_route": json.dumps({"breaking": 4, "digest": 2})},
        {"date_ict": "2026-08-12", "n_sent": "3", "n_failed": "0",
         "by_route": json.dumps({"digest": 3})},
    ]

    s = delivery_stats.summarize(rows, days=7, today=NOW)

    assert (s["days"], s["n_sent"], s["n_failed"]) == (2, 8, 1)
    assert s["by_route"] == {"breaking": 4, "digest": 5}


def test_summarize_excludes_days_outside_the_window():
    rows = [{"date_ict": "2026-01-01", "n_sent": "99", "n_failed": "0",
             "by_route": "{}"}]

    assert delivery_stats.summarize(rows, days=7, today=NOW)["n_sent"] == 0


def test_summarize_survives_a_hand_edited_by_route_cell():
    """The sheet is human-editable; one bad cell must not break the report."""
    rows = [{"date_ict": "2026-08-13", "n_sent": "5", "n_failed": "0",
             "by_route": "oops not json"}]

    s = delivery_stats.summarize(rows, days=7, today=NOW)

    assert s["n_sent"] == 5
    assert s["by_route"] == {}


def test_summarize_requires_an_explicit_today():
    """No hidden clock reads — the caller passes now_utc() so this is testable."""
    with pytest.raises(ValueError):
        delivery_stats.summarize([], days=7)
