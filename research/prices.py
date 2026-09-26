"""1-minute price history from HistData.com (free M1 bars).

One download per instrument-year (past years) or instrument-month (current
year) instead of one per day. Dukascopy's per-day datafeed (prices_duka.py) was
tried first: ~50% HTTP 503 and 10-19 s per file from here put the ~8,000 files
this dataset needs at ~40 hours. It is kept only as an independent source to
spot-check these prices (see validate_against_dukascopy).

HistData M1 ASCII format, one bar per line:
    YYYYMMDD HHMMSS;open;high;low;close;volume
Timestamps are EST **without** daylight saving (fixed UTC-5), per HistData's
docs, so UTC = stamp + 5 h all year round.

⚠ Measured, not as documented: during EU summer time (last Sunday of March →
last Sunday of October) the stamps run exactly 60 min LATE versus Dukascopy,
in every year 2019-2026 (lag search, zero error at +60; zero at 0 outside it;
boundary dates 2025-03-25 / 04-01 / 10-28 and 2024-03-12 / 03-26 confirm the
EU — not US — switch dates). parse_m1 applies that correction. Without it
half of every year's reactions were read an hour off the release.

Download flow: GET the instrument page to read the hidden form token, then
POST it to /get.php → a zip holding the CSV. curl_cffi chrome impersonation.
Cache: research/data/histdata/{SYM}_{YYYY}[_{MM}].zip
"""
from __future__ import annotations

import io
import os
import re
import time
import zipfile
from datetime import date, datetime, timedelta, timezone

import numpy as np

DATA_DIR = os.path.join(os.path.dirname(__file__), "data", "histdata")

# Asset key → HistData symbol.
ASSETS: dict[str, str] = {
    "XAU": "XAUUSD",
    "EURUSD": "EURUSD",
    "USDJPY": "USDJPY",
    "GBPUSD": "GBPUSD",
    "US500": "SPXUSD",
}
_EST_TO_UTC = timedelta(hours=5)
_PAGE = "https://www.histdata.com/download-free-forex-historical-data/?/ascii/1-minute-bar-quotes/{sym}/{period}"
_INPUT = re.compile(r'<input type="hidden" name="(\w+)" id="\w+" value="([^"]*)"')


def _file_id(sym: str, year: int, month: int | None) -> str:
    return f"{sym}_{year}" if month is None else f"{sym}_{year}_{month:02d}"


def _zip_path(sym: str, year: int, month: int | None) -> str:
    tail = f"{year}" if month is None else f"{year}_{month:02d}"
    return os.path.join(DATA_DIR, f"{sym}_{tail}.zip")


def _download(sym: str, year: int, month: int | None) -> bytes | None:
    """Zip bytes, or None when HistData has no file for that period (yet)."""
    from curl_cffi import requests as cffi
    period = f"{year}" if month is None else f"{year}/{month}"
    url = _PAGE.format(sym=sym.lower(), period=period)
    last = None
    for attempt in range(4):
        try:
            s = cffi.Session(impersonate="chrome124")
            page = s.get(url, timeout=60)
            form = dict(_INPUT.findall(page.text))
            if "tk" not in form:
                return None                      # no such period published
            r = s.post("https://www.histdata.com/get.php", data=form,
                       headers={"Referer": url}, timeout=180)
            if r.status_code == 200 and r.content[:2] == b"PK":
                return r.content
            last = f"HTTP {r.status_code} {r.content[:60]!r}"
        except Exception as e:  # noqa: BLE001 — network retry loop
            last = repr(e)
        time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"histdata {sym} {period} failed: {last}")


def _get_zip(sym: str, year: int, month: int | None) -> bytes | None:
    path = _zip_path(sym, year, month)
    current = (year, month) == (date.today().year, date.today().month)
    if os.path.exists(path) and not current:
        with open(path, "rb") as f:
            return f.read() or None
    data = _download(sym, year, month)
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(path, "wb") as f:
        f.write(data or b"")                     # empty file = "not published", cached
    return data


def _eu_summer_mask(m: np.ndarray) -> np.ndarray:
    """True where epoch-minute `m` falls in EU summer time (01:00 UTC on the
    last Sunday of March → 01:00 UTC on the last Sunday of October)."""
    def last_sunday(year: int, month: int) -> datetime:
        d = datetime(year, month + 1, 1, 1, 0, tzinfo=timezone.utc) - timedelta(days=1)
        while d.weekday() != 6:
            d -= timedelta(days=1)
        return d
    mask = np.zeros(len(m), dtype=bool)
    if not len(m):
        return mask
    y0 = datetime.fromtimestamp(int(m.min()) * 60, tz=timezone.utc).year
    y1 = datetime.fromtimestamp(int(m.max()) * 60, tz=timezone.utc).year
    for y in range(y0, y1 + 1):
        a = int(last_sunday(y, 3).timestamp()) // 60
        b = int(last_sunday(y, 10).timestamp()) // 60
        mask |= (m >= a) & (m < b)
    return mask


def parse_m1(csv_text: str) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """→ (utc epoch minutes int64 sorted, open float64, close float64)."""
    mins, opens, closes = [], [], []
    for line in csv_text.splitlines():
        parts = line.strip().split(";")
        if len(parts) < 5:
            continue
        try:
            t = datetime.strptime(parts[0], "%Y%m%d %H%M%S")
            o, c = float(parts[1]), float(parts[4])
        except ValueError:
            continue
        utc = (t + _EST_TO_UTC).replace(tzinfo=timezone.utc)
        mins.append(int(utc.timestamp()) // 60)
        opens.append(o)
        closes.append(c)
    m = np.asarray(mins, dtype=np.int64)
    m = m - np.where(_eu_summer_mask(m), 60, 0)          # see module docstring
    order = np.argsort(m, kind="stable")
    return m[order], np.asarray(opens)[order], np.asarray(closes)[order]


def _read_csv_from_zip(blob: bytes) -> str:
    z = zipfile.ZipFile(io.BytesIO(blob))
    name = next(n for n in z.namelist() if n.lower().endswith(".csv"))
    return z.read(name).decode("utf-8", errors="replace")


class PriceBook:
    """All 1-minute bars for the requested years, per asset, in numpy arrays."""

    def __init__(self, years) -> None:
        self._m: dict[str, np.ndarray] = {}
        self._o: dict[str, np.ndarray] = {}
        self._c: dict[str, np.ndarray] = {}
        today = date.today()
        for asset, sym in ASSETS.items():
            parts = []
            for y in years:
                periods = [(y, None)] if y < today.year else [(y, m) for m in range(1, today.month + 1)]
                for yy, mm in periods:
                    b = _get_zip(sym, yy, mm)
                    if b:
                        parts.append(parse_m1(_read_csv_from_zip(b)))
            if not parts:
                continue
            m = np.concatenate([p[0] for p in parts])
            o = np.concatenate([p[1] for p in parts])
            c = np.concatenate([p[2] for p in parts])
            order = np.argsort(m, kind="stable")
            self._m[asset], self._o[asset], self._c[asset] = m[order], o[order], c[order]

    def close(self) -> None:          # API parity with the old per-day book
        pass

    def coverage(self, asset: str):
        m = self._m.get(asset)
        if m is None or not len(m):
            return None
        f = lambda x: datetime.fromtimestamp(int(x) * 60, tz=timezone.utc)  # noqa: E731
        return f(m[0]), f(m[-1])

    def price_at(self, asset: str, ts: datetime, use: str = "open",
                 search_back_min: int = 5) -> float | None:
        """Price of the bar starting at minute `ts` (UTC): its open (the price
        AT that minute) or its close. Walks back up to `search_back_min`
        minutes over gaps; None if no bar in that range."""
        m = self._m.get(asset)
        if m is None:
            return None
        target = int(ts.timestamp()) // 60
        i = int(np.searchsorted(m, target, side="right")) - 1
        if i < 0 or target - int(m[i]) > search_back_min:
            return None
        return float(self._o[asset][i] if use == "open" else self._c[asset][i])


def validate_against_dukascopy(pb: PriceBook, stamps: list[datetime]) -> list[tuple]:
    """Spot-check: same minutes from an independent feed. Returns rows of
    (asset, ts, histdata_close, dukascopy_close, abs % diff)."""
    from research import prices_duka
    duka_map = {"XAU": "XAU", "EURUSD": "EURUSD", "USDJPY": "USDJPY",
                "GBPUSD": "GBPUSD", "US500": "US500"}
    db = prices_duka.PriceBook()
    out = []
    for ts in stamps:
        for a in ASSETS:
            h = pb.price_at(a, ts, "close")
            d = db.price_at(duka_map[a], ts, "close")
            diff = abs(h / d - 1) * 100 if h and d else None
            out.append((a, ts.isoformat(), h, d, diff))
    return out


if __name__ == "__main__":
    pb = PriceBook([2024])
    ts = datetime(2024, 3, 12, 12, 29, tzinfo=timezone.utc)   # minute before US CPI
    for row in validate_against_dukascopy(pb, [ts, ts + timedelta(minutes=16)]):
        print(row)
