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
from . import dedup, digest, fred, health, news_alert, price_feed, scorer, social_feed, translator
from .fetcher import fetch_all, plan_fetch
from .line_client import LineClient
from .line_flex import (
    _pick_article_url,
    _source_label,
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
    score_to_impact,
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


def _push_or_skip(line, target, alt, bubble, sched_cfg, label="", bypass_quiet=False,
                   store=None):
    """LINE push wrapped in the quiet-hours gate. Returns the response dict
    on actual push, or a synthetic {status: 0, body: 'quiet_hours'} when
    suppressed so callers can keep idempotency logic clean.

    `bypass_quiet=True` skips the quiet-hours check — used for the daily
    calendar briefing at 04:40 ICT, which should fire even though it sits
    inside the 04:00-05:00 ICT market-close window.

    `store` is optional but recommended — passing it enables LINE
    quota + push-failure health tracking (watchdog detects 5xx streaks
    + 80% quota usage)."""
    if not bypass_quiet and is_quiet_hours_ict(_quiet_hours_cfg(sched_cfg)):
        log.info("quiet hours — suppressing push (%s)", label or "")
        return {"status": 0, "body": "quiet_hours"}
    resp = line.push_flex(target, alt, bubble)
    # Health tracking — counts only ACTUAL push attempts (not quiet-hour
    # suppressions). Records success or failure on every push attempt
    # so the watchdog can spot a degrading channel before it goes silent.
    from .line_client import record_line_outcome
    record_line_outcome(store, resp.get("status", 0))
    return resp

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
    #
    # BUT — still bump the heartbeat so the watchdog doesn't fire a
    # silence warning all weekend. The pipeline IS alive; it's just
    # idle. Caught live on 2026-05-23: 3 spam Health Check pushes
    # complained about silent cron when in fact cron was running every
    # 5 min and weekend-skipping correctly.
    if is_weekend_ict() and mode != "event":
        log.info("weekend (ICT) — skipping %s run", mode)
        try:
            store = Store.from_env()
            store.connect()
            store.load_all()
            health.write_heartbeat(store, items_seen=0)
            store.flush()
        except Exception as e:
            log.warning("weekend heartbeat write failed: %s", e)
        return 0

    store = Store.from_env()
    store.connect()
    store.load_all()

    # Social-feed records collected during this run (breaking/alert/digest) and
    # appended once before flush. Best-effort: never blocks the LINE push.
    social_records: list[dict[str, Any]] = []

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
            # Classify + rewrite into a structured Thai market alert. Skips
            # the push entirely when the classifier rejects the item
            # (personal finance / evergreen / opinion / stale).
            earliest = ev.first_seen_ts if ev.items else None
            from datetime import timezone as _tz
            age_hours = None
            if earliest:
                age_hours = (now_utc() - earliest).total_seconds() / 3600.0
            alert_obj = news_alert.classify_and_rewrite(
                ev.representative_title,
                ev.representative_summary,
                source_id=",".join(ev.source_list[:2]),
                age_hours=age_hours,
                store=store,
            )
            if not alert_obj.should_send:
                log.info("breaking/alert classifier rejected event_id=%s reason=%s",
                         ev.event_id, alert_obj.reason)
                continue
            if d.route == Route.BREAKING:
                bubble = breaking_bubble(ev, d.score, kw_cfg, alert=alert_obj)
                alt = alt_text_for_event("⚡ BREAKING", ev, d.score)
            else:
                bubble = alert_bubble(ev, d.score, kw_cfg, alert=alert_obj)
                alt = alt_text_for_event("🔔 ALERT", ev, d.score)
            resp = _push_or_skip(line, news_target, alt, bubble, sched_cfg, label=d.route.value, store=store)
            if resp["status"] == 200:
                store.upsert("sent_log", {
                    "event_id": ev.event_id,
                    "route_type": d.route.value,
                    "sent_ts": iso_utc(now_utc()),
                    "line_status": resp["status"],
                })
                try:
                    social_records.append(social_feed.record_news_event(
                        route=d.route.value,
                        category=alert_obj.category,
                        tone=alert_obj.tone,
                        impact_level=score_to_impact(d.score)[0],
                        headline_th=alert_obj.headline_th,
                        body_th=alert_obj.body_th,
                        impact_th=alert_obj.impact_th,
                        source=_source_label(ev.source_list),
                        url=_pick_article_url(ev.items),
                    ))
                except Exception:
                    log.exception("social_feed record (breaking/alert) failed event=%s", ev.event_id)
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
            _push_or_skip(line, health_target, alt, bubble, sched_cfg, label="health", store=store)

    # Push recoveries
    if recovered and health_target:
        line = line or LineClient.from_env()
        bubble = health_recovered_bubble(recovered)
        alt = f"✅ Health Recovered — {len(recovered)} item(s)"
        _push_or_skip(line, health_target, alt, bubble, sched_cfg, label="health_recovered", store=store)

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
            # Classify + rewrite each event. Rejected items are filtered
            # out of the carousel entirely — the classifier drops personal
            # finance / evergreen / opinion / stale articles that used to
            # leak through via keyword matching alone.
            ranked_alerts: dict[str, news_alert.MarketAlert] = {}
            kept: list[dedup.Event] = []
            for ev in ranked:
                earliest = ev.first_seen_ts if ev.items else None
                age_hours = None
                if earliest:
                    age_hours = (now_utc() - earliest).total_seconds() / 3600.0
                a = news_alert.classify_and_rewrite(
                    ev.representative_title,
                    ev.representative_summary,
                    source_id=",".join(ev.source_list[:2]),
                    age_hours=age_hours,
                    store=store,
                )
                if a.should_send:
                    ranked_alerts[ev.event_id] = a
                    kept.append(ev)
                else:
                    log.info("digest classifier rejected event_id=%s reason=%s",
                             ev.event_id, a.reason)
            log.info("digest classifier: %d/%d events kept", len(kept), len(ranked))
            if not kept:
                store.flush()
                return 0
            carousel = digest_carousel(kept, scores, slot, kw_cfg,
                                       alerts=ranked_alerts)
            if carousel and news_target:
                line = line or LineClient.from_env()
                # Use len(kept) so the LINE notification preview matches
                # what's actually inside the bubble — previously this showed
                # the pre-classifier pool size, so users saw "10 events"
                # in the notification but only 5 in the bubble.
                alt = f"📰 Digest {slot} ICT — {len(kept)} event(s)"
                resp = _push_or_skip(line, news_target, alt, carousel, sched_cfg, label="digest", store=store)
                digest.mark_sent(store, slot, resp["status"])
                # Per-event sent_log rows so EOD top_topics can count
                # only the events that actually reached the user (after
                # classifier filtering). Without these the EOD recap
                # over-reports the count.
                if resp["status"] == 200:
                    for ev in kept:
                        store.upsert("sent_log", {
                            "event_id": ev.event_id,
                            "route_type": "digest",
                            "sent_ts": iso_utc(now_utc()),
                            "line_status": resp["status"],
                        })
                        try:
                            a = ranked_alerts.get(ev.event_id)
                            if a:
                                social_records.append(social_feed.record_news_event(
                                    route="digest",
                                    category=a.category,
                                    tone=a.tone,
                                    impact_level=score_to_impact(scores.get(ev.event_id, 0))[0],
                                    headline_th=a.headline_th,
                                    body_th=a.body_th,
                                    impact_th=a.impact_th,
                                    source=_source_label(ev.source_list),
                                    url=_pick_article_url(ev.items),
                                ))
                        except Exception:
                            log.exception("social_feed record (digest) failed event=%s", ev.event_id)

    # 8. Heartbeat — stamp pipeline liveness before flush so the watchdog
    # can distinguish "cron stopped" from "cron ran but no news today".
    if mode in ("cron", "event"):
        health.write_heartbeat(store, items_seen=len(items))

    # 8b. Append social-feed rows (best-effort; never blocks the run)
    n_feed = social_feed.flush(store, social_records)
    if n_feed:
        log.info("social_feed: appended %d row(s)", n_feed)

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
            _push_or_skip(line, health_target, alt, bubble, sched_cfg, label="verify", store=store)
    store.flush()
    return 0


async def run_eod_recap() -> int:
    """End-of-day recap @ 23:00 ICT. Idempotent per ICT date that the
    recap is FOR (not the date the workflow happens to fire on).

    GitHub free-tier cron drops can delay this run past midnight ICT.
    When we fire at 01:xx ICT, the day we're summarising is yesterday,
    not "today" — so both the date label and the activity window
    must align on the recap's target day."""
    if is_weekend_ict():
        log.info("weekend (ICT) — skipping eod_recap")
        return 0
    _, _, sched_cfg = _load_configs()
    store = Store.from_env()
    store.connect()
    store.load_all()

    from datetime import datetime, time as _t, timedelta, timezone as _tz
    from .utils_time import ICT

    ict = now_ict()
    # Heuristic: hour >= 12 means we're firing on the same day the
    # recap is FOR (the scheduled 23:00 ICT slot). Below 12 means the
    # cron dropped and we're now running on the next ICT day — recap
    # is for the day that just ended.
    if ict.hour >= 12:
        recap_for_date = ict.date()
    else:
        recap_for_date = (ict - timedelta(days=1)).date()
    recap_start_ict = datetime.combine(recap_for_date, _t.min, tzinfo=ICT)
    recap_end_ict   = recap_start_ict + timedelta(days=1)
    today_start = recap_start_ict.astimezone(_tz.utc)
    today_end   = recap_end_ict.astimezone(_tz.utc)

    target_key = recap_for_date.strftime("%Y-%m-%d")
    sent_key = f"eod:{target_key}"
    if store.get("sent_log", (sent_key, "eod_recap")):
        log.info("eod_recap already sent for %s — skipping", target_key)
        store.flush()
        return 0
    breaking_n = alert_n = cal_pre_n = cal_post_n = 0
    from .utils_time import parse_iso
    for row in store.all_rows("sent_log"):
        ts = parse_iso(row.get("sent_ts"))
        if not ts or ts < today_start or ts >= today_end:
            continue
        rt = row.get("route_type", "")
        if rt == "breaking": breaking_n += 1
        elif rt == "alert": alert_n += 1
        elif rt == "calendar_pre": cal_pre_n += 1
        elif rt == "calendar_post": cal_post_n += 1

    # Top topics — only count events that were ACTUALLY PUSHED (not the
    # classifier-rejected ones). We intersect sent_log (filtered to the
    # recap window) with event_state to pick up topic_bucket per pushed
    # event. Pre-classifier this counted every clustered event regardless
    # of whether it reached the user, which was misleading.
    pushed_event_ids: set[str] = set()
    for row in store.all_rows("sent_log"):
        ts = parse_iso(row.get("sent_ts"))
        if not ts or ts < today_start or ts >= today_end:
            continue
        rt = row.get("route_type", "")
        if rt in ("breaking", "alert", "digest"):
            ev_id = row.get("event_id", "")
            if ev_id:
                pushed_event_ids.add(ev_id)

    topic_stats: dict[str, list[float]] = {}
    for row in store.all_rows("event_state"):
        if row.get("event_id") not in pushed_event_ids:
            continue
        topic = row.get("topic_bucket", "other")
        sc = float(row.get("score") or 0)
        topic_stats.setdefault(topic, []).append(sc)

    top_topics = [
        (topic, len(scores), max(scores))
        for topic, scores in topic_stats.items()
    ]
    top_topics.sort(key=lambda x: (-x[2], -x[1]))
    digest_events_n = sum(len(s) for s in topic_stats.values())

    # Classifier counters — 24h rolling window (Batch O: was cumulative
    # forever, now resets so degradation alerts fire on recent activity).
    # Token totals are still monthly (separate field on the same row).
    cl = news_alert.get_classifier_counters(store, source_id=None)
    classifier_kept = cl.get("kept", 0)
    classifier_rejected = cl.get("rejected", 0)
    classifier_fallback = cl.get("fallback", 0)
    classifier_total = classifier_kept + classifier_rejected
    month_tokens_in = cl.get("month_tokens_in", 0)
    month_tokens_out = cl.get("month_tokens_out", 0)

    stats = {
        "breaking_n": breaking_n,
        "alert_n": alert_n,
        "digest_events_n": digest_events_n,
        "calendar_pre_n": cal_pre_n,
        "calendar_post_n": cal_post_n,
        "top_topics": top_topics,
        "classifier_total": classifier_total,
        "classifier_kept": classifier_kept,
        "classifier_rejected": classifier_rejected,
        "classifier_fallback": classifier_fallback,
        "month_tokens_in": month_tokens_in,
        "month_tokens_out": month_tokens_out,
        "month_label": cl.get("month", ""),
    }
    target = os.environ.get("LINE_NEWS_TARGET", "")
    if not target:
        log.warning("LINE_NEWS_TARGET not set — skipping eod_recap push")
        store.flush()
        return 0
    line = LineClient.from_env()
    short_date = f"{recap_for_date.day}/{recap_for_date.month}/{recap_for_date.year % 100}"
    bubble = eod_recap_bubble(stats, short_date)
    alt = (f"🌙 EoD — {breaking_n} breaking, {alert_n} alert, "
           f"{cal_pre_n}+{cal_post_n} calendar")
    resp = _push_or_skip(line, target, alt, bubble, sched_cfg, label="eod_recap", store=store)
    if resp["status"] == 200:
        store.upsert("sent_log", {
            "event_id": sent_key, "route_type": "eod_recap",
            "sent_ts": iso_utc(now_utc()), "line_status": 200,
        })
        try:
            social_feed.flush(store, [social_feed.record_recap(stats, short_date)])
        except Exception:
            log.exception("social_feed recap record failed")
    store.flush()
    log.info("eod_recap done: %s", stats)
    return 0


async def run_social_seed() -> int:
    """Append one test draft to social_feed (creating the worksheet if needed).
    Lets you exercise the approve→post path without waiting for live news. The
    draft is clearly labelled as a test; delete the tweet from X afterwards."""
    store = Store.from_env()
    store.connect()
    rec = social_feed.record_news_event(
        route="test", category="Test", tone="neutral", impact_level="LOW",
        headline_th="ทดสอบระบบ social feed (ลบได้)",
        body_th=["draft ทดสอบจาก pipeline"],
        impact_th="ทดสอบการโพสต์อัตโนมัติขึ้น X",
        source="Pipeline", url="",
    )
    n = social_feed.flush(store, [rec])
    log.info("social_seed: appended %d test row(s) to social_feed", n)
    return 0


async def run_social_post() -> int:
    """Post approved social_feed drafts to X. Reads the social_feed worksheet,
    finds rows where `approved`=yes AND `posted` is empty, posts each via the X
    API, and stamps `posted` with the tweet URL. The operator gates every post
    by typing yes in the Sheet — nothing is posted automatically.

    Lightweight: no load_all / flush — it reads the feed tab and writes single
    cells directly. Per-tweet failures are logged and retried next run."""
    store = Store.from_env()
    store.connect()
    limit = int(os.environ.get("SOCIAL_POST_LIMIT", "5"))
    n = social_feed.post_pending(store, limit=limit)
    log.info("social_post: posted %d tweet(s)", n)
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

    # Translation cache — TTL 1 day + hard cap 2000 most-recent rows.
    # TTL keeps stale RSS titles from bloating Claude lookup; cap keeps
    # the Sheet small enough that load_all stays fast.
    removed_tc = store.purge_older_than("translation_cache", 1, ts_col="updated_at")
    removed_tc_cap = _cap_translation_cache(store, max_rows=2000)

    store.flush()

    log.info(
        "maintain done: event_state purged=%d, sent_log purged=%d, "
        "translation_cache TTL purged=%d + capped=%d, api_calls=%d",
        removed_es, removed_sl, removed_tc, removed_tc_cap, store.api_calls,
    )
    return 0


def _cap_translation_cache(store: Store, max_rows: int) -> int:
    """Drop oldest rows from translation_cache when over max_rows. Sorts
    by updated_at DESC — most recent kept, oldest evicted (LRU)."""
    rows = store.all_rows("translation_cache")
    if len(rows) <= max_rows:
        return 0
    rows.sort(key=lambda r: r.get("updated_at") or "", reverse=True)
    keepers = rows[:max_rows]
    keeper_keys = {r["cache_key"] for r in keepers}
    removed = 0
    for row in list(store.data.get("translation_cache", {}).values()):
        if row["cache_key"] not in keeper_keys:
            rk = row["cache_key"]
            store.data["translation_cache"].pop(rk, None)
            removed += 1
    if removed:
        store.dirty.setdefault("translation_cache", set()).add("__cap__")
    return removed


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
    # Short DD/M/YY range — gets folded into the title by the
    # weekly_preview_bubble header (e.g. "Week Ahead (25/5/26 – 29/5/26)").
    def _short(dt):
        return f"{dt.day}/{dt.month}/{dt.year % 100}"
    week_label = f"{_short(start)} – {_short(end)}"

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
                          bubble, sched_cfg, label="weekly_preview", store=store)
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
    # Market pulse — yfinance call wrapped in retry (price_feed handles
    # 3-attempt backoff). Any failure renders as a missing cell.
    def _to_tuple(snap):
        return (snap.last, snap.pct_change_day) if snap else None
    xau_tuple = _to_tuple(price_feed.get_xau_snapshot())
    dxy_tuple = _to_tuple(price_feed.get_dxy_snapshot())
    hui_tuple = _to_tuple(price_feed.get_hui_snapshot())
    gld_tuple = _to_tuple(price_feed.get_gld_snapshot())
    thb_tuple = _to_tuple(price_feed.get_thb_snapshot())
    bubble = calendar_day_bubble(
        filtered, date_label,
        xau_snapshot=xau_tuple, dxy_snapshot=dxy_tuple,
        hui_snapshot=hui_tuple, gld_snapshot=gld_tuple,
        thb_snapshot=thb_tuple,
    )
    if bubble is None:
        store.flush()
        return 0
    line = LineClient.from_env()
    # Daily briefing is the ONE push that's allowed through the
    # 04:00-05:00 ICT quiet window (it's the wake-up email of the day).
    resp = _push_or_skip(line, target, f"📅 Calendar — {len(filtered)} events today",
                          bubble, sched_cfg, label="calendar_daily",
                          bypass_quiet=True, store=store)
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
            resp = _push_or_skip(line, target, alt, bubble, sched_cfg, label="calendar", store=store)
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
                    # Health tracking — empty dict = scrape failed or all
                    # actuals empty. Watchdog promotes 3 in a row to LINE.
                    from . import ff_scraper
                    ff_scraper.record_scrape_result(store, len(ff_actuals_cache))
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
            resp = _push_or_skip(line, target, alt, bubble, sched_cfg, label="calendar", store=store)
            if resp["status"] == 200:
                store.upsert("sent_log", {
                    "event_id": sent_key, "route_type": "calendar_post",
                    "sent_ts": iso_utc(now_utc()), "line_status": 200,
                })
                post_pushed += 1

    log.info("calendar_check pushes: pre=%d post=%d", pre_pushed, post_pushed)
    store.flush()
    return 0


def _watchdog_source_id(warning_type: str) -> str:
    """Map a watchdog warning_type to the source_state row it's about.
    Pipeline-level warnings go under _pipeline_heartbeat; per-subsystem
    warnings (FF scraper, classifier health, per-source noise) go under
    their own row id so they raise / resolve independently."""
    if warning_type == "ff_scraper_dead":
        return "_ff_scraper"
    if warning_type == "classifier_degraded":
        return "_classifier_health"
    if warning_type == "workflow_failure":
        return "_workflow_failures"
    if warning_type in ("line_push_failing", "line_quota_high"):
        return "_line_push"
    if warning_type.startswith("source_noisy:"):
        return f"_class:{warning_type.split(':', 1)[1][:30]}"
    return health.HEARTBEAT_SOURCE_ID


async def run_watchdog() -> int:
    """Pipeline-level self-monitor. Runs every 30 min (via watchdog.yml).
    Reads the heartbeat written by run_once and the FF scraper streak;
    pushes a LINE health alert when:
      - pipeline silent (cron stopped or Sheet writes broken)
      - all sources returned 0 items for hours (network/scraper issue)
      - FF HTML scraper hit 3 consecutive empties (Cloudflare/HTML change)

    Cooldown-aware: won't repeat the same warning inside 120 min. Pushes a
    `recovered` bubble when each individual warning clears."""
    store = Store.from_env()
    store.connect()
    store.load_all()

    warnings = health.check_pipeline_health(store)

    # Workflow failure check — read GH Actions API for recent failed
    # runs. Requires GITHUB_TOKEN (auto-provided inside the workflow) +
    # GITHUB_REPOSITORY. Silent no-op when either is missing.
    wf_failures = health.check_recent_workflow_failures(hours=24)
    if wf_failures:
        # Bundle into ONE warning so the bubble doesn't spam — show
        # count + first failure as preview.
        first = wf_failures[0]
        if len(wf_failures) > 1:
            msg = f"{len(wf_failures)} workflow runs failed in 24h. Latest: {first}"
        else:
            msg = f"Workflow failed: {first}"
        warnings.append(("workflow_failure", msg))

    warning_types = {wt for wt, _ in warnings}

    line = None
    health_target = os.environ.get("LINE_HEALTH_TARGET", "")

    # Resolve any open warnings that are no longer firing — one row per
    # (source_id, warning_type) so each clears independently. Static
    # warning types are listed here; dynamic source_noisy:* warnings
    # auto-resolve via the scan below.
    recovered: list[tuple[str, str]] = []
    static_types = ("watchdog_silence", "watchdog_no_items", "ff_scraper_dead",
                    "classifier_degraded", "workflow_failure",
                    "line_push_failing", "line_quota_high")
    for warning_type in static_types:
        if warning_type in warning_types:
            continue
        sid = _watchdog_source_id(warning_type)
        if health.warning_open_minutes(store, sid, warning_type) > 0:
            health.resolve_warning(store, sid, warning_type)
            recovered.append((sid, warning_type))
    # Source-noise auto-resolve: any open source_noisy:* warning whose
    # current ratio fell back below the threshold should be cleared.
    for row in store.all_rows("health_log"):
        wt = row.get("warning_type", "")
        if not wt.startswith("source_noisy:") or row.get("resolved_ts"):
            continue
        if wt not in warning_types:
            sid = _watchdog_source_id(wt)
            health.resolve_warning(store, sid, wt)
            recovered.append((sid, wt))

    # Raise fresh warnings (cooldown-gated, per (source_id, warning_type)).
    fresh: list[tuple[str, str, str]] = []  # (source_id, warning_type, message)
    for warning_type, message in warnings:
        sid = _watchdog_source_id(warning_type)
        new_row = health.raise_warning(store, sid, warning_type, cooldown_min=120)
        if new_row:
            fresh.append((sid, warning_type, message))

    if fresh and health_target:
        line = line or LineClient.from_env()
        # health_bubble takes (source_id, warning_type) pairs — labels come
        # from SOURCE_NAMES + WARNING_MESSAGES tables in line_flex.py.
        warning_pairs = [(sid, wt) for sid, wt, _ in fresh]
        bubble = health_bubble(warning_pairs)
        alt = f"🚨 Pipeline alert — {len(fresh)} issue(s)"
        line.push_flex(health_target, alt, bubble)
        for sid, wt, msg in fresh:
            log.warning("watchdog: %s/%s — %s", sid, wt, msg)

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
        "watchdog", "social_post", "social_seed",
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
    if args.mode == "social_post":
        return asyncio.run(run_social_post())
    if args.mode == "social_seed":
        return asyncio.run(run_social_seed())
    return asyncio.run(run_once(mode=args.mode))


if __name__ == "__main__":
    sys.exit(main())
