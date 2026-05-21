"""ForexFactory HTML scraper — fallback for next-week data.

The free `ff_calendar_thisweek.json` endpoint only ships ISO-week-current
data and rolls over Sunday morning ET, so Saturday-morning ICT runs of
weekly_preview find nothing. The HTML calendar page (forexfactory.com/calendar
?week=next) DOES carry next-week data the whole time; we just have to
bypass Cloudflare.

curl_cffi with `chrome124` TLS impersonation gets through. Plain httpx /
requests are blocked at 403.

Returns dicts in the same shape as FF JSON so parse_ff_payload can ingest
them transparently:
    {title, country, date (ISO 8601 with offset), impact, forecast, previous}
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup
from curl_cffi import requests as cffi

log = logging.getLogger(__name__)

CALENDAR_URL = "https://www.forexfactory.com/calendar?week=next"

# FF page renders in Eastern Time by default for non-logged-in visitors.
# zoneinfo handles EDT/EST automatically.
ET = ZoneInfo("America/New_York")

_MONTHS = {"Jan":1,"Feb":2,"Mar":3,"Apr":4,"May":5,"Jun":6,
           "Jul":7,"Aug":8,"Sep":9,"Oct":10,"Nov":11,"Dec":12}

_IMPACT_MAP = {
    "red": "High",
    "ora": "Medium",
    "yel": "Low",
    "gra": "Holiday",
}


def _parse_impact(td) -> str:
    span = td.find("span", class_=re.compile(r"icon--ff-impact"))
    if not span:
        return "Low"
    for c in span.get("class", []):
        m = re.search(r"impact-(\w{3})", c)
        if m:
            return _IMPACT_MAP.get(m.group(1), "Low")
    return "Low"


def _parse_time(s: str) -> tuple[int, int] | None:
    s = (s or "").strip().lower()
    if not s or s in ("all day", "tentative"):
        return None
    # Require explicit am/pm — bare numbers are too ambiguous (some FF rows
    # show "12 hours" or "3rd" which would otherwise misparse).
    m = re.match(r"^(\d{1,2}):?(\d{2})?\s*(am|pm)$", s)
    if not m:
        return None
    h = int(m.group(1))
    mm = int(m.group(2) or 0)
    ap = m.group(3)
    if ap == "pm" and h < 12:
        h += 12
    elif ap == "am" and h == 12:
        h = 0
    if not (0 <= h <= 23 and 0 <= mm <= 59):
        return None
    return (h, mm)


def _resolve_year(month: int, ref: datetime) -> int:
    """If parsed month is many months behind ref, assume next year (Dec→Jan)."""
    if month < ref.month - 6:
        return ref.year + 1
    return ref.year


def _parse_date_header(text: str, ref: datetime) -> datetime | None:
    """FF day header looks like 'SunMay 24'. Returns ET midnight on that day."""
    m = re.match(r"[A-Za-z]{3}([A-Za-z]{3})\s+(\d+)", (text or "").strip())
    if not m:
        return None
    mon = _MONTHS.get(m.group(1))
    if not mon:
        return None
    day = int(m.group(2))
    year = _resolve_year(mon, ref)
    try:
        return datetime(year, mon, day, tzinfo=ET)
    except ValueError:
        return None


def scrape_ff_html(
    url: str = CALENDAR_URL,
    impersonate: str = "chrome124",
    timeout: float = 25.0,
) -> list[dict[str, Any]]:
    """Returns a list of FF-JSON-shaped dicts for the requested week."""
    r = cffi.get(url, impersonate=impersonate, timeout=timeout)
    if r.status_code != 200:
        log.warning("FF scrape HTTP %s for %s", r.status_code, url)
        return []
    soup = BeautifulSoup(r.text, "lxml")
    table = soup.find("table", class_="calendar__table")
    if not table:
        log.warning("FF scrape: no calendar__table in response")
        return []

    ref_now = datetime.now(timezone.utc)
    current_date_et: datetime | None = None
    last_time: tuple[int, int] | None = None
    out: list[dict[str, Any]] = []

    for tr in table.find_all("tr"):
        cls = tr.get("class", []) or []
        if any("day-breaker" in c or "calendar__row--day" in c for c in cls):
            current_date_et = _parse_date_header(tr.get_text(strip=True), ref_now)
            last_time = None
            continue

        ccy_td = tr.find("td", class_="calendar__currency")
        evt_td = tr.find("td", class_="calendar__event")
        if not (current_date_et and ccy_td and evt_td):
            continue

        time_td = tr.find("td", class_="calendar__time")
        imp_td  = tr.find("td", class_="calendar__impact")
        fc_td   = tr.find("td", class_="calendar__forecast")
        prv_td  = tr.find("td", class_="calendar__previous")

        time_s = time_td.get_text(strip=True) if time_td else ""
        if time_s:
            parsed = _parse_time(time_s)
            if parsed:
                last_time = parsed
        if not last_time:
            # "All Day", "Tentative", or unparseable — skip; we only care
            # about time-specific releases for the preview.
            continue
        h, mm = last_time
        dt_et = current_date_et.replace(hour=h, minute=mm)

        country = ccy_td.get_text(strip=True).upper()
        title = evt_td.get_text(strip=True)
        if not country or not title:
            continue

        out.append({
            "title": title,
            "country": country,
            "date": dt_et.isoformat(),
            "impact": _parse_impact(imp_td) if imp_td else "Low",
            "forecast": fc_td.get_text(strip=True) if fc_td else "",
            "previous": prv_td.get_text(strip=True) if prv_td else "",
        })

    log.info("FF scrape parsed %d events from %s", len(out), url)
    return out
