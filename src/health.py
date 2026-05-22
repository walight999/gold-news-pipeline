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
    """Returns True if a NEW warning row was written (caller should push to health channel).
    Returns False if suppressed by cooldown."""
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


def check_pipeline_health(
    store: Store,
    max_silence_min: int = 25,
    max_no_items_min: int = 180,
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
