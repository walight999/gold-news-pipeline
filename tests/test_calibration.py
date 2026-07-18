"""Calibration feedback loop — xau_return backfill eligibility + precision table.

Pure-logic tests (no yfinance / no Sheets). The network/store wiring in
run_backfill_xau / run_precision_report is exercised via GHA dispatch.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.main import (
    _backfill_due,
    _has_official_source,
    _precision_breakdown,
    _precision_table,
    _source_count_bucket,
)
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


def test_source_count_bucket():
    assert _source_count_bucket({"source_count": 1}) == "1"
    assert _source_count_bucket({"source_count": 2}) == "2"
    assert _source_count_bucket({"source_count": 5}) == "3+"
    assert _source_count_bucket({"source_count": 0}) is None
    assert _source_count_bucket({"source_count": ""}) is None


def test_has_official_source():
    assert _has_official_source({"source_list": "fed,forexlive"}) is True
    assert _has_official_source({"source_list": "bbc_world,cnbc"}) is False
    assert _has_official_source({"source_list": ""}) is False
    # substring must not false-match (e.g. a source literally named 'fedwatch')
    assert _has_official_source({"source_list": "fedwatch"}) is False


def test_precision_breakdown_by_direction_reveals_signed_move():
    """The measure-first view: does direction_label predict the SIGN of the
    move? dovish → gold up (+), hawkish → gold down (-)."""
    rows = [
        _row(direction_label="dovish", xau_return_15m="0.30"),
        _row(direction_label="dovish", xau_return_15m="0.20"),
        _row(direction_label="hawkish", xau_return_15m="-0.25"),
        _row(direction_label="", xau_return_15m="0.40"),   # no direction → skipped
    ]
    bd = {d["k"]: d for d in _precision_breakdown(
        rows, lambda r: r.get("direction_label") or None, key_label="k")}
    assert set(bd) == {"dovish", "hawkish"}
    assert bd["dovish"]["avg_signed"] > 0
    assert bd["hawkish"]["avg_signed"] < 0


def test_precision_breakdown_by_source_count():
    rows = [
        _row(source_count=1, xau_return_15m="0.05"),
        _row(source_count=3, xau_return_15m="0.40"),
        _row(source_count=4, xau_return_15m="0.30"),
    ]
    bd = {d["k"]: d for d in _precision_breakdown(rows, _source_count_bucket, key_label="k")}
    assert bd["1"]["n"] == 1
    assert bd["3+"]["n"] == 2
