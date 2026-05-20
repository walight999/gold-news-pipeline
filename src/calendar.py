"""Economic calendar.

Source: ForexFactory free weekly JSON
    https://nfs.faireconomy.media/ff_calendar_thisweek.json

Schema per item:
    {title, country, date (ISO with tz), impact, forecast, previous}

Two consumers:
    - Daily calendar push (06:30 ICT) — today's events for XAU-relevant currencies
    - Pre-release alert (every 15 min) — T-15min for high-impact USD/EUR events
"""
from __future__ import annotations

import hashlib
import logging
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
