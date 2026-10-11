"""2026-10-11: print → Released-card latency.

The 🎯 call on a Released card is a 5-minute call; RELEASE-LEARNING.md warns
the card often lands after that move. sent_log now carries release_ts on
calendar_post rows so the lag is measured, rolled into delivery_daily, and
shown on the weekly 1:1 card."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from src import delivery_stats as ds
from src import main as m
from src import store as store_mod
from src import weekly_report
from src.line_flex import weekly_report_bubble

T0 = datetime(2026, 10, 9, 12, 30, tzinfo=timezone.utc)


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _sent(key, sent, rel=None, route="calendar_post", status=200):
    r = {"event_id": key, "route_type": route, "sent_ts": _iso(sent), "line_status": status}
    if rel is not None:
        r["release_ts"] = _iso(rel)
    return r


def test_latency_summary_nearest_rank():
    assert ds.latency_summary([30.0]) == {"n": 1, "p50_s": 30, "p90_s": 30, "max_s": 30}
    # n=2: p50 is the LOWER value under nearest-rank (ceil(0.5*2)=1st)
    assert ds.latency_summary([200.0, 100.0])["p50_s"] == 100
    v = ds.latency_summary([float(s) for s in range(1, 11)])
    assert (v["p50_s"], v["p90_s"], v["max_s"]) == (5, 9, 10)


def test_aggregate_adds_latency_only_for_delivered_released_cards():
    rows = [
        _sent("postcal:a", T0 + timedelta(seconds=90), T0),
        _sent("postcal:b", T0 + timedelta(seconds=300), T0),
        _sent("postcal:c", T0 + timedelta(seconds=50), T0, status=429),   # not delivered
        _sent("postcal:old", T0 + timedelta(seconds=60)),                 # pre-column row
        _sent("precal:x", T0 - timedelta(minutes=15), T0, route="calendar_pre"),
        _sent("postcal:bad", T0 - timedelta(seconds=5), T0),              # sent before print
    ]
    out = ds.aggregate(rows, cutoff=T0 - timedelta(days=2))
    assert len(out) == 1
    assert json.loads(out[0]["release_latency"]) == {"n": 2, "p50_s": 90, "p90_s": 300, "max_s": 300}


def test_aggregate_leaves_latency_blank_without_data():
    out = ds.aggregate([_sent("digest:x", T0, route="digest")], cutoff=T0 - timedelta(days=2))
    assert out[0]["release_latency"] == ""


def test_release_latency_rows_respects_window():
    rows = [_sent("postcal:a", T0 + timedelta(seconds=120), T0),
            _sent("postcal:old", T0 - timedelta(days=9), T0 - timedelta(days=9, seconds=600))]
    assert ds.release_latency_rows(rows, T0 - timedelta(days=7)) == {
        "n": 1, "p50_s": 120, "p90_s": 120, "max_s": 120}
    assert ds.release_latency_rows([], T0) is None


class _Line:
    def push_flex(self, target, alt, contents):
        return {"status": 200, "results": [{"status": 200}]}


def test_push_writes_release_ts_on_released_cards(monkeypatch):
    s = store_mod.Store(sheet_id="x", creds_json="{}")
    for tab in store_mod.SCHEMAS:
        s.data[tab] = {}
    monkeypatch.setattr(m, "is_quiet_hours_ict", lambda cfg: False)
    monkeypatch.setattr(m, "record_line_outcome", lambda *a, **k: None)
    item = {"ev": SimpleNamespace(dt_utc=T0, title="CPI"), "sent_key": "postcal:a",
            "bubble": {"type": "bubble"}, "alt": "a", "on_delivered": None}
    m._push_calendar_groups(_Line(), "C1", [item], {}, s, route_type="calendar_post", priority=0)
    assert s.get("sent_log", ("postcal:a", "calendar_post"))["release_ts"] == _iso(T0)
    # pre-release cards don't get one
    item2 = dict(item, sent_key="precal:a")
    m._push_calendar_groups(_Line(), "C1", [item2], {}, s, route_type="calendar_pre", priority=0)
    assert not s.get("sent_log", ("precal:a", "calendar_pre")).get("release_ts")


def test_weekly_report_and_card_show_latency():
    now = T0 + timedelta(days=1)
    rep = weekly_report.build_weekly_report(
        now, delivery_rows=[], scorecard_rows=[], calibration_rows=[], source_rows=[],
        classifier_health_row=None,
        sent_rows=[_sent("postcal:a", T0 + timedelta(seconds=75), T0)])
    assert rep["release_latency"]["p50_s"] == 75
    texts = json.dumps(weekly_report_bubble(rep, "2026-W41"), ensure_ascii=False)
    assert "1m15s" in texts and "n=1" in texts
    # no sent rows → no line, no crash
    rep2 = weekly_report.build_weekly_report(
        now, delivery_rows=[], scorecard_rows=[], calibration_rows=[], source_rows=[],
        classifier_health_row=None)
    assert rep2["release_latency"] is None
    assert "n=" not in json.dumps(weekly_report_bubble(rep2, "W"), ensure_ascii=False)
