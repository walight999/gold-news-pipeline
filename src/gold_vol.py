"""Gold volatility regime (GVZ) — PRINT-ONLY PROTOTYPE.

A different pillar from the news firehose: instead of "what happened", this reads
"how scared is the gold market right now" from GVZ — the CBOE Gold ETF Volatility
Index (the "gold VIX"), implied vol on GLD options. For a gold trader the vol
REGIME is a position-sizing + expectation input: expanding vol = wider ranges,
2-way risk, size down; compressed vol = calm, breakouts from a coiled range mean
more. It is orthogonal to the news pipeline, which carries no vol dimension.

Source: FRED series GVZCLS via the KEYLESS graph-CSV endpoint (no FRED_API_KEY
needed — same series the keyed API in fred.py would return). FRED holds the full
daily history, so unlike prediction_odds this needs NO local snapshot: percentile
/ z-score / deltas are computed fresh from the fetched trailing window each run.

Print-only + standalone: not wired into main.py or macro_push.py. If it proves
useful the natural home is macro_push.py's `risk` factor (today VIX/SPX-based) —
GVZ is the gold-specific analogue. Run it, judge, then decide.

Run:  python -m src.gold_vol
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

log = logging.getLogger(__name__)

_CSV = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=GVZCLS"
_WINDOW = 252  # ~1 trading year for the percentile / z-score reference


@dataclass
class VolRead:
    date: str
    level: float
    d1: float | None          # 1-day change (abs vol points)
    d5: float | None          # 5-day change
    pct_rank: float           # 0..100 percentile of level within the window
    z: float                  # z-score of level within the window
    lo: float                 # window min
    hi: float                 # window max
    n: int                    # obs in window


def _fetch_series(url: str = _CSV, timeout: float = 20.0) -> list[tuple[str, float]]:
    """Return [(date, value)] for GVZCLS, oldest→newest, skipping blank (holiday)
    rows. Never raises for a bad row — a flaky FRED response yields a short/empty
    series and the caller reports 'no data' rather than crashing."""
    with httpx.Client() as c:
        r = c.get(url, timeout=timeout)
        r.raise_for_status()
    out: list[tuple[str, float]] = []
    for line in r.text.splitlines()[1:]:  # skip header
        parts = line.split(",")
        if len(parts) != 2:
            continue
        date, raw = parts[0].strip(), parts[1].strip()
        if not raw or raw == ".":          # FRED marks missing days with "."
            continue
        try:
            out.append((date, float(raw)))
        except ValueError:
            continue
    return out


def compute(series: list[tuple[str, float]], window: int = _WINDOW) -> VolRead | None:
    if not series:
        return None
    vals = [v for _, v in series]
    win = vals[-window:]
    n = len(win)
    latest_date, latest = series[-1]
    lo, hi = min(win), max(win)
    below = sum(1 for v in win if v <= latest)
    pct_rank = 100.0 * below / n
    mean = sum(win) / n
    var = sum((v - mean) ** 2 for v in win) / n if n > 1 else 0.0
    std = var ** 0.5
    z = (latest - mean) / std if std > 0 else 0.0
    d1 = latest - vals[-2] if len(vals) >= 2 else None
    d5 = latest - vals[-6] if len(vals) >= 6 else None
    return VolRead(latest_date, latest, d1, d5, pct_rank, z, lo, hi, n)


def _regime(pct: float) -> str:
    # Percentile-based (self-calibrating) — no hardcoded vol levels to go stale.
    if pct < 25:
        return "LOW / compressed"
    if pct < 50:
        return "below-normal"
    if pct < 75:
        return "elevated"
    if pct < 90:
        return "HIGH"
    return "STRESS (top-decile)"


def _xau_note(r: VolRead) -> str:
    trend = ""
    if r.d5 is not None:
        if r.d5 >= 1.0:
            trend = "vol EXPANDING (wider ranges, 2-way risk — size down)"
        elif r.d5 <= -1.0:
            trend = "vol COMPRESSING (calmer; coiled-range breakouts mean more)"
        else:
            trend = "vol flat"
    return trend


def render(r: VolRead | None) -> str:
    if r is None:
        return "GVZ: no data returned from FRED."
    lines = [
        "=" * 60,
        "GOLD VOL REGIME — GVZ (CBOE Gold ETF Vol Index)  — prototype",
        "=" * 60,
        f"as of {r.date}   level = {r.level:.2f}",
        f"regime : {_regime(r.pct_rank)}   ({r.pct_rank:.0f}th pct of {r.n}d, z={r.z:+.2f})",
        f"range  : {r.lo:.2f} – {r.hi:.2f}  (trailing {r.n}d)",
        f"change : 1d {r.d1:+.2f}   5d {r.d5:+.2f}" if r.d1 is not None and r.d5 is not None
        else f"change : 1d {r.d1}   5d {r.d5}",
        f"XAU    : {_xau_note(r)}",
        "=" * 60,
    ]
    return "\n".join(lines)


def main() -> None:
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s :: %(message)s")
    try:
        series = _fetch_series()
    except Exception as e:  # network/API — report, don't crash
        print(f"GVZ fetch failed: {e}")
        return
    print(render(compute(series)))


if __name__ == "__main__":
    main()
