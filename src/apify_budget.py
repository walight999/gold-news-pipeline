"""Apify spend pacing — fit every Apify call inside the FIXED monthly plan limit.

White's rule (2026-09-27): use the subscribed Apify limit efficiently, never
raise it. The 2026-09 incident: Truth Social (parsebird, $0.017 per call, even
when empty) burned 83% of the $29 cycle by day 22 → every call 403'd → the
@tradetongkam squawk mirror went silent for days while every workflow still
showed ✅.

This module reads Apify's own usage counter (`GET /v2/users/me/limits`, free)
at most every `refresh_min`, caches it in `source_state` row `_apify_budget`,
and turns it into a pacing level:

  ok         spend is on/under the straight-line pace for the billing cycle
  tight      spend is ahead of pace → callers stretch their intervals
  exhausted  (almost) at the limit → callers skip Apify entirely (it would 403)

The watchdog reads the cached row (no token needed) to alert on
tight/exhausted, so a budget blow-out is loud instead of silent.

Weekend: gold is shut Sat ~05:00 → Mon ~05:00 ICT, so the scrape cadence is
stretched then (`weekend_multiplier`) — the budget goes to trading hours.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

import httpx

from .utils_time import iso_utc, now_utc, parse_iso, to_ict

log = logging.getLogger("apify_budget")

STATE_KEY = "_apify_budget"
LIMITS_URL = "https://api.apify.com/v2/users/me/limits"

OK, TIGHT, EXHAUSTED = "ok", "tight", "exhausted"


def budget_level(usage: float, limit: float, cycle_start: datetime,
                 cycle_end: datetime, now: datetime, *,
                 reserve_pct: float = 5.0, slack_pct: float = 5.0) -> str:
    """Pacing level for `usage` USD spent against `limit` USD this cycle.

    Pace line = (limit − reserve) × elapsed fraction of the cycle. Spending
    more than `slack_pct` of the limit above that line ⇒ TIGHT. Within 1% of the
    limit ⇒ EXHAUSTED. No/invalid limit ⇒ OK (nothing to pace against)."""
    if limit <= 0:
        return OK
    if usage >= limit * 0.99:
        return EXHAUSTED
    span = (cycle_end - cycle_start).total_seconds()
    if span <= 0:
        return OK
    frac = min(1.0, max(0.0, (now - cycle_start).total_seconds() / span))
    pace = limit * (1 - reserve_pct / 100.0) * frac
    if usage > pace + limit * slack_pct / 100.0:
        return TIGHT
    return OK


def is_weekend_closed(now: datetime) -> bool:
    """True while spot gold is shut: Sat 05:00 ICT → Mon 05:00 ICT."""
    ict = to_ict(now)
    wd, hr = ict.weekday(), ict.hour   # Mon=0 … Sun=6
    return (wd == 5 and hr >= 5) or wd == 6 or (wd == 0 and hr < 5)


def fetch_usage(token: str, timeout: float = 15.0) -> dict[str, Any] | None:
    """Apify's own usage + limit for the current billing cycle, or None on any
    error (best-effort: a failed read keeps the last cached level). Token goes
    in the header, never the URL — this repo's Actions logs are public."""
    if not token:
        return None
    try:
        with httpx.Client(timeout=timeout) as c:
            r = c.get(LIMITS_URL, headers={"Authorization": f"Bearer {token}"})
        r.raise_for_status()
        d = r.json()["data"]
        return {
            "usage": float(d["current"]["monthlyUsageUsd"]),
            "limit": float(d["limits"]["maxMonthlyUsageUsd"]),
            "cycle_start": d["monthlyUsageCycle"]["startAt"],
            "cycle_end": d["monthlyUsageCycle"]["endAt"],
        }
    except Exception as e:  # noqa: BLE001 — pacing is advisory, never block the run
        msg = str(e).replace(token, "***")
        log.warning("apify_budget: usage read failed: %s", msg)
        return None


def read_cached(store) -> tuple[str, dict[str, Any]]:
    """(level, blob) from the cached state row; (OK, {}) when never written."""
    row = store.get("source_state", (STATE_KEY,)) or {}
    try:
        blob = json.loads(row.get("items_last_hour") or "{}")
    except (TypeError, ValueError):
        blob = {}
    return (row.get("last_status") or OK), (blob if isinstance(blob, dict) else {})


def current_level(store, token: str, cfg: dict[str, Any] | None,
                  now: datetime | None = None, fetch=fetch_usage) -> str:
    """The pacing level for this run. Refreshes from Apify when the cache is
    older than `refresh_min`; otherwise (or if the read fails) uses the cache.
    Disabled/absent config ⇒ always OK (legacy behaviour)."""
    cfg = cfg or {}
    if not cfg.get("enabled"):
        return OK
    now = now or now_utc()
    row = store.get("source_state", (STATE_KEY,)) or {}
    last = parse_iso(row.get("last_attempt_ts"))
    level, _ = read_cached(store)
    if last is not None and (now - last).total_seconds() < int(cfg.get("refresh_min", 30)) * 60:
        return level
    u = fetch(token)
    if u is None:
        return level
    start, end = parse_iso(u["cycle_start"]), parse_iso(u["cycle_end"])
    if start is None or end is None:
        return level
    new = budget_level(u["usage"], u["limit"], start, end, now,
                       reserve_pct=float(cfg.get("reserve_pct", 5)),
                       slack_pct=float(cfg.get("slack_pct", 5)))
    store.upsert("source_state", {
        "source_id": STATE_KEY,
        "last_attempt_ts": iso_utc(now),
        "last_success_ts": iso_utc(now),
        "last_status": new,
        "items_last_hour": json.dumps(u),
    })
    log.info("apify_budget: $%.2f / $%.2f this cycle (ends %s) → %s",
             u["usage"], u["limit"], u["cycle_end"][:10], new)
    return new


def interval_multiplier(level: str, cfg: dict[str, Any] | None,
                        now: datetime | None = None) -> float:
    """How much to stretch every Apify min-interval this run. Tight pace and
    the gold-closed weekend compound (e.g. 2 × 3 = 6×)."""
    cfg = cfg or {}
    if not cfg.get("enabled"):
        return 1.0
    m = 1.0
    if level == TIGHT:
        m *= float(cfg.get("tight_multiplier", 2))
    if is_weekend_closed(now or now_utc()):
        m *= float(cfg.get("weekend_multiplier", 3))
    return m
