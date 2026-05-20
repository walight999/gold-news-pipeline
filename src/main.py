"""Entry point.

Modes:
  cron   — */5m: respect each source's poll_min, normal routing.
  event  — Tier-0-only loop, 30m, sleep 60s. Triggered by Calendar Bot dispatch.
  digest — Build a digest if now_ict ∈ ±5m of a slot. Idempotent.
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

from . import dedup, digest, health, scorer
from .fetcher import fetch_all, plan_fetch
from .line_client import LineClient
from .normalizer import normalize
from .parser import parse_feed
from .router import Route, decide, format_alert, format_breaking
from .store import Store
from .utils_time import iso_utc, now_utc, within_digest_slot

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
            health.resolve_warning(store, r.source["id"], "http_errors_streak")
    items = normalize(raw_entries)
    log.info("items normalized: %d (from %d entries)", len(items), len(raw_entries))

    # 3. Cluster + score
    events = dedup.cluster(items, kw_cfg)
    scores: dict[str, float] = {ev.event_id: scorer.score_event(ev, kw_cfg) for ev in events}
    log.info("clustered events: %d", len(events))

    # 4. Route
    rl = sched_cfg.get("rate_limit", {})
    decisions = decide(
        events, scores, store,
        rate_limit_window_min=int(rl.get("breaking_alert_window_minutes", 15)),
        rate_limit_max=int(rl.get("breaking_alert_max", 5)),
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
            text = format_breaking(ev, d.score, kw_cfg) if d.route == Route.BREAKING else format_alert(ev, d.score, kw_cfg)
            resp = line.push(news_target, text)
            if resp["status"] == 200:
                store.upsert("sent_log", {
                    "event_id": ev.event_id,
                    "route_type": d.route.value,
                    "sent_ts": iso_utc(now_utc()),
                    "line_status": resp["status"],
                })
            else:
                log.warning("LINE push failed event=%s status=%s — not marking sent", ev.event_id, resp["status"])

    # 6. Health pass
    health_cfg = sched_cfg.get("health", {})
    is_event_day = (mode == "event")
    health_warnings: list[tuple[str, str]] = []
    for s in src_cfg["sources"]:
        if not s.get("enabled"):
            continue
        health_warnings.extend(health.check_source_health(store, s, health_cfg, is_event_day=is_event_day))

    if health_warnings and health_target:
        line = line or LineClient.from_env()
        cooldown = int(health_cfg.get("alert_cooldown_minutes", 60))
        msg_lines = ["⚠️ HEALTH"]
        emitted = 0
        for sid, wtype in health_warnings:
            if health.raise_warning(store, sid, wtype, cooldown):
                msg_lines.append(f"- {sid}: {wtype}")
                emitted += 1
        if emitted:
            line.push(health_target, "\n".join(msg_lines))

    # 7. Digest if in slot
    if mode in ("cron", "digest"):
        slots_ict = sched_cfg["digest"]["slots_ict"]
        window = int(sched_cfg["digest"]["window_minutes"])
        slot = within_digest_slot(slots_ict, window)
        if slot and not digest.already_sent(store, slot):
            # build from events scoring >= 2.5 OR rate-limit-downgrade routed digest
            digest_events = [d.event for d in decisions if d.route == Route.DIGEST]
            text = digest.build_digest_text(
                digest_events, scores, slot,
                max_events=int(sched_cfg["digest"].get("max_events", 10)),
                kw_config=kw_cfg,
            )
            if text and news_target:
                line = line or LineClient.from_env()
                resp = line.push(news_target, text)
                digest.mark_sent(store, slot, resp["status"])

    # 8. Flush state
    store.flush()
    log.info("done. sheets API calls=%d", store.api_calls)
    return 0


# --------------- Event-mode loop ---------------

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
    p.add_argument("--mode", choices=("cron", "event", "digest"), default="cron")
    p.add_argument("--event-duration-min", type=int, default=30)
    p.add_argument("--event-sleep-sec", type=int, default=60)
    args = p.parse_args(argv)
    if args.mode == "event":
        return asyncio.run(run_event_mode(args.event_duration_min, args.event_sleep_sec))
    return asyncio.run(run_once(mode=args.mode))


if __name__ == "__main__":
    sys.exit(main())
