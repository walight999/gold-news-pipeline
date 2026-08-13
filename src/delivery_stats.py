"""Roll `sent_log` up into a permanent per-day delivery record before it's purged.

`sent_log` is an idempotency ledger on a 30-day retention, so "how much did we
actually send, and did it land?" evaporates a month after the fact. That is the
one question the months of accumulated delivery data were supposed to answer.
`calibration_log` keeps 180 days but records the ROUTING DECISION (`routed_as`),
not the delivery — a card the quota gate shed still reads `alert` there.

This module turns the ledger into one bounded row per ICT day (~365/year) so the
history survives the purge. Pure functions; the caller owns store I/O.
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from .utils_time import ICT, parse_iso

# LINE returns 200 on a delivered push. Anything else (429 quota, 401 token,
# 0 = never attempted) is a miss worth counting separately — the 2026-07 quota
# exhaustion and the 2026-07-18 silent week would both show up here as a day
# where n_sent held steady but n_failed spiked.
_DELIVERED_STATUS = {"200", "200.0"}


def ict_day(ts: str | datetime | None) -> str | None:
    """The ICT calendar day (YYYY-MM-DD) a UTC timestamp falls on, or None."""
    dt = parse_iso(ts) if not isinstance(ts, datetime) else ts
    if dt is None:
        return None
    return dt.astimezone(ICT).strftime("%Y-%m-%d")


def _day_start_utc(day: str) -> datetime:
    """00:00 ICT on `day`, as UTC."""
    return datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=ICT).astimezone(timezone.utc)


def aggregate(rows: list[dict[str, Any]], cutoff: datetime) -> list[dict[str, Any]]:
    """One `delivery_daily` row per ICT day represented in `rows`.

    `cutoff` is the purge boundary (rows older than it are about to be dropped).
    Days that straddle it are SKIPPED, because their surviving rows are only a
    fraction of what that day really sent and writing that fraction would
    overwrite the complete figure a previous run already recorded. Nothing is
    lost by skipping: every day is fully inside the window on the runs before
    it ages out.
    """
    by_day: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    totals: dict[str, list[int]] = defaultdict(lambda: [0, 0])  # [sent, failed]

    for r in rows:
        day = ict_day(r.get("sent_ts"))
        if day is None:
            continue
        route = str(r.get("route_type") or "unknown")
        by_day[day][route] += 1
        status = str(r.get("line_status") or "").strip()
        idx = 0 if status in _DELIVERED_STATUS else 1
        totals[day][idx] += 1

    out: list[dict[str, Any]] = []
    for day in sorted(by_day):
        # Skip the boundary day and anything older — partial data.
        if _day_start_utc(day) < cutoff:
            continue
        sent, failed = totals[day]
        out.append({
            "date_ict": day,
            "n_sent": sent,
            "n_failed": failed,
            # JSON blob rather than one column per route: route types come and
            # go (calendar_pre, scorecard, weekly…) and a blob means adding one
            # never needs a schema migration on a tab that already has history.
            "by_route": json.dumps(dict(sorted(by_day[day].items())),
                                   ensure_ascii=False),
        })
    return out


def summarize(rows: list[dict[str, Any]], days: int = 7,
              today: datetime | None = None) -> dict[str, Any]:
    """Rolling totals over the last `days` ICT days of `delivery_daily`.

    Used by the audit/report path; kept here so the aggregation semantics live
    in one place."""
    if today is None:
        raise ValueError("today is required — callers pass now_utc() so this "
                         "stays testable and free of hidden clock reads")
    first = (today.astimezone(ICT) - timedelta(days=days - 1)).strftime("%Y-%m-%d")
    window = [r for r in rows if str(r.get("date_ict") or "") >= first]
    sent = failed = 0
    per_route: dict[str, int] = defaultdict(int)
    for r in window:
        sent += int(r.get("n_sent") or 0)
        failed += int(r.get("n_failed") or 0)
        try:
            for k, v in json.loads(r.get("by_route") or "{}").items():
                per_route[k] += int(v)
        except (ValueError, TypeError):
            continue   # a hand-edited cell must not break the report
    return {"days": len(window), "n_sent": sent, "n_failed": failed,
            "by_route": dict(sorted(per_route.items()))}
