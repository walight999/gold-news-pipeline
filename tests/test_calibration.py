"""Calibration feedback loop — xau_return backfill eligibility + precision table.

Pure-logic tests (no yfinance / no Sheets). The network/store wiring in
run_backfill_xau / run_precision_report is exercised via GHA dispatch.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.main import _backfill_due, _precision_table
from src.utils_time import iso_utc

NOW = datetime(2026, 6, 21, 12, 0, 0, tzinfo=timezone.utc)


def _row(**kw):
    base = {
        "event_id": "e", "first_seen_ts": "", "topic_bucket": "inflation",
        "routed_as": "breaking", "xau_return_5m": "", "xau_return_15m": "",
        "xau_return_30m": "",
    }
    base.update(kw)
    return base


def _aged(minutes=0, days=0):
    return iso_utc(NOW - timedelta(minutes=minutes, days=days))


def test_backfill_due_window():
    # 40 min old, empty → due (30-min window closed, within 5-day intraday)
    assert _backfill_due(_row(first_seen_ts=_aged(minutes=40)), NOW) is True
    # too fresh — 30-min reaction window not closed
    assert _backfill_due(_row(first_seen_ts=_aged(minutes=10)), NOW) is False
    # boundary: exactly 35 min → due
    assert _backfill_due(_row(first_seen_ts=_aged(minutes=35)), NOW) is True
    # 6 days old → beyond intraday window, unfillable
    assert _backfill_due(_row(first_seen_ts=_aged(days=6)), NOW) is False


def test_backfill_due_skips_filled_and_bad_ts():
    # already has a 30m return → skip
    assert _backfill_due(_row(first_seen_ts=_aged(minutes=40), xau_return_30m="0.12"), NOW) is False
    # unparseable timestamp → skip
    assert _backfill_due(_row(first_seen_ts="garbage"), NOW) is False
    assert _backfill_due(_row(first_seen_ts=""), NOW) is False


def test_precision_table_groups_and_stats():
    rows = [
        _row(topic_bucket="inflation", routed_as="breaking", xau_return_15m="0.30"),   # hit
        _row(topic_bucket="inflation", routed_as="breaking", xau_return_15m="-0.20"),  # hit (abs)
        _row(topic_bucket="inflation", routed_as="breaking", xau_return_15m="0.05"),   # not a hit
        _row(topic_bucket="geopolitics", routed_as="alert", xau_return_15m=""),         # no return → skipped
        _row(topic_bucket="geopolitics", routed_as="alert", xau_return_15m="bad"),      # unparseable → skipped
    ]
    table = _precision_table(rows, move_threshold_pct=0.15)
    assert len(table) == 1                      # only the inflation/breaking group qualifies
    g = table[0]
    assert (g["topic"], g["route"], g["n"]) == ("inflation", "breaking", 3)
    assert round(g["hit_pct"]) == 67            # 2 of 3 moved >= 0.15%
    assert abs(g["avg_signed"] - (0.30 - 0.20 + 0.05) / 3) < 1e-9
    assert abs(g["avg_abs"] - (0.30 + 0.20 + 0.05) / 3) < 1e-9


def test_precision_table_empty_when_no_returns():
    assert _precision_table([_row(xau_return_15m="")], 0.15) == []
