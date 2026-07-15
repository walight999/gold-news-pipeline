"""XAU release base price — must be the pre-print bar, not the bar the release
falls into (which already carries ~5 min of the reaction)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd

from src import price_feed


def test_base_price_uses_pre_release_bar(monkeypatch):
    rel = datetime(2026, 7, 15, 13, 30, tzinfo=timezone.utc)
    # yfinance 5-min bars are stamped by OPEN time. The 13:25 bar (opens 13:25,
    # closes 13:30) is the last price BEFORE the print; the 13:30 bar already
    # contains the spike. Base must be the 13:25 close (2001), not 2010.
    times = [rel - timedelta(minutes=10), rel - timedelta(minutes=5),
             rel, rel + timedelta(minutes=5)]
    closes = [2000.0, 2001.0, 2010.0, 2012.0]
    df = pd.DataFrame({"Close": closes}, index=pd.DatetimeIndex(times))

    class _Ticker:
        def __init__(self, *a, **k): pass
        def history(self, **k): return df

    class _YF:
        Ticker = _Ticker

    monkeypatch.setattr(price_feed, "_yf", lambda: _YF)
    base, _ = price_feed.xau_base_and_returns_from_release(rel, (5,))
    assert base == 2001.0
