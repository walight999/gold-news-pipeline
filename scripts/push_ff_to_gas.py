"""Push FF calendar data into the gold-analyst GAS WeeklyCache WebApp.

The GAS WebApp (paste-into-GAS code lives in the **private** gold-analyst
repo at `scripts/WeeklyCache.gs`) caches the payload in ScriptProperties
so the LINE bot's Saturday Weekly Brief and daily Calendar V2 broadcasts
can read fresh data even when:

- FF's `ff_calendar_nextweek.json` hasn't rolled (Sat morning ICT before
  the rollover window), OR
- FF's CDN rate-limits the GAS UrlFetchApp (HTTP 429 on shared egress IPs)

Two modes:

  weekly    — scrapes the FF HTML calendar?week=next page via the existing
              `src/ff_scraper.py:scrape_ff_html` (curl_cffi Cloudflare
              bypass) and POSTs `{kind: weekly_html, events: [...]}` —
              fills `ff_weekly_cache_v1` ScriptProperty.

  thisweek  — fetches `nfs.faireconomy.media/ff_calendar_thisweek.json`
              directly (no Cloudflare in front of the CDN, just rate-limit)
              and POSTs `{kind: thisweek_json, raw_json: "..."}` —
              passes through into `FF_LAST_JSON` (the same key
              `ffFetchWeekJson` already reads in goldbot_gas.js, so the
              Calendar V2 broadcast picks it up with no Code.gs change).

Env:
  GAS_WEBAPP_URL    — Apps Script Web App deployment /exec URL
  GAS_WEBAPP_TOKEN  — shared secret; must match the GAS_WEBAPP_TOKEN
                      ScriptProperty in the GAS project
  DRY_RUN           — if truthy, skip the POST (fetch + parse + log only)

Exit codes:
  0  ok (or dry-run ok)
  1  empty / no events returned (refuses to overwrite cache)
  2  fetch / parse / POST failure
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import timedelta

import httpx

# Allow `python scripts/push_ff_to_gas.py` to import from `src.*`
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.calendar import FF_URL  # noqa: E402
from src.ff_scraper import CALENDAR_URL, scrape_ff_html  # noqa: E402
from src.utils_time import iso_utc, now_ict, now_utc  # noqa: E402

log = logging.getLogger("push_ff_to_gas")


def _post_to_gas(url: str, token: str, payload: dict) -> dict:
    body = dict(payload)
    body["token"] = token
    # GAS WebApp /exec answers a POST with a 302 redirect to
    # script.googleusercontent.com that carries the real JSON body. httpx does
    # NOT follow redirects by default, so without follow_redirects the call
    # raises on the 302 and the response is never read. (Same GAS quirk that
    # bites TradingView/Telegram POSTs.)
    with httpx.Client(timeout=30.0, follow_redirects=True) as c:
        r = c.post(url, json=body)
    r.raise_for_status()
    try:
        return r.json()
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"GAS returned non-JSON ({r.status_code}): {r.text[:400]}"
        ) from e


def _next_workweek_monday_target_dates() -> tuple[str, set[str]]:
    """Return (week_start_ict_str, {YYYY-MM-DD for Mon..Fri})."""
    now = now_ict()
    days_to_mon = (7 - now.weekday()) % 7
    if days_to_mon == 0:
        days_to_mon = 7
    mon = (now + timedelta(days=days_to_mon)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    target = {(mon + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(5)}
    return mon.strftime("%Y-%m-%d"), target


def push_weekly(url: str, token: str, dry_run: bool) -> int:
    events = scrape_ff_html(CALENDAR_URL)
    log.info("scrape_ff_html: %d total events", len(events))

    week_start_ict, target_dates = _next_workweek_monday_target_dates()
    week_events = [
        e for e in events if (e.get("date") or "")[:10] in target_dates
    ]
    log.info(
        "week %s · Mon-Fri events: %d", week_start_ict, len(week_events)
    )

    by_impact: dict[str, int] = {}
    for e in week_events:
        by_impact[e["impact"]] = by_impact.get(e["impact"], 0) + 1
    log.info("by impact: %s", by_impact)

    high = [e for e in week_events if e["impact"] == "High"]
    log.info("high-impact: %d", len(high))
    for h in high[:10]:
        log.info(
            "  %s %s · %s",
            (h.get("date") or "")[:16],
            h.get("country", ""),
            h.get("title", ""),
        )

    if not week_events:
        log.error("zero Mon-Fri events parsed — refusing to push empty cache")
        return 1

    if dry_run:
        log.info("DRY_RUN — skipping POST")
        log.info("--- sample payload (first 3) ---")
        log.info(json.dumps(week_events[:3], indent=2, ensure_ascii=False))
        return 0

    payload = {
        "kind": "weekly_html",
        "week_start_ict": week_start_ict,
        "scraped_at_utc": iso_utc(now_utc()),
        "source": "ff-html-scrape",
        "source_url": CALENDAR_URL,
        "events": week_events,
    }
    resp = _post_to_gas(url, token, payload)
    log.info("GAS response: %s", resp)
    if not resp.get("ok"):
        log.error("GAS rejected payload: %s", resp)
        return 2
    return 0


def push_thisweek(url: str, token: str, dry_run: bool) -> int:
    with httpx.Client(
        timeout=20.0, headers={"User-Agent": "gold-news-pipeline/1.0"}
    ) as c:
        r = c.get(FF_URL, follow_redirects=True)
    r.raise_for_status()
    raw_json = r.text

    try:
        arr = json.loads(raw_json)
    except json.JSONDecodeError as e:
        log.error("FF returned non-JSON: %s ... head=%r", e, raw_json[:200])
        return 2

    if not isinstance(arr, list) or not arr:
        log.error(
            "FF JSON not a non-empty array (type=%s, len=%s)",
            type(arr).__name__,
            len(arr) if isinstance(arr, list) else "n/a",
        )
        return 1

    log.info("FF JSON: %d items, %d bytes", len(arr), len(raw_json))
    dates = sorted({
        (item.get("date") or "")[:10] for item in arr if item.get("date")
    })
    date_span = [dates[0], dates[-1]] if dates else []
    log.info("date span: %s", date_span)

    if dry_run:
        log.info("DRY_RUN — skipping POST")
        return 0

    payload = {
        "kind": "thisweek_json",
        "scraped_at_utc": iso_utc(now_utc()),
        "raw_json": raw_json,
        "date_span": date_span,
    }
    resp = _post_to_gas(url, token, payload)
    log.info("GAS response: %s", resp)
    if not resp.get("ok"):
        log.error("GAS rejected payload: %s", resp)
        return 2
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )

    parser = argparse.ArgumentParser(
        description="Push FF calendar data to GAS WeeklyCache WebApp"
    )
    parser.add_argument("--mode", required=True, choices=("weekly", "thisweek"))
    args = parser.parse_args(argv)

    url = os.environ.get("GAS_WEBAPP_URL", "").strip()
    token = os.environ.get("GAS_WEBAPP_TOKEN", "").strip()
    dry_run = bool(os.environ.get("DRY_RUN", "").strip())

    if dry_run:
        log.info("DRY_RUN mode active — fetch + parse only, no POST")
    if not dry_run and (not url or not token):
        log.error(
            "GAS_WEBAPP_URL and GAS_WEBAPP_TOKEN must both be set (or DRY_RUN=1)"
        )
        return 2

    if args.mode == "weekly":
        return push_weekly(url, token, dry_run)
    return push_thisweek(url, token, dry_run)


if __name__ == "__main__":
    sys.exit(main())
