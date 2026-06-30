"""Health channel.

Gated feed-validator (NOT run every cron):
  - never validated, OR
  - failed previously (consecutive_errors > 0), OR
  - last_validation_ts > 24h, OR
  - manual mode.

Anti-spam: never repeat the same (source_id, warning_type) inside cooldown
unless it was resolved, then re-fired.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from .store import Store
from .utils_time import iso_utc, now_utc, parse_iso

log = logging.getLogger(__name__)


def needs_validation(state: dict[str, Any] | None, hours: int = 24, manual: bool = False) -> bool:
    if manual:
        return True
    if not state:
        return True
    if int(state.get("consecutive_errors") or 0) > 0:
        return True
    last = parse_iso(state.get("last_validation_ts"))
    if not last:
        return True
    return (now_utc() - last) > timedelta(hours=hours)


def mark_validated(store: Store, source_id: str) -> None:
    state = store.get("source_state", (source_id,)) or {"source_id": source_id}
    state["source_id"] = source_id
    state["last_validation_ts"] = iso_utc(now_utc())
    store.upsert("source_state", state)


def _recent_alert_for(store: Store, source_id: str, warning_type: str, cooldown_min: int) -> bool:
    """Cooldown lookup. Applies REGARDLESS of resolution status — without
    this, oscillating conditions (e.g. ForexLive going 28-32min between
    posts) bypass the cooldown by resolving and re-firing every cycle."""
    cutoff = now_utc() - timedelta(minutes=cooldown_min)
    for row in store.all_rows("health_log"):
        if row.get("source_id") != source_id or row.get("warning_type") != warning_type:
            continue
        ts = parse_iso(row.get("warning_ts"))
        if ts and ts >= cutoff:
            return True
    return False


def warning_open_minutes(store: Store, source_id: str, warning_type: str) -> float:
    """Returns how many minutes the most recent (still-open) warning for this
    (source, type) has been open. 0.0 if no open warning."""
    most_recent_open_ts: datetime | None = None
    for row in store.all_rows("health_log"):
        if row.get("source_id") != source_id or row.get("warning_type") != warning_type:
            continue
        if row.get("resolved_ts"):
            continue
        ts = parse_iso(row.get("warning_ts"))
        if ts and (most_recent_open_ts is None or ts > most_recent_open_ts):
            most_recent_open_ts = ts
    if most_recent_open_ts is None:
        return 0.0
    return (now_utc() - most_recent_open_ts).total_seconds() / 60.0


def raise_warning(store: Store, source_id: str, warning_type: str, cooldown_min: int = 60) -> bool:
    """Returns True if a NEW warning row was written (caller should push to
    health channel). Returns False if suppressed by cooldown.

    Cooldown auto-extends on repeat fires of the same condition so an
    ongoing outage doesn't spam every cycle:
      1st fire: now (base cooldown)
      2nd fire: ≥4h since last
      3rd+ fire: ≥12h since last
    After 24h with no new fires the counter resets — a fresh outage
    gets the normal cooldown again."""
    recent_24h = _count_recent_warnings(store, source_id, warning_type, hours=24)
    if recent_24h >= 2:
        cooldown_min = max(cooldown_min, 12 * 60)
    elif recent_24h >= 1:
        cooldown_min = max(cooldown_min, 4 * 60)
    if _recent_alert_for(store, source_id, warning_type, cooldown_min):
        return False
    ts = iso_utc(now_utc())
    store.upsert("health_log", {
        "source_id": source_id,
        "warning_type": warning_type,
        "warning_ts": ts,
        "resolved_ts": "",
    })
    return True


def _count_recent_warnings(store: Store, source_id: str, warning_type: str, hours: int = 24) -> int:
    """How many warnings of this (source, type) fired in the last N
    hours. Used to back off cooldown on ongoing conditions."""
    cutoff = now_utc() - timedelta(hours=hours)
    n = 0
    for row in store.all_rows("health_log"):
        if row.get("source_id") != source_id or row.get("warning_type") != warning_type:
            continue
        ts = parse_iso(row.get("warning_ts"))
        if ts and ts >= cutoff:
            n += 1
    return n


def resolve_warning(store: Store, source_id: str, warning_type: str) -> int:
    """Mark all open warnings of this (source, type) as resolved. Returns count resolved."""
    ts = iso_utc(now_utc())
    n = 0
    for row in list(store.all_rows("health_log")):
        if row.get("source_id") == source_id and row.get("warning_type") == warning_type and not row.get("resolved_ts"):
            row["resolved_ts"] = ts
            store.upsert("health_log", row)
            n += 1
    return n


# ---------------- Pipeline-level self-monitoring ----------------
#
# Per-source health (above) detects upstream breakage. The heartbeat below
# detects pipeline-level breakage: cron not firing, Sheet writes failing,
# all sources returning 0 items, etc. Watchdog mode reads the heartbeat
# every 30 min and alerts when stale.

HEARTBEAT_SOURCE_ID = "_pipeline_heartbeat"


def write_heartbeat(store: Store, items_seen: int = 0) -> None:
    """Stamp a liveness marker into source_state. Called from run_once at
    end of every successful run. The row's `last_success_ts` is the last
    successful pipeline iteration; `last_item_ts` is the last iteration
    that actually saw items > 0 (distinguishes silent crash vs. quiet news
    day)."""
    ts = iso_utc(now_utc())
    prev = store.get("source_state", (HEARTBEAT_SOURCE_ID,)) or {}
    last_item_ts = ts if items_seen > 0 else (prev.get("last_item_ts") or "")
    store.upsert("source_state", {
        "source_id": HEARTBEAT_SOURCE_ID,
        "last_success_ts": ts,
        "last_item_ts": last_item_ts,
        "items_last_hour": str(items_seen),
        "updated_at": ts,
    })


def ping_deadman(url: str | None) -> bool:
    """Ping an EXTERNAL dead-man monitor (healthchecks.io / BetterStack / cron-job.org
    monitor) on every successful run. This is the independent watchdog: the in-repo
    watchdog mode shares GitHub Actions + the cron-job.org dispatcher's lifeline, so if
    the dispatcher dies the watchdog dies with it (the 2026-06-23 dead-zone). An external
    monitor that expects a ping every N minutes will alert when the pings stop, no matter
    why. Env-gated by HEALTHCHECK_PING_URL; best-effort, never raises."""
    if not url:
        return False
    try:
        import httpx
        httpx.get(url, timeout=10.0)
        return True
    except Exception as e:  # noqa: BLE001
        log.warning("deadman ping failed: %s", e)
        return False


MACRO_HEARTBEAT_SOURCE_ID = "_macro_heartbeat"


def write_macro_heartbeat(store: Store) -> None:
    """Stamp macro-push liveness into source_state. Called by run_macro_push after a
    successful POST so the watchdog can alarm if the (separate, 6-hourly) macro push
    silently stops — otherwise every signal tags macro_aligned=null invisibly."""
    ts = iso_utc(now_utc())
    store.upsert("source_state", {
        "source_id": MACRO_HEARTBEAT_SOURCE_ID,
        "last_success_ts": ts,
        "updated_at": ts,
    })


def check_pipeline_health(
    store: Store,
    max_silence_min: int = 25,
    max_no_items_min: int = 180,
    max_macro_silence_min: int = 840,  # 14h — alert BEFORE the worker's 18h staleness gate
    max_ff_scrape_errors: int = 3,
    classifier_fallback_threshold_pct: int = 30,
    classifier_min_samples: int = 20,
    source_reject_threshold_pct: int = 90,
    source_min_samples: int = 50,
) -> list[tuple[str, str]]:
    """Returns (warning_type, human_message) pairs. Empty list = healthy.
    Used by --mode watchdog.

    Three failure modes:
      `watchdog_silence`  — heartbeat hasn't ticked in >max_silence_min.
                            Cron stopped firing OR Sheet writes broken.
      `watchdog_no_items` — heartbeat ticking but 0 items for >max_no_items_min
                            during market hours. All sources dead simultaneously
                            (unlikely random, likely scraper / network issue).
      `ff_scraper_dead`   — FF HTML scraper returned 0 events
                            max_ff_scrape_errors times in a row. Cloudflare
                            tightened OR FF changed HTML — post-release
                            actuals for non-FRED events go silent."""
    out: list[tuple[str, str]] = []
    row = store.get("source_state", (HEARTBEAT_SOURCE_ID,)) or {}
    last_hb = parse_iso(row.get("last_success_ts"))
    if not last_hb:
        out.append(("watchdog_silence",
                    "No heartbeat ever recorded — pipeline has never run successfully"))
    else:
        silence_min = (now_utc() - last_hb).total_seconds() / 60.0
        if silence_min > max_silence_min:
            out.append(("watchdog_silence",
                        f"Pipeline silent for {silence_min:.0f} min "
                        f"(last heartbeat {row.get('last_success_ts')})"))
        last_item = parse_iso(row.get("last_item_ts"))
        if last_item:
            no_item_min = (now_utc() - last_item).total_seconds() / 60.0
            if no_item_min > max_no_items_min:
                out.append(("watchdog_no_items",
                            f"No items fetched across all sources for {no_item_min:.0f} min — "
                            "scraper or network may be down"))

    # Macro-push staleness. Only alarm if macro was EVER activated (heartbeat row
    # exists) — a missing row = env-gated off, not a failure (mirrors the worker's
    # "never launched → don't alarm"). Stale = the 6h push has missed >2 cycles.
    macro_row = store.get("source_state", (MACRO_HEARTBEAT_SOURCE_ID,))
    if macro_row:
        last_macro = parse_iso(macro_row.get("last_success_ts"))
        if last_macro:
            macro_silence_min = (now_utc() - last_macro).total_seconds() / 60.0
            if macro_silence_min > max_macro_silence_min:
                out.append(("macro_push_dead",
                            f"Macro push silent for {macro_silence_min / 60:.1f}h "
                            f"(last {macro_row.get('last_success_ts')}). Pillar-C signal "
                            "tagging will go null — check macro_push.yml / cron-job.org."))

    # FF scraper streak check. Row is written by ff_scraper.record_scrape_result.
    ff_row = store.get("source_state", ("_ff_scraper",)) or {}
    ff_consec = int(ff_row.get("consecutive_errors") or 0)
    if ff_consec >= max_ff_scrape_errors:
        last_ok = ff_row.get("last_success_ts") or "never"
        out.append(("ff_scraper_dead",
                    f"FF HTML scrape returned 0 events {ff_consec}× in a row "
                    f"(last success: {last_ok}). Cloudflare tightened or HTML changed."))

    # Classifier degradation — Claude key invalidated / API down → most
    # calls fall through to permissive Google translate. LINE channel
    # gets noisy but no error fires. Detect via fallback ratio.
    from .news_alert import get_classifier_counters
    cl = get_classifier_counters(store, source_id=None)
    cl_total = (cl.get("kept", 0) + cl.get("rejected", 0))
    cl_fallback = cl.get("fallback", 0)
    if cl_total >= classifier_min_samples:
        pct = (cl_fallback / cl_total) * 100
        if pct >= classifier_fallback_threshold_pct:
            out.append(("classifier_degraded",
                        f"Classifier fallback rate {pct:.0f}% over {cl_total} samples "
                        f"({cl_fallback} fell to literal-translation). "
                        f"ANTHROPIC_API_KEY invalid or Claude down?"))

    # LINE push health — consecutive failures + monthly quota.
    from .line_client import LINE_PUSH_SOURCE_ID, get_line_quota_status, LINE_FREE_TIER_QUOTA
    line_row = store.get("source_state", (LINE_PUSH_SOURCE_ID,)) or {}
    line_consec = int(line_row.get("consecutive_errors") or 0)
    if line_consec >= 5:
        out.append(("line_push_failing",
                    f"LINE push failed {line_consec}× in a row — token expired? channel disabled?"))
    qs = get_line_quota_status(store)
    if qs.get("count", 0) > 0:
        pct = qs.get("pct", 0)
        if pct >= 80:
            count = qs.get("count")
            month = qs.get("month") or "this month"
            out.append(("line_quota_high",
                        f"LINE free-tier usage {count}/{LINE_FREE_TIER_QUOTA} ({pct}%) for {month} "
                        "— upgrading to Light plan recommended."))

    # Per-source reject rate. If a source's classifier reject rate is
    # >90% over the all-time counter window, it's just noise — consider
    # disabling in sources.yaml. We only fire when the source has a
    # statistically meaningful sample (>=50 items).
    for row in store.all_rows("source_state"):
        sid = row.get("source_id", "")
        if not sid.startswith("_class:"):
            continue
        source_id = sid[len("_class:"):]
        cnt = get_classifier_counters(store, source_id=source_id)
        total = cnt.get("kept", 0) + cnt.get("rejected", 0)
        rejected = cnt.get("rejected", 0)
        if total < source_min_samples:
            continue
        pct = (rejected / total) * 100
        if pct >= source_reject_threshold_pct:
            out.append((f"source_noisy:{source_id}",
                        f"Source '{source_id}' reject rate {pct:.0f}% "
                        f"over {total} items — consider disabling in sources.yaml."))

    return out


def check_recent_workflow_failures(
    repo: str | None = None,
    token: str | None = None,
    hours: int = 24,
) -> list[str]:
    """Query GitHub Actions API for failed workflow runs in the last N
    hours. Returns a list of "<workflow> failed at <time>" descriptions.

    Reads `GITHUB_TOKEN` (auto-provided inside Actions) and `GITHUB_REPOSITORY`
    from env when args aren't provided. Returns [] silently when either
    is missing — fail open so the watchdog still works locally."""
    import os as _os
    repo = repo or _os.environ.get("GITHUB_REPOSITORY")
    token = token or _os.environ.get("GITHUB_TOKEN")
    if not repo or not token:
        return []

    import httpx as _httpx
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    cutoff = _dt.now(_tz.utc) - _td(hours=hours)
    url = f"https://api.github.com/repos/{repo}/actions/runs"
    headers = {"Authorization": f"Bearer {token}",
               "Accept": "application/vnd.github+json"}
    out: list[str] = []
    try:
        # Filter to failed conclusions only — saves bandwidth.
        params = {"status": "failure", "per_page": 30}
        r = _httpx.get(url, headers=headers, params=params, timeout=15)
        r.raise_for_status()
        for run in r.json().get("workflow_runs", []):
            created = run.get("created_at", "")
            try:
                ts = _dt.fromisoformat(created.replace("Z", "+00:00"))
            except (TypeError, ValueError):
                continue
            if ts < cutoff:
                continue
            name = run.get("name") or run.get("workflow_id") or "?"
            out.append(f"{name} failed at {created}")
    except Exception as e:
        log.warning("check_recent_workflow_failures: %s", e)
        return []
    return out


def check_source_health(
    store: Store,
    source: dict[str, Any],
    cfg: dict[str, Any],
    is_event_day: bool = False,
) -> list[tuple[str, str]]:
    """Inspect state per spec §4.1. Returns (source_id, warning_type) pairs to alert about.
    Caller decides whether to push to LINE."""
    sid = source["id"]
    tier = int(source["tier"])
    state = store.get("source_state", (sid,)) or {}
    out: list[tuple[str, str]] = []

    consecutive_errors = int(state.get("consecutive_errors") or 0)
    if consecutive_errors >= int(cfg.get("http_consecutive_errors_threshold", 3)):
        out.append((sid, "http_errors_streak"))

    last_success = parse_iso(state.get("last_success_ts"))
    last_item = parse_iso(state.get("last_item_ts"))
    now = now_utc()

    if tier == 0 and is_event_day:
        if not last_success or (now - last_success) > timedelta(minutes=15):
            out.append((sid, "tier0_event_day_no_success"))
    if tier == 1:
        threshold = int(cfg.get("tier1_no_success_minutes", 60))
        if not last_success or (now - last_success) > timedelta(minutes=threshold):
            out.append((sid, "tier1_no_success"))
    if tier == 2:
        threshold = int(cfg.get("tier2_no_item_minutes", 30))
        if (not last_item or (now - last_item) > timedelta(minutes=threshold)):
            out.append((sid, "tier2_no_item"))
    return out
