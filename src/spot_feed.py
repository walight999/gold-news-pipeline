"""XAU/USD SPOT prices — the price the desk trades (not COMEX futures).

Two sources, each for what it can do:

* Swissquote public quote (no key): current spot bid/ask, real time.
  → current_spot(): the XAU level shown on cards.
* Dukascopy datafeed: historical 1-minute spot (BID).
  → minute_series(): grading / calibration of calls (base price at the print
    and the 5/15/30/60-min reaction). Complete UTC days come from one
    day-candle file; the current day from per-hour tick files, available
    once the hour has closed.

Why not yfinance: Yahoo delisted XAUUSD=X, so the pipeline used GC=F
(futures). Measured 2026-09-27 over 71 releases: GC=F-5m vs spot-1m 15-min
reactions differ by a median 0.11% and disagree on direction in 14/71 —
too noisy to grade a call against.

Dukascopy rejects python-httpx's TLS fingerprint and returns intermittent 503s
(~50%, 10-30 s per file), so downloads go through the system `curl` with
retries and a per-process file budget (MAX_FILES) that bounds run time.
Everything is best-effort: failures return None / {} and the caller falls back.
"""
from __future__ import annotations

import logging
import lzma
import os
import struct
import subprocess
import tempfile
import time
from datetime import date, datetime, timedelta, timezone

log = logging.getLogger(__name__)

SWISSQUOTE_URL = "https://forex-data-feed.swissquote.com/public-quotes/bboquotes/instrument/XAU/USD"
DUKA_BASE = "https://datafeed.dukascopy.com/datafeed/XAUUSD"
_SCALE = 1000.0                   # Dukascopy XAU integer price scale
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36"
MAX_FILES = int(os.environ.get("SPOT_MAX_FILES", "12"))
# Wall-clock budget for ALL Dukascopy downloads in one process. maintain runs
# under a 5-min job timeout and scorecard under 6 — past the budget every
# fetch returns None and callers fall back / retry on a later run.
TIME_BUDGET_S = float(os.environ.get("SPOT_TIME_BUDGET_S", "90"))

_files_used = [0]
_t0: list[float] = []
_cache: dict[str, dict[int, float]] = {}


# ---------------------------------------------------------------- current spot

def current_spot(timeout: float = 10.0) -> float | None:
    """Mid of Swissquote's first quoted spread profile, or None."""
    try:
        import httpx
        r = httpx.get(SWISSQUOTE_URL, timeout=timeout, headers={"User-Agent": _UA})
        r.raise_for_status()
        for platform in r.json() or []:
            for prof in platform.get("spreadProfilePrices") or []:
                bid, ask = prof.get("bid"), prof.get("ask")
                if bid and ask:
                    return (float(bid) + float(ask)) / 2
    except Exception as e:  # noqa: BLE001 — display decoration
        log.warning("swissquote spot failed: %s", e)
    return None


# ---------------------------------------------------------------- Dukascopy history

def _curl(url: str, attempts: int = 4) -> bytes | None:
    """Body bytes on 200, b"" on 404 (no data), None when it never succeeded."""
    if not _t0:
        _t0.append(time.time())
    if _files_used[0] >= MAX_FILES or time.time() - _t0[0] > TIME_BUDGET_S:
        return None
    _files_used[0] += 1
    for i in range(attempts):
        if time.time() - _t0[0] > TIME_BUDGET_S:
            break
        fd, tmp = tempfile.mkstemp(suffix=".bi5")
        os.close(fd)
        try:
            p = subprocess.run(["curl", "-s", "--max-time", "60", "-A", _UA, "-o", tmp,
                                "-w", "%{http_code}", url],
                               capture_output=True, text=True, timeout=90)
            code = p.stdout.strip()
            with open(tmp, "rb") as f:
                body = f.read()
        except (subprocess.SubprocessError, OSError):
            code, body = "000", b""
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass
        if code == "200":
            return body
        if code == "404":
            return b""
        time.sleep(min(20, 2 + 3 * i))      # 503 flakiness / rate limit
    log.warning("dukascopy fetch failed: %s", url)
    return None


def _day_minutes(d: date) -> dict[int, float] | None:
    key = f"d{d.isoformat()}"
    if key in _cache:
        return _cache[key]
    body = _curl(f"{DUKA_BASE}/{d.year}/{d.month - 1:02d}/{d.day:02d}/BID_candles_min_1.bi5")
    if body is None:
        return None
    raw = lzma.decompress(body) if body else b""
    day0 = int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp()) // 60
    out: dict[int, float] = {}
    rec = struct.Struct(">5if")
    for i in range(0, len(raw) - len(raw) % rec.size, rec.size):
        sec, o, c, _lo, _hi, vol = rec.unpack_from(raw, i)
        if vol <= 0 and o == c:
            continue                          # filler bar while the market is shut
        out[day0 + sec // 60] = c / _SCALE
    _cache[key] = out
    return out


def _hour_minutes(d: date, hour: int) -> dict[int, float] | None:
    key = f"h{d.isoformat()}T{hour:02d}"
    if key in _cache:
        return _cache[key]
    body = _curl(f"{DUKA_BASE}/{d.year}/{d.month - 1:02d}/{d.day:02d}/{hour:02d}h_ticks.bi5")
    if body is None:
        return None
    raw = lzma.decompress(body) if body else b""
    h0 = int(datetime(d.year, d.month, d.day, hour, tzinfo=timezone.utc).timestamp()) // 60
    out: dict[int, float] = {}
    rec = struct.Struct(">3i2f")
    for i in range(0, len(raw) - len(raw) % rec.size, rec.size):
        ms, _ask, bid, _av, _bv = rec.unpack_from(raw, i)
        out[h0 + ms // 60000] = bid / _SCALE     # last tick of the minute wins
    _cache[key] = out
    return out


def minute_series(start: datetime, end: datetime, now: datetime | None = None
                  ) -> list[tuple[datetime, float]] | None:
    """1-minute spot closes over [start, end] as [(minute_open_utc, close)].
    Past UTC days → one day file each; today → closed hours only. None if any
    needed file could not be fetched (caller falls back)."""
    now = now or datetime.now(timezone.utc)
    start = start.astimezone(timezone.utc)
    end = min(end.astimezone(timezone.utc), now)
    merged: dict[int, float] = {}
    t = start.replace(minute=0, second=0, microsecond=0)
    while t <= end:
        if t.date() < now.date():
            m = _day_minutes(t.date())
        elif t + timedelta(hours=1) <= now - timedelta(minutes=2):
            m = _hour_minutes(t.date(), t.hour)
        else:
            m = {}                                # current hour: not published yet
        if m is None:
            return None
        merged.update(m)
        t += timedelta(hours=1)
    if not merged:
        return None
    return [(datetime.fromtimestamp(k * 60, tz=timezone.utc), v) for k, v in sorted(merged.items())]
