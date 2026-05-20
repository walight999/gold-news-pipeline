"""FRED API client — fetches actual values for US economic releases.

Free signup: https://fred.stlouisfed.org/docs/api/api_key.html
Set FRED_API_KEY env var to enable; without it, fetch_actual returns None
and the rest of the pipeline keeps working with directional guidance only.

Supported series (Phase 2.1 starter set — most-watched for XAU):
    CPI m/m            CPIAUCSL    transform mom_pct
    Core CPI m/m       CPILFESL    transform mom_pct
    PCE m/m            PCEPI       transform mom_pct
    Core PCE m/m       PCEPILFE    transform mom_pct
    NFP                PAYEMS      transform delta_k    (level → monthly delta)
    Unemployment Rate  UNRATE      transform level_pct
    Fed Funds Rate     DFEDTARU    transform level_pct
    PPI m/m            PPIACO      transform mom_pct
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass

import httpx

log = logging.getLogger(__name__)

FRED_BASE = "https://api.stlouisfed.org/fred"

# (FF-title regex, series_id, transform name)
# transform values: mom_pct, delta_k, level_pct
_SERIES_MAP: list[tuple[re.Pattern[str], str, str]] = [
    (re.compile(r"\bcore cpi m/m\b",                       re.I), "CPILFESL", "mom_pct"),
    (re.compile(r"\bcpi m/m\b",                            re.I), "CPIAUCSL", "mom_pct"),
    (re.compile(r"\bcore pce price index m/m\b",           re.I), "PCEPILFE", "mom_pct"),
    (re.compile(r"\bpce price index m/m\b",                re.I), "PCEPI",    "mom_pct"),
    (re.compile(r"\b(non[- ]?farm.*employ.*change|nfp|non[- ]?farm payroll)\b", re.I), "PAYEMS", "delta_k"),
    (re.compile(r"\bunemployment rate\b",                  re.I), "UNRATE",   "level_pct"),
    (re.compile(r"\b(federal funds rate|fed funds rate)\b", re.I), "DFEDTARU", "level_pct"),
    (re.compile(r"\bcore ppi m/m\b",                       re.I), "PPILFE",   "mom_pct"),
    (re.compile(r"\bppi m/m\b",                            re.I), "PPIACO",   "mom_pct"),
]


@dataclass(frozen=True)
class FredResult:
    series_id: str
    actual_text: str        # human-readable, e.g. "+0.4%", "215K", "3.9%"
    actual_value: float     # numeric value in the same unit as actual_text (sans suffix)
    observation_date: str   # YYYY-MM-DD from FRED


def fred_api_key() -> str:
    return os.environ.get("FRED_API_KEY", "").strip()


def find_series_for_event(title: str) -> tuple[str, str] | None:
    for pat, sid, transform in _SERIES_MAP:
        if pat.search(title):
            return (sid, transform)
    return None


def _get_observations(series_id: str, api_key: str, n: int) -> list[dict]:
    url = f"{FRED_BASE}/series/observations"
    params = {
        "series_id": series_id,
        "api_key": api_key,
        "file_type": "json",
        "sort_order": "desc",
        "limit": str(n),
    }
    with httpx.Client(timeout=15.0) as c:
        r = c.get(url, params=params)
    r.raise_for_status()
    obs = r.json().get("observations", [])
    # Drop sentinel "." values FRED uses for unavailable data
    return [o for o in obs if o.get("value", ".") != "."]


def fetch_actual(title: str, api_key: str | None = None) -> FredResult | None:
    """Try to fetch the actual value for a calendar event.

    Returns None if:
      - No FRED_API_KEY configured.
      - Event title doesn't map to any supported series.
      - FRED has fewer than 2 observations for the series (needed for deltas).
      - HTTP error.
    """
    if api_key is None:
        api_key = fred_api_key()
    if not api_key:
        return None

    series = find_series_for_event(title)
    if not series:
        return None
    sid, transform = series

    try:
        obs = _get_observations(sid, api_key, n=2)
    except (httpx.HTTPError, ValueError) as e:
        log.warning("fred fetch failed series=%s: %s", sid, e)
        return None
    if not obs:
        return None

    try:
        latest_val = float(obs[0]["value"])
    except (KeyError, ValueError):
        return None
    obs_date = obs[0].get("date", "")

    if transform == "mom_pct":
        if len(obs) < 2:
            return None
        try:
            prev_val = float(obs[1]["value"])
        except (KeyError, ValueError):
            return None
        if prev_val == 0:
            return None
        pct = (latest_val - prev_val) / prev_val * 100
        return FredResult(sid, f"{pct:+.1f}%", round(pct, 2), obs_date)

    if transform == "delta_k":
        if len(obs) < 2:
            return None
        try:
            prev_val = float(obs[1]["value"])
        except (KeyError, ValueError):
            return None
        delta_k = latest_val - prev_val   # PAYEMS already in thousands of jobs
        return FredResult(sid, f"{delta_k:+.0f}K", round(delta_k, 0), obs_date)

    if transform == "level_pct":
        return FredResult(sid, f"{latest_val:.2f}%", round(latest_val, 2), obs_date)

    return None


def parse_forecast_value(text: str) -> float | None:
    """Parse FF forecast strings ('0.3%', '+215K', '3.9%') to floats in the
    same unit as FredResult.actual_value (i.e. percentage points or thousands)."""
    if not text:
        return None
    s = text.strip().replace(",", "").replace("+", "")
    if s.endswith("%"):
        s = s[:-1]
    elif s.endswith("K"):
        s = s[:-1]
    elif s.endswith("M"):
        s = s[:-1]
        try:
            return float(s) * 1000
        except ValueError:
            return None
    try:
        return float(s)
    except ValueError:
        return None


def compute_surprise_label(actual: float, forecast: float, tolerance_pct: float = 5.0) -> str:
    """beat / miss / in-line.
    'In-line' if |diff| is within tolerance_pct % of |forecast| (default 5%).
    """
    if forecast == 0:
        return "in-line" if actual == 0 else ("beat" if actual > 0 else "miss")
    diff = actual - forecast
    if abs(diff) <= abs(forecast) * (tolerance_pct / 100.0):
        return "in-line"
    return "beat" if diff > 0 else "miss"


def reconcile_with_impact(surprise: str, impact: dict[str, str]) -> str:
    """Combine surprise label + directional guidance into the verdict text
    (e.g., 'beat' on CPI → 'Bearish gold' since higher CPI is bearish)."""
    if surprise == "in-line":
        return "⚪ Neutral — print matched forecast"
    if surprise == "beat":
        # actual > forecast → use higher_is mapping
        return impact["higher_is"]
    # surprise == "miss"
    return impact["lower_is"]
