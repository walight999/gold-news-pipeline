"""ForexFactory calendar history → research/data/ff_events.csv.

The monthly calendar page (forexfactory.com/calendar?month=mar.2024) embeds the
whole month as JSON in `window.calendarComponentStates[1] = {days: [...]}`.
Each event carries:
    dateline   UTC epoch of the release (no timezone guessing needed)
    ebaseId    stable id of the event TYPE ("US Core CPI m/m" is always 79)
    currency, impactName, name, actual, forecast, previous, revision
    actualBetterWorse   FF's own read: 1 = better for the currency, 2 = worse

Pages are cached under research/data/ff_raw/ so re-runs only fetch new months.
curl_cffi chrome impersonation is required (plain HTTP clients get a 403).

    python -m research.ff_history --start 2019-01 --end 2026-09
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import time
from datetime import date, datetime, timezone

log = logging.getLogger(__name__)

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
RAW_DIR = os.path.join(DATA_DIR, "ff_raw")
OUT_CSV = os.path.join(DATA_DIR, "ff_events.csv")
_MON = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]

FIELDS = ["event_uid", "ebase_id", "dt_utc", "currency", "impact", "title",
          "actual", "forecast", "previous", "revision", "better_worse"]


def _months(start: str, end: str):
    y, m = map(int, start.split("-"))
    ey, em = map(int, end.split("-"))
    while (y, m) <= (ey, em):
        yield y, m
        m += 1
        if m == 13:
            y, m = y + 1, 1


def extract_days(html: str) -> list[dict]:
    """Pull the `days: [...]` JSON array out of the page (bracket-matched)."""
    anchor = html.find("calendarComponentStates[1]")
    if anchor < 0:
        raise ValueError("calendarComponentStates not found (blocked / layout change?)")
    i = html.find("days: [", anchor)
    if i < 0:
        raise ValueError("days array not found")
    i += len("days: ")
    depth, in_str, esc = 0, False, False
    for k in range(i, len(html)):
        c = html[k]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                return json.loads(html[i:k + 1])
    raise ValueError("unterminated days array")


def events_from_days(days: list[dict]) -> list[dict]:
    out = []
    for d in days:
        for e in d.get("events") or []:
            ts = e.get("dateline")
            if not ts or e.get("timeMasked"):
                continue          # "All Day" / "Tentative" — no usable release minute
            out.append({
                "event_uid": e.get("id"),
                "ebase_id": e.get("ebaseId"),
                "dt_utc": datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "currency": e.get("currency") or "",
                "impact": (e.get("impactName") or "").lower(),
                "title": e.get("name") or "",
                "actual": e.get("actual") or "",
                "forecast": e.get("forecast") or "",
                "previous": e.get("previous") or "",
                "revision": e.get("revision") or "",
                "better_worse": e.get("actualBetterWorse") or 0,
            })
    return out


def _fetch_month(y: int, m: int, force: bool = False) -> str:
    os.makedirs(RAW_DIR, exist_ok=True)
    path = os.path.join(RAW_DIR, f"{y}-{m:02d}.html")
    current = (y, m) >= (date.today().year, date.today().month)
    if os.path.exists(path) and not force and not current:
        with open(path, encoding="utf-8") as f:
            return f.read()
    from curl_cffi import requests as cffi
    url = f"https://www.forexfactory.com/calendar?month={_MON[m - 1]}.{y}"
    last = None
    for attempt in range(4):
        try:
            r = cffi.get(url, impersonate="chrome", timeout=40)
            if r.status_code == 200 and "calendarComponentStates" in r.text:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(r.text)
                return r.text
            last = f"HTTP {r.status_code}"
        except Exception as e:  # noqa: BLE001 — network retry loop
            last = repr(e)
        time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"ff month {y}-{m:02d} failed: {last}")


def build(start: str, end: str, force: bool = False) -> int:
    rows: dict[str, dict] = {}
    for y, m in _months(start, end):
        html = _fetch_month(y, m, force)
        evs = events_from_days(extract_days(html))
        for e in evs:
            rows[str(e["event_uid"])] = e
        log.info("%d-%02d: %d events", y, m, len(evs))
        time.sleep(1.5)
    os.makedirs(DATA_DIR, exist_ok=True)
    ordered = sorted(rows.values(), key=lambda r: (r["dt_utc"], str(r["event_uid"])))
    with open(OUT_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(ordered)
    return len(ordered)


def load_events() -> list[dict]:
    with open(OUT_CSV, encoding="utf-8") as f:
        return list(csv.DictReader(f))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2019-01")
    ap.add_argument("--end", default=date.today().strftime("%Y-%m"))
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    print("events:", build(a.start, a.end, a.force))
