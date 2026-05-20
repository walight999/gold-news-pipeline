"""Entry point.

Modes:
  cron              — */5m: respect each source's poll_min, normal routing.
  event             — Tier-0-only loop, 30m, sleep 60s. Triggered by dispatch.
  digest            — Build a digest if now_ict ∈ ±5m of a slot. Idempotent.
  calendar_daily    — Push today's economic calendar (06:30 ICT). Idempotent.
  calendar_check    — Check for events releasing in [15, 25) min. Push T-15 alerts.
  maintain          — Purge stale rows from event_state + sent_log. Daily.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import time as _time
from pathlib import Path
from typing import Any

import yaml

from . import calendar as cal
from . import dedup, digest, fred, health, scorer
from .fetcher import fetch_all, plan_fetch
from .line_client import LineClient
from .line_flex import (
    alert_bubble,
    alt_text_for_event,
    breaking_bubble,
    calendar_day_bubble,
    digest_carousel,
    health_bubble,
    health_recovered_bubble,
    post_release_bubble,
    pre_release_bubble,
)
from .normalizer import normalize
from .parser import parse_feed
from .router import Route, decide
from .store import Store
from .utils_time import ICT, iso_utc, now_ict, now_utc, within_digest_slot

log = logging.getLogger("gold-news")
CFG_DIR = Path(__file__).resolve().parent.parent / "config"


def _load_yaml(name: str) -> dict[str, Any]:
    with (CFG_DIR / name).open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _load_configs() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    return _load_yaml("sources.yaml"), _load_yaml("keywords.yaml"), _load_yaml("schedule.yaml")


# --------------- Single-run pipeline ---------------

async def run_once(mode: str, tier_filter: set[int] | None = None) -> int:
    src_cfg, kw_cfg, sched_cfg = _load_configs()

    store = Store.from_env()
    store.connect()
    store.load_all()

    sources = src_cfg["sources"]
    if tier_filter is not None:
        sources = [s for s in sources if int(s["tier"]) in tier_filter]

    # 1. Plan + fetch
    plan = plan_fetch(sources, store)
    log.info("plan: fetch=%d skipped_disabled=%d skipped_poll=%d",
             len(plan.sources), len(plan.skipped_disabled), len(plan.skipped_polled_recently))
    results = await fetch_all(plan, store)

    # 2. Parse + normalize
    raw_entries: list[dict[str, Any]] = []
    for r in results:
        if r.body:
            raw_entries.extend(parse_feed(r.body, r.source))
            # update last_item_ts based on most recent published entry
            state = store.get("source_state", (r.source["id"],)) or {}
            most_recent = max((e.get("published_ts") for e in raw_entries if e.get("source_id") == r.source["id"] and e.get("published_ts")), default=None)
            if most_recent:
                state["last_item_ts"] = iso_utc(most_recent)
                state["source_id"] = r.source["id"]
                store.upsert("source_state", state)
            health.mark_validated(store, r.source["id"])
    items = normalize(raw_entries)
    log.info("items normalized: %d (from %d entries)", len(items), len(raw_entries))

    # 3. Cluster + score
    events = dedup.cluster(items, kw_cfg)
    scores: dict[str, float] = {ev.event_id: scorer.score_event(ev, kw_cfg) for ev in events}
    log.info("clustered events: %d", len(events))

    # 4. Route
    rl = sched_cfg.get("rate_limit", {})
    routing_cfg = sched_cfg.get("routing", {})
    decisions = decide(
        events, scores, store,
        rate_limit_window_min=int(rl.get("breaking_alert_window_minutes", 15)),
        rate_limit_max=int(rl.get("breaking_alert_max", 5)),
        require_breaking_confirmation=bool(routing_cfg.get("breaking_require_confirmation", False)),
    )

    # 5. Send + persist
    line = None
    breaking_alert_decisions = [d for d in decisions if d.route in (Route.BREAKING, Route.ALERT)]
    if breaking_alert_decisions:
        line = LineClient.from_env()
    news_target = os.environ.get("LINE_NEWS_TARGET", "")
    health_target = os.environ.get("LINE_HEALTH_TARGET", "")

    for d in decisions:
        ev = d.event
        store.upsert("event_state", dedup.serialize_event_for_store(ev, d.score, d.route.value))
        # calibration log: every event with score >= 2
        if d.score >= 2.0:
            cal = dedup.serialize_event_for_store(ev, d.score, d.route.value)
            store.upsert("calibration_log", {
                **cal,
                "routed_as": d.route.value,
                "xau_return_5m": "",
                "xau_return_15m": "",
                "xau_return_30m": "",
            })
        if d.route in (Route.BREAKING, Route.ALERT) and line and news_target:
            # idempotency
            existing = store.get("sent_log", (ev.event_id, d.route.value))
            if existing:
                continue
            if d.route == Route.BREAKING:
                bubble = breaking_bubble(ev, d.score, kw_cfg)
                alt = alt_text_for_event("⚡ BREAKING", ev, d.score)
            else:
                bubble = alert_bubble(ev, d.score, kw_cfg)
                alt = alt_text_for_event("🔔 ALERT", ev, d.score)
            resp = line.push_flex(news_target, alt, bubble)
            if resp["status"] == 200:
                store.upsert("sent_log", {
                    "event_id": ev.event_id,
                    "route_type": d.route.value,
                    "sent_ts": iso_utc(now_utc()),
                    "line_status": resp["status"],
                })
            else:
                log.warning("LINE push failed event=%s status=%s — not marking sent", ev.event_id, resp["status"])

    # 6. Health pass — first detect current warnings, then resolve any
    # open warnings whose triggering condition is no longer true, then push.
    health_cfg = sched_cfg.get("health", {})
    is_event_day = (mode == "event")
    current_warnings: list[tuple[str, str]] = []
    sources_checked: set[str] = set()
    for s in src_cfg["sources"]:
        if not s.get("enabled"):
            continue
        sources_checked.add(s["id"])
        current_warnings.extend(
            health.check_source_health(store, s, health_cfg, is_event_day=is_event_day)
        )
    current_set = set(current_warnings)

    # Recoveries: warnings open in store but no longer in current_set.
    recovered: list[tuple[str, str]] = []
    for row in list(store.all_rows("health_log")):
        if row.get("resolved_ts"):
            continue
        sid = row.get("source_id")
        wtype = row.get("warning_type")
        if sid not in sources_checked:
            continue
        if (sid, wtype) not in current_set:
            if health.resolve_warning(store, sid, wtype) > 0:
                recovered.append((sid, wtype))

    # Push new warnings (gated by raise_warning cooldown)
    if current_warnings and health_target:
        line = line or LineClient.from_env()
        cooldown = int(health_cfg.get("alert_cooldown_minutes", 60))
        emitted_warnings: list[tuple[str, str]] = []
        for sid, wtype in current_warnings:
            if health.raise_warning(store, sid, wtype, cooldown):
                emitted_warnings.append((sid, wtype))
        if emitted_warnings:
            bubble = health_bubble(emitted_warnings)
            alt = f"⚠️ Health Check — {len(emitted_warnings)} warning(s)"
            line.push_flex(health_target, alt, bubble)

    # Push recoveries
    if recovered and health_target:
        line = line or LineClient.from_env()
        bubble = health_recovered_bubble(recovered)
        alt = f"✅ Health Recovered — {len(recovered)} item(s)"
        line.push_flex(health_target, alt, bubble)

    # 7. Digest if in slot
    if mode in ("cron", "digest"):
        slots_ict = sched_cfg["digest"]["slots_ict"]
        window = int(sched_cfg["digest"]["window_minutes"])
        slot = within_digest_slot(slots_ict, window)
        if slot and not digest.already_sent(store, slot):
            digest_events = [d.event for d in decisions if d.route == Route.DIGEST]
            max_events = int(sched_cfg["digest"].get("max_events", 10))
            ranked = sorted(digest_events, key=lambda e: -scores.get(e.event_id, 0))[:max_events]
            carousel = digest_carousel(ranked, scores, slot, kw_cfg)
            if carousel and news_target:
                line = line or LineClient.from_env()
                alt = f"📰 Digest {slot} ICT — {len(ranked)} event(s)"
                resp = line.push_flex(news_target, alt, carousel)
                digest.mark_sent(store, slot, resp["status"])

    # 8. Flush state
    store.flush()
    log.info("done. sheets API calls=%d", store.api_calls)
    return 0


# --------------- Event-mode loop ---------------

async def run_maintain() -> int:
    """Purge stale rows so the Sheet doesn't grow unbounded.

    Retention defaults:
        event_state:     7 days   (keep last week for context)
        sent_log:        30 days  (keep idempotency window long enough that
                                   the same dispatch can't slip past twice)
        calibration_log: kept forever — Phase 3 will backfill xau_return_* here.
        source_state, health_log: kept (small + per-source, not row-per-event).
    """
    _, _, sched_cfg = _load_configs()
    retention = sched_cfg.get("retention", {}) or {}
    days_event_state = int(retention.get("event_state_days", 7))
    days_sent_log    = int(retention.get("sent_log_days", 30))

    store = Store.from_env()
    store.connect()
    store.load_all()

    removed_es = store.purge_older_than("event_state", days_event_state, ts_col="last_seen_ts")
    removed_sl = store.purge_older_than("sent_log",    days_sent_log,    ts_col="sent_ts")
    store.flush()

    log.info("maintain done: event_state purged=%d, sent_log purged=%d, api_calls=%d",
             removed_es, removed_sl, store.api_calls)
    return 0


async def run_calendar_daily() -> int:
    """Push today's economic calendar to LINE_NEWS_TARGET. Idempotent per ICT day."""
    _, _, sched_cfg = _load_configs()
    cal_cfg = sched_cfg.get("calendar", {})

    store = Store.from_env()
    store.connect()
    store.load_all()

    today_key = now_ict().strftime("%Y-%m-%d")
    sent_key = f"cal_daily:{today_key}"
    if store.get("sent_log", (sent_key, "calendar_daily")):
        log.info("calendar_daily already sent for %s — skipping", today_key)
        store.flush()
        return 0

    try:
        events = cal.fetch_calendar(cal_cfg.get("source_url", cal.FF_URL))
    except Exception as e:
        log.exception("fetch_calendar failed: %s", e)
        store.flush()
        return 1

    today = cal.filter_today_ict(events)
    countries = tuple(cal_cfg.get("daily_currencies", cal.DEFAULT_DAILY_COUNTRIES))
    impacts   = tuple(cal_cfg.get("daily_impacts",    cal.DEFAULT_DAILY_IMPACTS))
    filtered = cal.filter_by_impact(cal.filter_by_country(today, countries), impacts)
    log.info("calendar_daily: %d total, %d today, %d after filter", len(events), len(today), len(filtered))

    if not filtered:
        store.flush()
        return 0

    target = os.environ.get("LINE_NEWS_TARGET", "")
    if not target:
        log.warning("LINE_NEWS_TARGET not set — skipping push")
        store.flush()
        return 0

    date_label = now_ict().strftime("%a %d %b %Y")
    bubble = calendar_day_bubble(filtered, date_label)
    if bubble is None:
        store.flush()
        return 0
    line = LineClient.from_env()
    resp = line.push_flex(target, f"📅 Calendar — {len(filtered)} events today", bubble)
    if resp["status"] == 200:
        store.upsert("sent_log", {
            "event_id": sent_key, "route_type": "calendar_daily",
            "sent_ts": iso_utc(now_utc()), "line_status": 200,
        })
    store.flush()
    return 0


async def run_calendar_check() -> int:
    """Single sweep that does BOTH pre-release and post-release alerts.

    Pre-release: events in [pre_lo, pre_hi) minutes ahead.
    Post-release: events released in [post_lo, post_hi) minutes — currently
    (-15, 0]. Both gated by sent_log idempotency per event_id.
    """
    _, _, sched_cfg = _load_configs()
    cal_cfg = sched_cfg.get("calendar", {})

    store = Store.from_env()
    store.connect()
    store.load_all()

    try:
        events = cal.fetch_calendar(cal_cfg.get("source_url", cal.FF_URL))
    except Exception as e:
        log.exception("fetch_calendar failed: %s", e)
        store.flush()
        return 1

    countries = tuple(cal_cfg.get("pre_release_currencies", cal.DEFAULT_PRE_COUNTRIES))
    impacts   = tuple(cal_cfg.get("pre_release_impacts",    cal.DEFAULT_PRE_IMPACTS))
    pre_lo = int(cal_cfg.get("pre_release_window_low_min",  cal.DEFAULT_PRE_WINDOW_LOW))
    pre_hi = int(cal_cfg.get("pre_release_window_high_min", cal.DEFAULT_PRE_WINDOW_HIGH))
    post_lo = int(cal_cfg.get("post_release_window_low_min",  -15))
    post_hi = int(cal_cfg.get("post_release_window_high_min", 0))

    relevant = cal.filter_by_impact(cal.filter_by_country(events, countries), impacts)

    upcoming = cal.filter_upcoming(relevant, pre_lo, pre_hi)
    just_released = cal.filter_upcoming(relevant, post_lo, post_hi)
    log.info("calendar_check: pre=%d in [%d,%d) min, post=%d in [%d,%d) min",
             len(upcoming), pre_lo, pre_hi, len(just_released), post_lo, post_hi)

    target = os.environ.get("LINE_NEWS_TARGET", "")
    if not target:
        log.warning("LINE_NEWS_TARGET not set — skipping push")
        store.flush()
        return 0

    pre_pushed = 0
    post_pushed = 0
    if upcoming or just_released:
        line = LineClient.from_env()

        # Pre-release alerts
        for ev in upcoming:
            sent_key = f"precal:{ev.event_id}"
            if store.get("sent_log", (sent_key, "calendar_pre")):
                continue
            mins_to = cal.minutes_until(ev)
            impact_info = cal.gold_impact_directional(ev)
            bubble = pre_release_bubble(ev, mins_to, impact_info)
            alt = f"⏰ T-{mins_to}min · {ev.country} {ev.title}"
            resp = line.push_flex(target, alt, bubble)
            if resp["status"] == 200:
                store.upsert("sent_log", {
                    "event_id": sent_key, "route_type": "calendar_pre",
                    "sent_ts": iso_utc(now_utc()), "line_status": 200,
                })
                pre_pushed += 1

        # Post-release alerts. If FRED_API_KEY is set + event title maps to a
        # supported series, upgrade to actual + surprise + final verdict; else
        # fall back to directional-only.
        fred_key = fred.fred_api_key()
        for ev in just_released:
            sent_key = f"postcal:{ev.event_id}"
            if store.get("sent_log", (sent_key, "calendar_post")):
                continue
            impact_info = cal.gold_impact_directional(ev)
            actual_text = surprise = verdict = None
            if fred_key:
                result = fred.fetch_actual(ev.title, fred_key)
                if result:
                    actual_text = result.actual_text
                    forecast_val = fred.parse_forecast_value(ev.forecast)
                    if forecast_val is not None:
                        surprise = fred.compute_surprise_label(result.actual_value, forecast_val)
                        verdict = fred.reconcile_with_impact(surprise, impact_info)
                    else:
                        verdict = None
            bubble = post_release_bubble(ev, impact_info,
                                         actual_text=actual_text,
                                         surprise=surprise, verdict=verdict)
            alt_extra = f" · actual {actual_text}" if actual_text else ""
            alt = f"📊 Released · {ev.country} {ev.title}{alt_extra}"
            resp = line.push_flex(target, alt, bubble)
            if resp["status"] == 200:
                store.upsert("sent_log", {
                    "event_id": sent_key, "route_type": "calendar_post",
                    "sent_ts": iso_utc(now_utc()), "line_status": 200,
                })
                post_pushed += 1

    log.info("calendar_check pushes: pre=%d post=%d", pre_pushed, post_pushed)
    store.flush()
    return 0


async def run_event_mode(duration_min: int = 30, sleep_sec: int = 60) -> int:
    deadline = _time.time() + duration_min * 60
    iteration = 0
    while _time.time() < deadline:
        iteration += 1
        log.info("event-mode iteration #%d", iteration)
        try:
            await run_once(mode="event", tier_filter={0})
        except (RuntimeError, ValueError, OSError) as e:
            log.exception("event-mode iteration failed: %s", e)
        await asyncio.sleep(sleep_sec)
    return 0


# --------------- CLI ---------------

def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    )
    p = argparse.ArgumentParser()
    p.add_argument("--mode", choices=(
        "cron", "event", "digest", "calendar_daily", "calendar_check", "maintain",
    ), default="cron")
    p.add_argument("--event-duration-min", type=int, default=30)
    p.add_argument("--event-sleep-sec", type=int, default=60)
    args = p.parse_args(argv)
    if args.mode == "event":
        return asyncio.run(run_event_mode(args.event_duration_min, args.event_sleep_sec))
    if args.mode == "calendar_daily":
        return asyncio.run(run_calendar_daily())
    if args.mode == "calendar_check":
        return asyncio.run(run_calendar_check())
    if args.mode == "maintain":
        return asyncio.run(run_maintain())
    return asyncio.run(run_once(mode=args.mode))


if __name__ == "__main__":
    sys.exit(main())
