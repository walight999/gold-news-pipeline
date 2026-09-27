"""Spot prices: Dukascopy parsing, 1-minute reaction maths, card snapshot,
and graded calibration rows measured on spot (futures only as fallback)."""
from __future__ import annotations

import lzma
import struct
from datetime import date, datetime, timedelta, timezone

import pytest

from src import main as main_mod
from src import price_feed, spot_feed
from src import store as store_mod
from src.utils_time import iso_utc

T = datetime(2026, 9, 25, 12, 30, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _reset_spot_cache():
    spot_feed._cache.clear()
    spot_feed._files_used[0] = 0
    yield
    spot_feed._cache.clear()


# ---- Dukascopy decoding ----

def _candles(rows):   # rows: (sec_of_day, open, close, vol)
    raw = b"".join(struct.pack(">5if", s, int(o * 1000), int(c * 1000), 0, 0, v) for s, o, c, v in rows)
    return lzma.compress(raw, format=lzma.FORMAT_ALONE)


def _ticks(rows):     # rows: (ms_of_hour, bid)
    raw = b"".join(struct.pack(">3i2f", ms, int(b * 1000) + 300, int(b * 1000), 1.0, 1.0) for ms, b in rows)
    return lzma.compress(raw, format=lzma.FORMAT_ALONE)


def test_day_file_decodes_and_drops_filler_bars(monkeypatch):
    monkeypatch.setattr(spot_feed, "_curl", lambda url: _candles(
        [(12 * 3600 + 29 * 60, 4000.0, 4001.5, 3.0), (12 * 3600 + 30 * 60, 4001.0, 4001.0, 0.0)]))
    m = spot_feed._day_minutes(date(2026, 9, 25))
    k = int(datetime(2026, 9, 25, 12, 29, tzinfo=timezone.utc).timestamp()) // 60
    assert m == {k: 4001.5}


def test_hour_ticks_keep_last_bid_per_minute(monkeypatch):
    monkeypatch.setattr(spot_feed, "_curl", lambda url: _ticks([(1000, 4000.0), (50000, 4000.5), (61000, 4002.0)]))
    m = spot_feed._hour_minutes(date(2026, 9, 25), 12)
    k0 = int(datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc).timestamp()) // 60
    assert m == {k0: 4000.5, k0 + 1: 4002.0}


def test_minute_series_uses_day_file_for_past_days_and_skips_open_hour(monkeypatch):
    urls = []

    def fake(url):
        urls.append(url)
        return _candles([]) if "candles" in url else _ticks([(0, 4000.0)])
    monkeypatch.setattr(spot_feed, "_curl", fake)
    # past day → one day file
    spot_feed.minute_series(T, T + timedelta(minutes=70), now=T + timedelta(days=1))
    assert len(urls) == 1 and urls[0].endswith("/2026/08/25/BID_candles_min_1.bi5")
    # same day at 13:20 → hour 12 tick file only; the open 13h is not requested
    urls.clear()
    spot_feed._cache.clear()
    s = spot_feed.minute_series(T - timedelta(minutes=10), T + timedelta(minutes=62),
                                now=datetime(2026, 9, 25, 13, 20, tzinfo=timezone.utc))
    assert urls == ["https://datafeed.dukascopy.com/datafeed/XAUUSD/2026/08/25/12h_ticks.bi5"]
    assert s


def test_minute_series_none_when_fetch_fails(monkeypatch):
    monkeypatch.setattr(spot_feed, "_curl", lambda url: None)
    assert spot_feed.minute_series(T, T + timedelta(minutes=30), now=T + timedelta(days=1)) is None


# ---- 1-minute reaction maths ----

def test_base_and_returns_one_minute_bars():
    series = [(T + timedelta(minutes=i), 4000.0 + i) for i in range(-10, 70)]
    base, rets = price_feed.base_and_returns_from_series(
        series, T, (5, 15), now=T + timedelta(hours=2), bar_min=1)
    # base = close of the bar ending at the print (opened 12:29) = 3999
    assert base == 3999.0
    # T+5 = close of the bar ending at 12:35 (opened 12:34) = 4004
    assert rets[5] == pytest.approx((4004 - 3999) / 3999 * 100)


# ---- card level: spot, day % from futures ----

def test_xau_snapshot_is_spot_with_futures_day_change(monkeypatch):
    fut = price_feed.PriceSnapshot(ticker="GC=F", last=4321.2, prev_close=4300.0,
                                   pct_change_day=0.49, bar_time_utc=T)
    monkeypatch.setattr(price_feed, "get_snapshot", lambda t: fut)
    monkeypatch.setattr(spot_feed, "current_spot", lambda: 4285.2)
    s = price_feed.get_xau_snapshot()
    assert s.ticker == "XAUUSD" and s.last == 4285.2 and s.pct_change_day == 0.49


def test_xau_snapshot_falls_back_to_futures(monkeypatch):
    fut = price_feed.PriceSnapshot(ticker="GC=F", last=4321.2, prev_close=4300.0,
                                   pct_change_day=0.49, bar_time_utc=T)
    monkeypatch.setattr(price_feed, "get_snapshot", lambda t: fut)
    assert price_feed.get_xau_snapshot() is fut          # conftest: spot unavailable


# ---- graded calibration rows on spot ----

NOW = T + timedelta(minutes=70)


def _store(rows):
    s = store_mod.Store(sheet_id="x", creds_json="{}")
    s.data = {t: {} for t in store_mod.SCHEMAS}
    s.dirty = {t: set() for t in store_mod.SCHEMAS}
    for r in rows:
        full = {c: r.get(c, "") for c in store_mod.SCHEMAS["calibration_log"]}
        s.data["calibration_log"][full["event_id"]] = full
    return s


def _row(eid, graded=True, minutes_ago=70):
    return {"event_id": eid, "first_seen_ts": iso_utc(NOW - timedelta(minutes=minutes_ago)),
            "predicted_dir": "bear" if graded else ""}


def _futures(monkeypatch, calls):
    def fetch(*a, **k):
        calls.append(1)
        return [(NOW, 1.0)]
    monkeypatch.setattr(price_feed, "fetch_intraday_series", fetch)
    monkeypatch.setattr(price_feed, "base_and_returns_from_series",
                        lambda *a, **k: (4321.0, {5: 9.0, 15: 9.0, 30: 9.0, 60: 9.0}))


def test_graded_row_priced_on_spot(monkeypatch):
    calls = []
    _futures(monkeypatch, calls)
    monkeypatch.setattr(price_feed, "xau_spot_base_and_returns",
                        lambda ts, offs, now=None: (4285.0, {5: -0.1, 15: -0.2, 30: -0.3, 60: -0.4}))
    s = _store([_row("cal:a")])
    main_mod._backfill_xau_on_store(s, NOW)
    r = s.get("calibration_log", ("cal:a",))
    assert (r["xau_base_price"], r["xau_return_15m"]) == (4285.0, -0.2)
    assert calls == []                     # futures never fetched


def test_young_graded_row_waits_for_spot(monkeypatch):
    calls = []
    _futures(monkeypatch, calls)
    s = _store([_row("cal:a", minutes_ago=70)])
    main_mod._backfill_xau_on_store(s, NOW)          # spot unavailable (conftest)
    assert s.get("calibration_log", ("cal:a",))["xau_return_15m"] == ""
    assert calls == []


def test_old_graded_row_falls_back_to_futures(monkeypatch):
    calls = []
    _futures(monkeypatch, calls)
    s = _store([_row("cal:a", minutes_ago=4 * 60)])
    main_mod._backfill_xau_on_store(s, NOW)
    assert s.get("calibration_log", ("cal:a",))["xau_return_15m"] == 9.0


def test_ungraded_rows_stay_on_futures(monkeypatch):
    calls = []
    _futures(monkeypatch, calls)
    monkeypatch.setattr(price_feed, "xau_spot_base_and_returns",
                        lambda *a, **k: pytest.fail("spot must not be fetched for ungraded rows"))
    s = _store([_row("n1", graded=False), _row("n2", graded=False)])
    main_mod._backfill_xau_on_store(s, NOW)
    assert calls == [1]                    # one batch fetch for all of them
    assert s.get("calibration_log", ("n1",))["xau_return_15m"] == 9.0
