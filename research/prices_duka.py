"""Dukascopy 1-minute BID candles, cached per instrument-day.

    https://datafeed.dukascopy.com/datafeed/{SYM}/{YYYY}/{MM0}/{DD}/BID_candles_min_1.bi5
    (MM0 = zero-based month). Body = LZMA; records of 24 bytes, big-endian:
    int32 seconds-from-00:00-UTC, int32 open, int32 close, int32 low,
    int32 high, float32 volume. Prices are integers scaled by POINT[sym].

The datafeed rate-limits bursts (429/503), so fetching is paced and retried.
It also rejects python-httpx's TLS fingerprint outright, so downloads go
through the system curl binary.
Cache: research/data/duka/{SYM}/{YYYY-MM-DD}.bin holds the decompressed
records ("" file = no trading that day, so it is not re-fetched).
"""
from __future__ import annotations

import lzma
import os
import struct
import time
from datetime import date, datetime, timedelta, timezone


DATA_DIR = os.path.join(os.path.dirname(__file__), "data", "duka")

# Asset key → (Dukascopy symbol, price divisor). Divisors verified against
# known levels (XAU ~2,000 in 2024, EURUSD ~1.08, USDJPY ~150, SPX ~5,000).
ASSETS: dict[str, tuple[str, float]] = {
    "XAU": ("XAUUSD", 1000.0),
    "EURUSD": ("EURUSD", 100000.0),
    "USDJPY": ("USDJPY", 1000.0),
    "GBPUSD": ("GBPUSD", 100000.0),
    "US500": ("USA500IDXUSD", 1000.0),
}

_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"}
_REC = struct.Struct(">5if")
_MIN_INTERVAL_S = 0.0   # latency-bound (~10-15s per file); pacing comes from worker count
_last_req = [0.0]   # per-thread pacing is fine: prefetch uses a few workers


def _url(sym: str, d: date) -> str:
    return (f"https://datafeed.dukascopy.com/datafeed/{sym}/{d.year}/"
            f"{d.month - 1:02d}/{d.day:02d}/BID_candles_min_1.bi5")


def _fetch_raw(sym: str, d: date, client=None) -> bytes:
    """Decompressed record bytes ('' when the day has no data).

    Uses the system `curl` binary: the datafeed rejects python-httpx's TLS
    fingerprint (503 while curl gets 200) and curl_cffi's impersonation
    profiles were flaky from here (timeouts / resets on most profiles)."""
    import subprocess
    import tempfile
    for attempt in range(15):
        wait = _MIN_INTERVAL_S - (time.time() - _last_req[0])
        if wait > 0:
            time.sleep(wait)
        _last_req[0] = time.time()
        fd, tmp = tempfile.mkstemp(suffix=".bi5")
        os.close(fd)
        try:
            p = subprocess.run(
                ["curl", "-s", "--max-time", "60", "-A", _HEADERS["User-Agent"],
                 "-o", tmp, "-w", "%{http_code}", _url(sym, d)],
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
            return lzma.decompress(body) if body else b""
        if code == "404":
            return b""
        # 503 "No server is available" is intermittent load-balancer flakiness
        # (~50% of requests), not a ban: short waits, escalating on repeats.
        time.sleep(min(60, 2 + 3 * attempt))
    raise RuntimeError(f"dukascopy {sym} {d} failed after retries")


def day_bars(asset: str, d: date, client=None) -> dict[int, tuple[float, float]]:
    """{minute-of-day (UTC): (open, close)} for one instrument-day."""
    sym, div = ASSETS[asset]
    path = os.path.join(DATA_DIR, sym, f"{d.isoformat()}.bin")
    if os.path.exists(path):
        with open(path, "rb") as f:
            raw = f.read()
    else:
        raw = _fetch_raw(sym, d)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(raw)
    out: dict[int, tuple[float, float]] = {}
    for i in range(0, len(raw) - len(raw) % _REC.size, _REC.size):
        sec, o, c, _lo, _hi, vol = _REC.unpack_from(raw, i)
        if vol <= 0 and o == c:
            continue          # flat filler bar (market closed) — not a real print
        out[sec // 60] = (o / div, c / div)
    return out


class PriceBook:
    """Lazy per-day cache of 1-minute bars across assets."""

    def __init__(self) -> None:
        self._days: dict[tuple[str, date], dict[int, tuple[float, float]]] = {}
        self._client = None

    def close(self) -> None:
        pass

    def _bars(self, asset: str, d: date):
        k = (asset, d)
        if k not in self._days:
            self._days[k] = day_bars(asset, d, self._client)
        return self._days[k]

    def price_at(self, asset: str, ts: datetime, use: str = "open",
                 search_back_min: int = 5) -> float | None:
        """Price at minute `ts` (UTC): the bar's open (= price AT that minute)
        or close. Walks back up to `search_back_min` minutes over gaps."""
        for back in range(search_back_min + 1):
            t = ts - timedelta(minutes=back)
            bars = self._bars(asset, t.date())
            bar = bars.get(t.hour * 60 + t.minute)
            if bar:
                return bar[0] if use == "open" else bar[1]
        return None


def _selftest() -> None:
    pb = PriceBook()
    ts = datetime(2024, 3, 12, 12, 30, tzinfo=timezone.utc)   # US CPI release
    for a in ASSETS:
        print(a, pb.price_at(a, ts - timedelta(minutes=1)), pb.price_at(a, ts + timedelta(minutes=15)))
    pb.close()


if __name__ == "__main__":
    _selftest()


def cache_path(asset: str, d: date) -> str:
    return os.path.join(DATA_DIR, ASSETS[asset][0], f"{d.isoformat()}.bin")


def prefetch(pairs: list[tuple[str, date]], workers: int = 3) -> int:
    """Download missing (asset, day) files with a small thread pool."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    todo = [(a, d) for a, d in pairs if not os.path.exists(cache_path(a, d))]
    done = 0

    def one(a, d):
        raw = _fetch_raw(ASSETS[a][0], d)
        os.makedirs(os.path.dirname(cache_path(a, d)), exist_ok=True)
        with open(cache_path(a, d), "wb") as f:
            f.write(raw)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(one, a, d): (a, d) for a, d in todo}
        for fut in as_completed(futs):
            try:
                fut.result()
                done += 1
            except Exception as e:  # noqa: BLE001 — report and keep going
                print("prefetch failed", futs[fut], e, flush=True)
            if done % 250 == 0:
                print(f"prefetch {done}/{len(todo)}", flush=True)
    return done
