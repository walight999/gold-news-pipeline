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
from . import dedup, digest, fred, health, price_feed, scorer, translator
from .fetcher import fetch_all, plan_fetch
from .line_client import LineClient
from .line_flex import (
    alert_bubble,
    alt_text_for_event,
    breaking_bubble,
    calendar_day_bubble,
    digest_carousel,
    eod_recap_bubble,
    health_bubble,
    health_recovered_bubble,
    post_release_bubble,
    pre_release_bubble,
    weekly_preview_bubble,
)
from .normalizer import normalize
from .parser import parse_feed
from .router import Route, decide
from .store import Store
from .utils_time import (
    ICT,
    is_quiet_hours_ict,
    is_weekend_ict,
    iso_utc,
    now_ict,
    now_utc,
    within_digest_slot,
)


def _quiet_hours_cfg(sched_cfg):
    return sched_cfg.get("quiet_hours") or {}


def _push_or_skip(line, target, alt, bubble, sched_cfg, label="", bypass_quiet=False):
    """LINE push wrapped in the quiet-hours gate. Returns the response dict
    on actual push, or a synthetic {status: 0, body: 'quiet_hours'} when
    suppressed so callers can keep idempotency logic clean.

    `bypass_quiet=True` skips the quiet-hours check — used for the daily
    calendar briefing at 04:40 ICT, which should fire even though it sits
    inside the 04:00-05:00 ICT market-close window."""
    if not bypass_quiet and is_quiet_hours_ict(_quiet_hours_cfg(sched_cfg)):
        log.info("quiet hours — suppressing push (%s)", label or "")
        return {"status": 0, "body": "quiet_hours"}
    return line.push_flex(target, alt, bubble)

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

    # Skip ICT weekends. Forex/gold markets are closed Sat 04:00 ICT to
    # Mon 04:00 ICT — no point burning Sheets writes / cron minutes.
    if is_weekend_ict() and mode != "event":
        log.info("weekend (ICT) — skipping %s run", mode)
        return 0

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
            # Translate title + summary to Thai inline so the bubble carries
            # full context without the user clicking through. Falls back to
            # English if Google Translate hiccups.
            title_th   = translator.to_thai(ev.representative_title, 200)
            summary_th = translator.to_thai(ev.representative_summary, 600)
            if d.route == Route.BREAKING:
                bubble = breaking_bubble(ev, d.score, kw_cfg,
                                          title_th=title_th, summary_th=summary_th)
                alt = alt_text_for_event("⚡ BREAKING", ev, d.score)
            else:
                bubble = alert_bubble(ev, d.score, kw_cfg,
                                       title_th=title_th, summary_th=summary_th)
                alt = alt_text_for_event("🔔 ALERT", ev, d.score)
            resp = _push_or_skip(line, news_target, alt, bubble, sched_cfg, label=d.route.value)
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
    # Only PUSH the recovery if the warning was actually open >= 30 min —
    # quick oscillations (cron jitter, source briefly idle) resolve silently.
    MIN_OPEN_MIN_FOR_RECOVERY_PUSH = 30
    recovered: list[tuple[str, str]] = []
    for row in list(store.all_rows("health_log")):
        if row.get("resolved_ts"):
            continue
        sid = row.get("source_id")
        wtype = row.get("warning_type")
        if sid not in sources_checked:
            continue
        if (sid, wtype) in current_set:
            continue
        open_min = health.warning_open_minutes(store, sid, wtype)
        if health.resolve_warning(store, sid, wtype) > 0 and open_min >= MIN_OPEN_MIN_FOR_RECOVERY_PUSH:
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
            _push_or_skip(line, health_target, alt, bubble, sched_cfg, label="health")

    # Push recoveries
    if recovered and health_target:
        line = line or LineClient.from_env()
        bubble = health_recovered_bubble(recovered)
        alt = f"✅ Health Recovered — {len(recovered)} item(s)"
        _push_or_skip(line, health_target, alt, bubble, sched_cfg, label="health_recovered")

    # 7. Digest if in slot
    if mode in ("cron", "digest"):
        slots_ict = sched_cfg["digest"]["slots_ict"]
        window = int(sched_cfg["digest"]["window_minutes"])
        slot = within_digest_slot(slots_ict, window)
        if slot and not digest.already_sent(store, slot):
            digest_floor = float(sched_cfg["digest"].get("min_score", 0.5))
            max_events = int(sched_cfg["digest"].get("max_events", 10))
            non_breaking = [d.event for d in decisions
                            if d.route not in (Route.BREAKING, Route.ALERT)]
            digest_events = [
                e for e in non_breaking
                if scores.get(e.event_id, 0) >= digest_floor
            ]
            # Fallback: if even with the relaxed floor the filtered pool
            # is empty (very slow hour), still surface the top events
            # ordered by score. Guarantees the slot bubble always has
            # something to look at.
            if not digest_events and non_breaking:
                digest_events = non_breaking
            ranked = sorted(digest_events, key=lambda e: -scores.get(e.event_id, 0))[:max_events]
            # Translate title + summary to Thai for each event going to the
            # bubble. Faithful translation (Google Translate) — not AI rewriting.
            translations: dict[str, dict[str, str | None]] = {}
            for ev in ranked:
                translations[ev.event_id] = {
                    "title_th":   translator.to_thai(ev.representative_title, 200),
                    "summary_th": translator.to_thai(ev.representative_summary, 400),
                }
            carousel = digest_carousel(ranked, scores, slot, kw_cfg, translations=translations)
            if carousel and news_target:
                line = line or LineClient.from_env()
                alt = f"📰 Digest {slot} ICT — {len(ranked)} event(s)"
                resp = _push_or_skip(line, news_target, alt, carousel, sched_cfg, label="digest")
                digest.mark_sent(store, slot, resp["status"])

    # 8. Heartbeat — stamp pipeline liveness before flush so the watchdog
    # can distinguish "cron stopped" from "cron ran but no news today".
    if mode in ("cron", "event"):
        health.write_heartbeat(store, items_seen=len(items))

    # 9. Flush state
    store.flush()
    log.info("done. sheets API calls=%d", store.api_calls)
    return 0


# --------------- Event-mode loop ---------------

async def run_verify_sources() -> int:
    """Weekly probe — checks every enabled source URL + FF JSON + FF HTML
    parser. Pushes a health alert if anything broke (and the failure
    isn't already in health_log within cooldown)."""
    import asyncio as _aio
    import httpx
    src_cfg, _, sched_cfg = _load_configs()
    sources = [s for s in src_cfg.get("sources", []) if s.get("enabled")]

    store = Store.from_env()
    store.connect()
    store.load_all()
    failures: list[tuple[str, str]] = []

    async def _probe(s):
        url = s.get("url")
        if not url:
            return s["id"], "no_url"
        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=True,
                                          headers={"User-Agent": "gold-news-pipeline/verify"}) as c:
                r = await c.get(url)
            if r.status_code >= 400:
                return s["id"], f"http_{r.status_code}"
            return s["id"], "ok"
        except Exception as e:
            return s["id"], f"err_{type(e).__name__}"

    results = await _aio.gather(*[_probe(s) for s in sources])
    for sid, status in results:
        if status != "ok":
            failures.append((sid, f"verify_{status}"))

    # FF JSON
    try:
        events = cal.fetch_calendar()
        if not events:
            failures.append(("forexfactory_json", "verify_empty"))
    except Exception as e:
        failures.append(("forexfactory_json", f"verify_err_{type(e).__name__}"))

    # FF HTML scraper
    try:
        from . import ff_scraper
        scraped = ff_scraper.scrape_ff_html()
        if not scraped:
            failures.append(("forexfactory_html", "verify_scrape_empty"))
    except Exception as e:
        failures.append(("forexfactory_html", f"verify_scrape_err_{type(e).__name__}"))

    log.info("verify_sources: %d sources checked, %d failures",
             len(sources) + 2, len(failures))

    health_target = os.environ.get("LINE_HEALTH_TARGET", "")
    if failures and health_target:
        cooldown = int(sched_cfg.get("health", {}).get("alert_cooldown_minutes", 60))
        emitted: list[tuple[str, str]] = []
        for sid, wtype in failures:
            if health.raise_warning(store, sid, wtype, cooldown):
                emitted.append((sid, wtype))
        if emitted:
            line = LineClient.from_env()
            bubble = health_bubble(emitted)
            alt = f"⚠️ Verify — {len(emitted)} source issue(s)"
            _push_or_skip(line, health_target, alt, bubble, sched_cfg, label="verify")
    store.flush()
    return 0


async def run_eod_recap() -> int:
    """End-of-day recap @ 23:00 ICT. Idempotent per ICT date."""
    if is_weekend_ict():
        log.info("weekend (ICT) — skipping eod_recap")
        return 0
    _, _, sched_cfg = _load_configs()
    store = Store.from_env()
    store.connect()
    store.load_all()

    from datetime import timedelta
    today_ict = now_ict().strftime("%Y-%m-%d")
    sent_key = f"eod:{today_ict}"
    if store.get("sent_log", (sent_key, "eod_recap")):
        log.info("eod_recap already sent for %s — skipping", today_ict)
        store.flush()
        return 0

    # Count today's pushes from sent_log
    today_start = now_utc().replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(hours=7)
    breaking_n = alert_n = cal_pre_n = cal_post_n = 0
    from .utils_time import parse_iso
    for row in store.all_rows("sent_log"):
        ts = parse_iso(row.get("sent_ts"))
        if not ts or ts < today_start:
            continue
        rt = row.get("route_type", "")
        if rt == "breaking": breaking_n += 1
        elif rt == "alert": alert_n += 1
        elif rt == "calendar_pre": cal_pre_n += 1
        elif rt == "calendar_post": cal_post_n += 1

    # Top topics from today's event_state (score >= 1.5)
    topic_stats: dict[str, list[float]] = {}
    for row in store.all_rows("event_state"):
        ts = parse_iso(row.get("first_seen_ts"))
        if not ts or ts < today_start:
            continue
        sc = float(row.get("score") or 0)
        if sc < 1.5:
            continue
        topic = row.get("topic_bucket", "other")
        topic_stats.setdefault(topic, []).append(sc)

    top_topics = [
        (topic, len(scores), max(scores))
        for topic, scores in topic_stats.items()
    ]
    top_topics.sort(key=lambda x: (-x[2], -x[1]))
    digest_events_n = sum(len(s) for s in topic_stats.values())

    stats = {
        "breaking_n": breaking_n,
        "alert_n": alert_n,
        "digest_events_n": digest_events_n,
        "calendar_pre_n": cal_pre_n,
        "calendar_post_n": cal_post_n,
        "top_topics": top_topics,
    }
    target = os.environ.get("LINE_NEWS_TARGET", "")
    if not target:
        log.warning("LINE_NEWS_TARGET not set — skipping eod_recap push")
        store.flush()
        return 0
    line = LineClient.from_env()
    ict = now_ict()
    short_date = f"{ict.day}/{ict.month}/{ict.year % 100}"
    bubble = eod_recap_bubble(stats, short_date)
    alt = (f"🌙 EoD — {breaking_n} breaking, {alert_n} alert, "
           f"{cal_pre_n}+{cal_post_n} calendar")
    resp = _push_or_skip(line, target, alt, bubble, sched_cfg, label="eod_recap")
    if resp["status"] == 200:
        store.upsert("sent_log", {
            "event_id": sent_key, "route_type": "eod_recap",
            "sent_ts": iso_utc(now_utc()), "line_status": 200,
        })
    store.flush()
    log.info("eod_recap done: %s", stats)
    return 0


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


async def run_weekly_preview() -> int:
    """Saturday-morning preview of next week's economic releases.

    One Flex bubble per ISO week, sectioned by day, with per-event
    forecast-vs-previous Effect emoji. Idempotent per ISO week via sent_log.
    """
    _, _, sched_cfg = _load_configs()
    cal_cfg = sched_cfg.get("calendar", {})

    store = Store.from_env()
    store.connect()
    store.load_all()

    # Idempotency: key on the TARGET Monday so Sat/Sun/Mon retries all
    # collapse to a single send once FF JSON has the next-week data.
    target_mon = cal.next_workweek_monday_ict().strftime("%Y-%m-%d")
    sent_key = f"weekly:{target_mon}"
    if store.get("sent_log", (sent_key, "weekly_preview")):
        log.info("weekly_preview already sent for week-of %s — skipping", target_mon)
        store.flush()
        return 0

    try:
        events = cal.fetch_calendar(cal_cfg.get("source_url", cal.FF_URL))
    except Exception as e:
        log.exception("fetch_calendar failed: %s", e)
        store.flush()
        return 1

    next_week = cal.filter_next_week_ict(events)

    # FF JSON only ships ISO-week-current; Sat-morning runs find nothing.
    # Fall back to HTML scrape (curl_cffi bypasses Cloudflare) so the
    # Saturday preview actually works.
    if not next_week:
        log.info("FF JSON has no next-week data — trying HTML scrape fallback")
        try:
            from . import ff_scraper
            scraped_raw = ff_scraper.scrape_ff_html()
            if scraped_raw:
                scraped_events = cal.parse_ff_payload(scraped_raw)
                next_week = cal.filter_next_week_ict(scraped_events)
                log.info("HTML scrape yielded %d events, %d in next week",
                         len(scraped_events), len(next_week))
        except Exception as e:
            log.warning("FF HTML scrape failed: %s", e)

    countries = tuple(cal_cfg.get("daily_currencies", cal.DEFAULT_DAILY_COUNTRIES))
    impacts   = tuple(cal_cfg.get("daily_impacts",    cal.DEFAULT_DAILY_IMPACTS))
    filtered = cal.filter_by_impact(cal.filter_by_country(next_week, countries), impacts)
    log.info("weekly_preview: %d total, %d in next week, %d after filter",
             len(events), len(next_week), len(filtered))

    if not filtered:
        store.flush()
        return 0

    effects = {ev.event_id: cal.forecast_vs_previous_effect(ev) for ev in filtered}
    from datetime import timedelta
    start = min(e.dt_ict for e in filtered)
    end = max(e.dt_ict for e in filtered)
    week_label = f"{start.strftime('%a %d %b')} – {end.strftime('%a %d %b')}"

    bubble = weekly_preview_bubble(filtered, effects, week_label)
    if bubble is None:
        store.flush()
        return 0
    target = os.environ.get("LINE_NEWS_TARGET", "")
    if not target:
        log.warning("LINE_NEWS_TARGET not set — skipping push")
        store.flush()
        return 0
    line = LineClient.from_env()
    resp = _push_or_skip(line, target, f"📅 Week Ahead — {len(filtered)} events",
                          bubble, sched_cfg, label="weekly_preview")
    if resp["status"] == 200:
        store.upsert("sent_log", {
            "event_id": sent_key, "route_type": "weekly_preview",
            "sent_ts": iso_utc(now_utc()), "line_status": 200,
        })
    store.flush()
    return 0


async def run_calendar_daily() -> int:
    """Push today's economic calendar to LINE_NEWS_TARGET.

    Two pushes per ICT day, idempotent per slot:
      `early` at 00:05 ICT — start-of-day briefing. Catches events that
                              fire between 00:00 and the 04:40 push that
                              would otherwise have only ~minutes warning
                              from calendar_check.
      `main`  at 04:40 ICT — pre-session briefing (the existing one).
                              Sits inside the 04:00-05:00 quiet window
                              but bypasses it.
    Slot is detected from the ICT hour so the same code handles both."""
    if is_weekend_ict():
        log.info("weekend (ICT) — skipping calendar_daily")
        return 0
    _, _, sched_cfg = _load_configs()
    cal_cfg = sched_cfg.get("calendar", {})

    store = Store.from_env()
    store.connect()
    store.load_all()

    hour_ict = now_ict().hour
    slot = "early" if hour_ict < 4 else "main"
    today_key = now_ict().strftime("%Y-%m-%d")
    sent_key = f"cal_daily:{today_key}:{slot}"
    if store.get("sent_log", (sent_key, "calendar_daily")):
        log.info("calendar_daily (%s) already sent for %s — skipping", slot, today_key)
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
    # Market pulse — yfinance call wrapped in try (skip on failure).
    xau_snap = price_feed.get_xau_snapshot()
    dxy_snap = price_feed.get_dxy_snapshot()
    xau_tuple = (xau_snap.last, xau_snap.pct_change_day) if xau_snap else None
    dxy_tuple = (dxy_snap.last, dxy_snap.pct_change_day) if dxy_snap else None
    bubble = calendar_day_bubble(filtered, date_label, xau_tuple, dxy_tuple)
    if bubble is None:
        store.flush()
        return 0
    line = LineClient.from_env()
    # Daily briefing is the ONE push that's allowed through the
    # 04:00-05:00 ICT quiet window (it's the wake-up email of the day).
    resp = _push_or_skip(line, target, f"📅 Calendar — {len(filtered)} events today",
                          bubble, sched_cfg, label="calendar_daily",
                          bypass_quiet=True)
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
    if is_weekend_ict():
        log.info("weekend (ICT) — skipping calendar_check")
        return 0
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
            effect_info = cal.forecast_vs_previous_effect(ev)
            bubble = pre_release_bubble(ev, mins_to, impact_info, effect_info)
            alt = f"⏰ T-{mins_to}min · {ev.country} {ev.title}"
            resp = _push_or_skip(line, target, alt, bubble, sched_cfg, label="calendar")
            if resp["status"] == 200:
                store.upsert("sent_log", {
                    "event_id": sent_key, "route_type": "calendar_pre",
                    "sent_ts": iso_utc(now_utc()), "line_status": 200,
                })
                pre_pushed += 1

        # Post-release alerts. Priority order for actual values:
        #   1. FRED — fast, reliable for US series (CPI/NFP/PCE/etc.)
        #   2. FF HTML actuals — covers everything else (PMI, IFO, BoC, etc.)
        #   3. Directional-only when both fail.
        fred_key = fred.fred_api_key()
        ff_actuals_cache: dict[str, str] | None = None   # lazy, once per run
        for ev in just_released:
            sent_key = f"postcal:{ev.event_id}"
            if store.get("sent_log", (sent_key, "calendar_post")):
                continue
            impact_info = cal.gold_impact_directional(ev)
            actual_text = surprise = verdict = None
            actual_value: float | None = None
            if fred_key:
                result = fred.fetch_actual(ev.title, fred_key)
                if result:
                    actual_text = result.actual_text
                    actual_value = result.actual_value
            # FF HTML actuals fallback for non-FRED events
            if actual_text is None:
                if ff_actuals_cache is None:
                    try:
                        from . import ff_scraper
                        ff_actuals_cache = ff_scraper.scrape_current_week_actuals()
                    except Exception as e:
                        log.warning("FF actuals scrape failed: %s", e)
                        ff_actuals_cache = {}
                from . import ff_scraper
                ff_actual = ff_scraper.lookup_actual_for_event(ev, ff_actuals_cache)
                if ff_actual:
                    actual_text = ff_actual
                    actual_value = fred.parse_forecast_value(ff_actual)
            if actual_text is not None and actual_value is not None:
                forecast_val = fred.parse_forecast_value(ev.forecast)
                if forecast_val is not None:
                    surprise = fred.compute_surprise_label(actual_value, forecast_val)
                    verdict = fred.reconcile_with_impact(surprise, impact_info)
            # XAU reaction since release (Phase 3 — when intraday data is
            # available; off-hours / 429s gracefully return None).
            xau_reaction = price_feed.xau_return_pct(ev.dt_utc, minutes_after=5)
            effect_info = cal.forecast_vs_previous_effect(ev)
            bubble = post_release_bubble(ev, impact_info,
                                         actual_text=actual_text,
                                         surprise=surprise, verdict=verdict,
                                         xau_return_pct=xau_reaction,
                                         effect=effect_info)
            alt_extra = f" · actual {actual_text}" if actual_text else ""
            alt = f"📊 Released · {ev.country} {ev.title}{alt_extra}"
            resp = _push_or_skip(line, target, alt, bubble, sched_cfg, label="calendar")
            if resp["status"] == 200:
                store.upsert("sent_log", {
                    "event_id": sent_key, "route_type": "calendar_post",
                    "sent_ts": iso_utc(now_utc()), "line_status": 200,
                })
                post_pushed += 1

    log.info("calendar_check pushes: pre=%d post=%d", pre_pushed, post_pushed)
    store.flush()
    return 0


async def run_watchdog() -> int:
    """Pipeline-level self-monitor. Runs every 30 min (via watchdog.yml).
    Reads the heartbeat written by run_once; pushes a LINE health alert
    when the pipeline goes silent (cron stopped firing or Sheet writes
    broken) or when no items have been fetched across all sources for
    hours (likely scraper / network issue).

    Cooldown-aware: won't repeat the same warning inside 120 min. Pushes a
    `recovered` bubble when the warning clears."""
    store = Store.from_env()
    store.connect()
    store.load_all()

    warnings = health.check_pipeline_health(store)
    warning_types = {wt for wt, _ in warnings}

    line = None
    health_target = os.environ.get("LINE_HEALTH_TARGET", "")

    # Resolve any open watchdog warnings that are no longer in the warning
    # set — and notify on transition.
    recovered: list[tuple[str, str]] = []
    for warning_type in ("watchdog_silence", "watchdog_no_items"):
        if warning_type in warning_types:
            continue
        # Open warning that no longer applies → resolve + push recovered.
        if health.warning_open_minutes(store, health.HEARTBEAT_SOURCE_ID, warning_type) > 0:
            health.resolve_warning(store, health.HEARTBEAT_SOURCE_ID, warning_type)
            recovered.append((health.HEARTBEAT_SOURCE_ID, warning_type))

    # Raise fresh warnings (cooldown-gated).
    fresh: list[tuple[str, str]] = []
    for warning_type, message in warnings:
        new_row = health.raise_warning(
            store, health.HEARTBEAT_SOURCE_ID, warning_type, cooldown_min=120,
        )
        if new_row:
            fresh.append((warning_type, message))

    if fresh and health_target:
        line = line or LineClient.from_env()
        # Reuse the per-source health bubble — labelled "Pipeline" via the
        # SOURCE_NAMES alias for _pipeline_heartbeat. WARNING_MESSAGES has
        # entries for watchdog_silence / watchdog_no_items so the bubble
        # renders human-readable lines without extra plumbing.
        warning_pairs = [(health.HEARTBEAT_SOURCE_ID, wt) for wt, _ in fresh]
        bubble = health_bubble(warning_pairs)
        alt = f"🚨 Pipeline alert — {len(fresh)} issue(s)"
        line.push_flex(health_target, alt, bubble)
        for wt, msg in fresh:
            log.warning("watchdog: %s — %s", wt, msg)

    if recovered and health_target:
        line = line or LineClient.from_env()
        bubble = health_recovered_bubble(recovered)
        alt = f"✅ Pipeline recovered — {len(recovered)} item(s)"
        line.push_flex(health_target, alt, bubble)

    log.info("watchdog: fresh=%d recovered=%d", len(fresh), len(recovered))
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
        "cron", "event", "digest", "calendar_daily", "calendar_check",
        "weekly_preview", "eod_recap", "verify_sources", "maintain",
        "watchdog",
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
    if args.mode == "weekly_preview":
        return asyncio.run(run_weekly_preview())
    if args.mode == "eod_recap":
        return asyncio.run(run_eod_recap())
    if args.mode == "verify_sources":
        return asyncio.run(run_verify_sources())
    if args.mode == "maintain":
        return asyncio.run(run_maintain())
    if args.mode == "watchdog":
        return asyncio.run(run_watchdog())
    return asyncio.run(run_once(mode=args.mode))


if __name__ == "__main__":
    sys.exit(main())
