"""Price feed for XAU (gold) and DXY (US dollar index).

Source: Yahoo Finance via yfinance — free, no API key, 1-min and 5-min
intraday available for the last ~7-60 days. Sufficient for post-release
reaction snapshots; daily granularity used for top-line panels.

Tickers:
    GC=F      Comex gold front-month futures (proxy for XAU spot — moves
              within ~0.5% of spot under normal conditions).
    DX-Y.NYB  Dollar index (ICE).

Yfinance occasionally rate-limits / 429s — every public helper here
returns None on failure rather than raising, so callers can render a
graceful "no price data" fallback.

XAUUSD=X (spot) is delisted on Yahoo, hence the futures proxy.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

log = logging.getLogger(__name__)

# Module-level import is lazy — yfinance pulls a lot at import time and we
# only need it when a price call actually fires.


@dataclass(frozen=True)
class PriceSnapshot:
    ticker: str
    last: float
    prev_close: float
    pct_change_day: float
    bar_time_utc: datetime


def _yf():
    import yfinance as yf
    return yf


def get_snapshot(ticker: str) -> PriceSnapshot | None:
    """Last close + day change. Returns None on any failure."""
    try:
        yf = _yf()
        t = yf.Ticker(ticker)
        h = t.history(period="2d", interval="1d")
        if h.empty or len(h) < 1:
            return None
        last_row = h.iloc[-1]
        last = float(last_row["Close"])
        prev = float(h.iloc[-2]["Close"]) if len(h) >= 2 else last
        bar_dt = h.index[-1].to_pydatetime()
        # Yahoo returns tz-aware index for most tickers, but normalise to UTC
        if bar_dt.tzinfo is None:
            bar_dt = bar_dt.replace(tzinfo=timezone.utc)
        else:
            bar_dt = bar_dt.astimezone(timezone.utc)
        pct = ((last - prev) / prev * 100) if prev else 0.0
        return PriceSnapshot(ticker=ticker, last=last, prev_close=prev,
                             pct_change_day=pct, bar_time_utc=bar_dt)
    except Exception as e:
        log.warning("price snapshot failed for %s: %s", ticker, e)
        return None


def get_xau_snapshot() -> PriceSnapshot | None:
    return get_snapshot("GC=F")


def get_dxy_snapshot() -> PriceSnapshot | None:
    return get_snapshot("DX-Y.NYB")


def get_intraday_price_at(ticker: str, ref_dt: datetime, lookback_min: int = 60) -> float | None:
    """Returns the last close at-or-before `ref_dt` from 5-min intraday data.

    `ref_dt` should be a tz-aware datetime. Useful for "what was XAU at
    the moment this release printed". None if no bar covers that time.
    """
    try:
        yf = _yf()
        t = yf.Ticker(ticker)
        # Fetch a window around the target time to ensure we have a bar
        period = "1d" if (datetime.now(timezone.utc) - ref_dt) < timedelta(hours=20) else "5d"
        h = t.history(period=period, interval="5m")
        if h.empty:
            return None
        # Normalise index to UTC for comparison
        idx_utc = []
        for ts in h.index:
            py = ts.to_pydatetime()
            if py.tzinfo is None:
                py = py.replace(tzinfo=timezone.utc)
            idx_utc.append(py.astimezone(timezone.utc))
        target = ref_dt.astimezone(timezone.utc) if ref_dt.tzinfo else ref_dt.replace(tzinfo=timezone.utc)
        # Find the last bar with time <= target
        best_idx = -1
        for i, ts in enumerate(idx_utc):
            if ts <= target:
                best_idx = i
            else:
                break
        if best_idx == -1:
            return None
        return float(h.iloc[best_idx]["Close"])
    except Exception as e:
        log.warning("intraday lookup failed for %s @ %s: %s", ticker, ref_dt, e)
        return None


def xau_return_pct(release_dt_utc: datetime, minutes_after: int = 5) -> float | None:
    """Returns XAU % change from `release_dt_utc` to release+`minutes_after`.

    Used for the post-release reaction display + Phase 3 calibration of
    base_impact via xau_return_5m/15m/30m. Returns None if either price
    point is unavailable (off-hours, holidays, etc.).
    """
    price_at_release = get_intraday_price_at("GC=F", release_dt_utc)
    if price_at_release is None or price_at_release == 0:
        return None
    later = release_dt_utc + timedelta(minutes=minutes_after)
    if later > datetime.now(timezone.utc):
        return None   # the future hasn't happened yet
    price_later = get_intraday_price_at("GC=F", later)
    if price_later is None:
        return None
    return (price_later - price_at_release) / price_at_release * 100
