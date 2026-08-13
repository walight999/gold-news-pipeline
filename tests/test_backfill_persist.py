"""Backfill must actually PERSIST — regression for the silent 2026-07-15→08-13 outage.

`_backfill_xau_on_store` fills xau_return_* on calibration_log rows. Those rows
come from `store.all_rows()`, which returns the LIVE dicts inside `store.data`.
Mutating them in place and then calling `upsert()` made the no-op guard (PR #65)
compare a row against itself — always equal, never dirtied, never flushed. The
run logged `xau backfilled=80/80` while the sheet stayed empty for a month.

These tests use the REAL Store (not conftest.FakeStore, which has no no-op
guard and therefore cannot catch this class of bug).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src import main as main_mod
from src import price_feed
from src import store as store_mod
from src.utils_time import iso_utc

NOW = datetime(2026, 8, 13, 12, 0, 0, tzinfo=timezone.utc)


def _store_with_rows(rows: list[dict]) -> store_mod.Store:
    s = store_mod.Store(sheet_id="x", creds_json="{}")
    s._sh = object()  # truthy; flush is patched away in these tests
    s.data = {t: {} for t in store_mod.SCHEMAS}
    s.dirty = {t: set() for t in store_mod.SCHEMAS}
    for r in rows:
        full = {c: r.get(c, "") for c in store_mod.SCHEMAS["calibration_log"]}
        s.data["calibration_log"][full["event_id"]] = full
    return s


def _cal_row(event_id: str, minutes_ago: int = 60) -> dict:
    return {
        "event_id": event_id,
        "first_seen_ts": iso_utc(NOW - timedelta(minutes=minutes_ago)),
        "topic_bucket": "inflation",
        "routed_as": "calendar_post",
        "predicted_dir": "up",
        "xau_return_5m": "", "xau_return_15m": "", "xau_return_30m": "",
        "xau_base_price": "",
    }


def _patch_prices(monkeypatch, base=3400.0, rets=(0.11, 0.22, 0.33)) -> list[int]:
    """Patch the batch price path. Returns a 1-element list counting how many
    times the network fetch fired, so tests can assert it stays at 1 per run."""
    calls = [0]

    def fake_fetch(ticker="GC=F", period="5d", interval="5m"):
        calls[0] += 1
        return [(NOW, 3400.0)]      # non-empty sentinel; maths is patched below

    def fake_compute(series, release_dt, offsets_min=(5, 15, 30), now=None):
        return base, {5: rets[0], 15: rets[1], 30: rets[2]}

    monkeypatch.setattr(price_feed, "fetch_intraday_series", fake_fetch)
    monkeypatch.setattr(price_feed, "base_and_returns_from_series", fake_compute)
    return calls


def test_backfill_marks_rows_dirty_so_flush_writes_them(monkeypatch):
    """The bug: filled rows were never added to store.dirty, so flush() skipped
    the whole calibration_log tab and the sheet never received the returns."""
    _patch_prices(monkeypatch)
    s = _store_with_rows([_cal_row("cal:a"), _cal_row("cal:b")])

    attempted, filled = main_mod._backfill_xau_on_store(s, NOW)

    assert (attempted, filled) == (2, 2)
    # The claim in the log ("filled=2") must be backed by a real pending write.
    assert s.dirty["calibration_log"] == {"cal:a", "cal:b"}


def test_backfill_values_land_in_store_data(monkeypatch):
    _patch_prices(monkeypatch, base=3400.0, rets=(0.11, 0.22, 0.33))
    s = _store_with_rows([_cal_row("cal:a")])

    main_mod._backfill_xau_on_store(s, NOW)

    row = s.data["calibration_log"]["cal:a"]
    assert row["xau_return_5m"] == 0.11
    assert row["xau_return_15m"] == 0.22
    assert row["xau_return_30m"] == 0.33
    assert row["xau_base_price"] == 3400.0
    assert row["updated_at"]  # stamped by upsert, proving it went through


def test_backfill_is_idempotent_second_run_is_a_noop(monkeypatch):
    """Once filled, a re-run must NOT re-dirty the tab — that churn is exactly
    what the PR #65 no-op guard exists to prevent. The fix must keep that."""
    _patch_prices(monkeypatch)
    s = _store_with_rows([_cal_row("cal:a")])
    main_mod._backfill_xau_on_store(s, NOW)
    s.dirty["calibration_log"] = set()   # simulate a flush having happened

    attempted, filled = main_mod._backfill_xau_on_store(s, NOW)

    assert (attempted, filled) == (0, 0)          # already filled → not due
    assert s.dirty["calibration_log"] == set()    # no pointless rewrite


def test_backfill_does_not_restamp_existing_base_price(monkeypatch):
    _patch_prices(monkeypatch, base=3500.0)
    s = _store_with_rows([_cal_row("cal:a")])
    s.data["calibration_log"]["cal:a"]["xau_base_price"] = "3333.0"

    main_mod._backfill_xau_on_store(s, NOW)

    assert s.data["calibration_log"]["cal:a"]["xau_base_price"] == "3333.0"


def test_backfill_skips_rows_with_no_price_data(monkeypatch):
    """Off-hours / holiday: all offsets None → leave the row untouched and
    un-dirtied so it retries on a later run (while still inside 5 days)."""
    monkeypatch.setattr(price_feed, "fetch_intraday_series",
                        lambda ticker="GC=F", period="5d", interval="5m": [(NOW, 3400.0)])
    monkeypatch.setattr(price_feed, "base_and_returns_from_series",
                        lambda series, dt, offsets_min=(5, 15, 30), now=None:
                            (None, {5: None, 15: None, 30: None}))
    s = _store_with_rows([_cal_row("cal:a")])

    attempted, filled = main_mod._backfill_xau_on_store(s, NOW)

    assert (attempted, filled) == (1, 0)
    assert s.dirty["calibration_log"] == set()
    assert s.data["calibration_log"]["cal:a"]["xau_return_15m"] == ""


def test_backfill_fetches_the_series_once_for_many_rows(monkeypatch):
    """The 80-row cap existed only because every row re-fetched the same 5-day
    series. One fetch per RUN is what makes an uncapped backfill affordable."""
    calls = _patch_prices(monkeypatch)
    s = _store_with_rows([_cal_row(f"cal:{i}") for i in range(300)])

    attempted, filled = main_mod._backfill_xau_on_store(s, NOW)

    assert (attempted, filled) == (300, 300)     # no 80-row cap any more
    assert calls[0] == 1
    assert len(s.dirty["calibration_log"]) == 300


def test_backfill_defers_all_rows_when_the_fetch_fails(monkeypatch):
    """yfinance down: report attempted-but-unfilled and touch nothing, so the
    rows stay due and retry next run instead of being marked done."""
    monkeypatch.setattr(price_feed, "fetch_intraday_series",
                        lambda ticker="GC=F", period="5d", interval="5m": None)
    s = _store_with_rows([_cal_row("cal:a"), _cal_row("cal:b")])

    attempted, filled = main_mod._backfill_xau_on_store(s, NOW)

    assert (attempted, filled) == (2, 0)
    assert s.dirty["calibration_log"] == set()


def test_backfill_skips_rows_outside_the_intraday_window(monkeypatch):
    """Too fresh (<35 min, 30m bar not closed) and too old (>5 days, outside the
    yfinance 5-min window) must not be attempted."""
    calls = _patch_prices(monkeypatch)
    s = _store_with_rows([
        _cal_row("cal:fresh", minutes_ago=10),
        _cal_row("cal:ok", minutes_ago=60),
        _cal_row("cal:stale", minutes_ago=6 * 24 * 60),
    ])

    attempted, filled = main_mod._backfill_xau_on_store(s, NOW)

    assert (attempted, filled) == (1, 1)
    assert s.dirty["calibration_log"] == {"cal:ok"}
    assert calls[0] == 1
