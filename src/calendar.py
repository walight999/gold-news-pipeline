"""Economic calendar.

Source: ForexFactory free weekly JSON
    https://nfs.faireconomy.media/ff_calendar_thisweek.json

Schema per item:
    {title, country, date (ISO with tz), impact, forecast, previous}
    (NB: free endpoint does NOT include `actual` — see gold_impact_directional
    docstring for the consequence on post-release alerts.)

Three consumers:
    - Daily calendar push (06:30 ICT) — today's events for XAU-relevant currencies
    - Pre-release alert (every 10 min) — T-15min for high-impact USD/EUR events
    - Post-release alert — directional gold-impact guidance shortly after release
"""
from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx

from .utils_time import ICT, UTC, now_utc

log = logging.getLogger(__name__)

FF_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"

DEFAULT_DAILY_COUNTRIES   = ("USD", "EUR", "CNY", "JPY", "GBP")
DEFAULT_DAILY_IMPACTS     = ("High", "Medium")
DEFAULT_PRE_COUNTRIES     = ("USD", "EUR")
DEFAULT_PRE_IMPACTS       = ("High",)
DEFAULT_PRE_WINDOW_LOW    = 15   # alert if event releases in >= this many minutes
DEFAULT_PRE_WINDOW_HIGH   = 25   # ... and < this many minutes


@dataclass(frozen=True)
class CalEvent:
    event_id: str
    title: str
    country: str
    impact: str          # Low | Medium | High | Holiday
    forecast: str
    previous: str
    dt_utc: datetime

    @property
    def dt_ict(self) -> datetime:
        return self.dt_utc.astimezone(ICT)

    @property
    def hhmm_ict(self) -> str:
        return self.dt_ict.strftime("%H:%M")


def _make_event_id(title: str, dt_iso: str, country: str) -> str:
    raw = f"{country}|{dt_iso}|{title}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def fetch_calendar(url: str = FF_URL, timeout: float = 20.0) -> list[CalEvent]:
    with httpx.Client(timeout=timeout,
                      headers={"User-Agent": "gold-news-pipeline/1.0"}) as c:
        r = c.get(url, follow_redirects=True)
    r.raise_for_status()
    return parse_ff_payload(r.json())


def parse_ff_payload(data: list[dict[str, Any]]) -> list[CalEvent]:
    events: list[CalEvent] = []
    for e in data or []:
        try:
            dt_str = (e.get("date") or "").strip()
            if not dt_str:
                continue
            dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
            dt_utc = dt.astimezone(UTC)
            title   = (e.get("title")   or "").strip()
            country = (e.get("country") or "").strip().upper()
            if not title or not country:
                continue
            events.append(CalEvent(
                event_id=_make_event_id(title, dt_str, country),
                title=title, country=country,
                impact=(e.get("impact") or "Low").strip(),
                forecast=(e.get("forecast") or "").strip(),
                previous=(e.get("previous") or "").strip(),
                dt_utc=dt_utc,
            ))
        except (ValueError, TypeError, KeyError) as ex:
            log.warning("ff parse error: %s for %s", ex, e)
            continue
    events.sort(key=lambda x: x.dt_utc)
    return events


def filter_today_ict(events: list[CalEvent], ref: datetime | None = None) -> list[CalEvent]:
    ref_ict = (ref or now_utc()).astimezone(ICT)
    today_str = ref_ict.strftime("%Y-%m-%d")
    return [e for e in events if e.dt_ict.strftime("%Y-%m-%d") == today_str]


def filter_by_country(events: list[CalEvent], countries: tuple[str, ...]) -> list[CalEvent]:
    cs = {c.upper() for c in countries}
    return [e for e in events if e.country in cs]


def filter_by_impact(events: list[CalEvent], impacts: tuple[str, ...]) -> list[CalEvent]:
    iset = set(impacts)
    return [e for e in events if e.impact in iset]


def filter_upcoming(
    events: list[CalEvent],
    window_min_low: int = DEFAULT_PRE_WINDOW_LOW,
    window_min_high: int = DEFAULT_PRE_WINDOW_HIGH,
    ref: datetime | None = None,
) -> list[CalEvent]:
    """Events that release in [window_min_low, window_min_high) minutes from ref."""
    ref_utc = ref or now_utc()
    out: list[CalEvent] = []
    for e in events:
        delta_min = (e.dt_utc - ref_utc).total_seconds() / 60.0
        if window_min_low <= delta_min < window_min_high:
            out.append(e)
    return out


def minutes_until(event: CalEvent, ref: datetime | None = None) -> int:
    ref_utc = ref or now_utc()
    return int(round((event.dt_utc - ref_utc).total_seconds() / 60.0))


# ---------- gold-impact directional rules ----------

# Each rule: (regex applied to lower-cased title, inverse_flag)
# inverse=True  means higher value is BULLISH gold (e.g., higher unemployment).
# inverse=False means higher value is BEARISH gold (e.g., higher CPI).
_GOLD_IMPACT_RULES: list[tuple[re.Pattern[str], bool, str]] = [
    (re.compile(r"\b(unemployment rate|jobless claims|continuing claims)\b"), True,
     "Weaker labor data softens USD/yields"),
    (re.compile(r"\b(cpi|core cpi|pce|core pce|ppi|inflation expectations)\b"), False,
     "Higher inflation lifts USD/yields"),
    (re.compile(r"\b(nfp|non[- ]?farm|payroll|employment change|jobs report)\b"), False,
     "Stronger jobs lift USD/yields"),
    (re.compile(r"\b(retail sales|durable goods|consumer (confidence|sentiment))\b"), False,
     "Stronger demand lifts USD/yields"),
    (re.compile(r"\b(gdp|industrial production|manufacturing pmi|services pmi|ism)\b"), False,
     "Stronger growth lifts USD/yields"),
    (re.compile(r"\b(rate decision|federal funds|interest rate decision|fed funds|"
                r"main refinancing|deposit facility|bank rate)\b"), False,
     "Hike (or hawkish tone) lifts USD/yields"),
]


def gold_impact_directional(event: CalEvent) -> dict[str, str]:
    """Returns directional gold-impact guidance keyed off the event title.

    Phase 1 — free FF JSON does NOT include `actual`, so we can't say which
    way the release surprised. Instead we give the user the *map*: if higher
    than forecast → effect X; if lower → effect Y. Then they can read the
    actual from any news source.

    Keys: higher_is, lower_is, rationale.
    """
    title_low = event.title.lower()
    for pat, inverse, rationale in _GOLD_IMPACT_RULES:
        if pat.search(title_low):
            if inverse:
                return {
                    "higher_is": "🟢 Bullish gold",
                    "lower_is":  "🔴 Bearish gold",
                    "rationale": rationale,
                }
            return {
                "higher_is": "🔴 Bearish gold",
                "lower_is":  "🟢 Bullish gold",
                "rationale": rationale,
            }
    return {
        "higher_is": "🔴 Bearish gold (hawkish)",
        "lower_is":  "🟢 Bullish gold (dovish)",
        "rationale": "Watch official statement for tone",
    }


def forecast_vs_previous_effect(event: "CalEvent") -> dict[str, str]:
    """Given just forecast + previous (no actual yet), return the directional
    gold-impact emoji + label the market is currently PRICING IN.

    Returns {emoji, label}. Used by the pre-release bubble's Effect column.
    """
    if not event.forecast or not event.previous:
        return {"emoji": "🟡", "label": "n/a"}
    # Lazy import to avoid circular dep at module load
    from .fred import parse_forecast_value
    fc = parse_forecast_value(event.forecast)
    pv = parse_forecast_value(event.previous)
    if fc is None or pv is None:
        return {"emoji": "🟡", "label": "n/a"}
    diff = fc - pv
    if abs(diff) < 0.01:
        return {"emoji": "🟡", "label": "neutral"}
    impact = gold_impact_directional(event)
    side_text = impact["higher_is"] if diff > 0 else impact["lower_is"]
    if "Bullish" in side_text:
        return {"emoji": "🟢", "label": "bullish"}
    if "Bearish" in side_text:
        return {"emoji": "🔴", "label": "bearish"}
    return {"emoji": "🟡", "label": "neutral"}
