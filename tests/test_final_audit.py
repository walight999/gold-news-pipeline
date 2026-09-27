"""Regression tests for the 2026-09-28 pre-live audit findings."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from src import main as m
from src import price_feed, speech_watch as sw
from src import store as store_mod
from src.line_flex import _gold_call_row, _price_cell
from src.news_alert import MarketAlert

T = datetime(2026, 9, 25, 12, 30, tzinfo=timezone.utc)


def _store():
    s = store_mod.Store(sheet_id="x", creds_json="{}")
    for tab in store_mod.SCHEMAS:
        s.data[tab] = {}
        s.dirty[tab] = set()
    return s


# 1. spot series: a bar far older than its target is not forward-filled
def test_spot_returns_leave_unpublished_offsets_empty():
    # bars only up to 12:59 (the 13:00 hour isn't published yet)
    series = [(T - timedelta(minutes=10) + timedelta(minutes=i), 4000.0 + i) for i in range(40)]
    base, rets = price_feed.base_and_returns_from_series(
        series, T, (5, 15, 60), now=T + timedelta(minutes=75), bar_min=1, max_gap_min=3)
    assert base is not None and rets[5] is not None and rets[15] is not None
    assert rets[60] is None                     # 13:30 target, last bar 12:59 → retry later


def test_spot_base_missing_when_release_hour_unpublished():
    series = [(T - timedelta(hours=2) + timedelta(minutes=i), 4000.0) for i in range(60)]
    base, _ = price_feed.base_and_returns_from_series(series, T, (5,), now=T + timedelta(hours=1),
                                                      bar_min=1, max_gap_min=3)
    assert base is None


# 2. carousel chunks respect a byte budget
def test_carousel_chunks_by_size():
    big = {"type": "bubble", "pad": "x" * 9000}
    items = [{"bubble": big} for _ in range(10)]
    chunks = m._carousel_chunks(items)
    assert all(sum(len(json.dumps(i["bubble"])) for i in c) <= m._CAROUSEL_BYTES for c in chunks)
    assert sum(len(c) for c in chunks) == 10
    small = [{"bubble": {"type": "bubble"}} for _ in range(14)]
    assert [len(c) for c in m._carousel_chunks(small)] == [12, 2]


# 3. unknown day change renders no percentage (not a fake 0.00%)
def test_unknown_day_change_hidden():
    cell = _price_cell("XAU", (4285.2, None), lambda v: f"{v:,.2f}")
    assert [c["text"] for c in cell["contents"]] == ["XAU", "4,285.20"]
    row = _gold_call_row(MarketAlert(action="keep", headline_th="h"), (4285.2, None))
    assert row["contents"][0]["text"] == "XAU 4,285.2"


def test_snapshot_pct_none_when_futures_down(monkeypatch):
    from src import spot_feed
    monkeypatch.setattr(price_feed, "get_snapshot", lambda t: None)
    monkeypatch.setattr(spot_feed, "current_spot", lambda: 4285.2)
    s = price_feed.get_xau_snapshot()
    assert s.last == 4285.2 and s.pct_change_day is None


# 4. expired speech buffers are deleted
def test_store_delete_and_drop_buffer():
    s = _store()
    s.upsert("source_state", {"source_id": "_speech:abc", "items_last_hour": "[]"})
    s.dirty["source_state"] = set()
    sw.drop_buffer(s, "abc")
    assert s.get("source_state", ("_speech:abc",)) is None
    assert "__purge__" in s.dirty["source_state"]
    assert s.delete("source_state", ("nope",)) is False


def test_expired_window_buffer_dropped_on_plan(monkeypatch):
    s = _store()
    old = {"id": "old", "title": "President Trump Speaks", "country": "USD",
           "start": (T - timedelta(hours=10)).isoformat(), "end": (T - timedelta(hours=9)).isoformat(),
           "keywords": ["TRUMP"]}
    sw.publish_windows(s, [old])
    s.upsert("source_state", {"source_id": "_speech:old", "items_last_hour": "[]"})
    monkeypatch.setattr(m, "now_utc", lambda: T)
    m._speech_plan(s, [], {})
    assert sw.stored_windows(s) == []
    assert s.get("source_state", ("_speech:old",)) is None


# 5. Friday-evening UTC (Saturday ICT) still runs the speech step
def test_friday_utc_runs_speech_only(monkeypatch):
    called = []
    monkeypatch.setattr(m, "is_weekend_ict", lambda: True)
    monkeypatch.setattr(m, "now_utc", lambda: datetime(2026, 10, 2, 18, 0, tzinfo=timezone.utc))

    async def fake():
        called.append(1)
        return 0
    monkeypatch.setattr(m, "_speech_only_run", fake)
    assert asyncio.run(m.run_calendar_check()) == 0 and called == [1]


def test_saturday_utc_skips(monkeypatch):
    monkeypatch.setattr(m, "is_weekend_ict", lambda: True)
    monkeypatch.setattr(m, "now_utc", lambda: datetime(2026, 10, 3, 10, 0, tzinfo=timezone.utc))
    monkeypatch.setattr(m, "_speech_only_run", lambda: pytest.fail("must not run on Saturday UTC"))
    assert asyncio.run(m.run_calendar_check()) == 0


# 6/7. skip marker is not a failed delivery; tone not stored as a surprise
def test_skip_marker_not_counted_as_failure():
    from src import delivery_stats
    s = _store()
    s.upsert("sent_log", {"event_id": "speech_end:w", "route_type": sw.SKIP_ROUTE,
                          "sent_ts": "2026-09-25T13:00:00Z", "line_status": "skipped"})
    s.upsert("sent_log", {"event_id": "e1", "route_type": "breaking",
                          "sent_ts": "2026-09-25T13:00:00Z", "line_status": "200"})
    rows = delivery_stats.aggregate(s.all_rows("sent_log"), cutoff=datetime(2026, 9, 1, tzinfo=timezone.utc))
    assert rows, "expected the 2026-09-25 day to be aggregated"
    assert [(r["n_sent"], r["n_failed"]) for r in rows] == [(1, 0)]


# 9. whole-word keywords
def test_boe_does_not_match_boeing():
    k = ["BOE", "BANK OF ENGLAND"]
    assert not sw.is_quote("BOEING SHARES JUMP ON ORDER", "x_firstsquawk", k)
    assert sw.is_quote("BOE'S BAILEY: RATES TO STAY RESTRICTIVE", "x_firstsquawk", k)
    assert sw.is_quote("FED'S WALLER: MORE HIKES POSSIBLE", "x_firstsquawk", ["FED"])
    assert not sw.is_quote("FEDEX RAISES GUIDANCE", "x_firstsquawk", ["FED"])


# 10. a failed analysis is cached for the same quotes
def test_failed_analysis_not_retried_on_same_quotes(monkeypatch):
    s = _store()
    calls = []
    monkeypatch.setattr(sw, "_llm", lambda p: calls.append(1) or "not json")
    w = {"id": "w", "title": "FOMC Press Conference", "country": "USD"}
    q = [{"t": "WARSH: X", "ts": T.isoformat()}]
    assert sw.analyze(w, q, "end", None, None, s) is None
    assert sw.analyze(w, q, "end", None, None, s) is None
    assert calls == [1]
    q2 = q + [{"t": "WARSH: Y", "ts": T.isoformat()}]
    sw.analyze(w, q2, "end", None, None, s)
    assert calls == [1, 1]                   # new quote → fresh attempt


# audit: speech-type events without a forecast get no 📊 / ⏰ calendar card
def test_speech_events_filtered_from_calendar_cards():
    from src.calendar import CalEvent
    cfg = sw.cfg_from({})
    lag = CalEvent("x", "ECB President Lagarde Speaks", "EUR", "Medium", "", "", T)
    ffr = CalEvent("y", "Federal Funds Rate", "USD", "High", "4.00%", "3.75%", T)
    cpi = CalEvent("z", "Core CPI m/m", "USD", "High", "0.3%", "0.2%", T)
    kept = [e for e in (lag, ffr, cpi)
            if not (sw.is_speech_event(e, cfg) and not (e.forecast or "").strip())]
    assert [e.title for e in kept] == ["Federal Funds Rate", "Core CPI m/m"]
