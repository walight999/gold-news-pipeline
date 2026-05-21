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
) -> list[tuple[str, str]]:
    """Returns (warning_type, human_message) pairs. Empty list = healthy.
    Used by --mode watchdog.

    Two failure modes:
      `watchdog_silence`  — heartbeat hasn't ticked in >max_silence_min.
                            Cron stopped firing OR Sheet writes broken.
      `watchdog_no_items` — heartbeat ticking but 0 items for >max_no_items_min
                            during market hours. All sources dead simultaneously
                            (unlikely random, likely scraper / network issue)."""
    out: list[tuple[str, str]] = []
    row = store.get("source_state", (HEARTBEAT_SOURCE_ID,)) or {}
    last_hb = parse_iso(row.get("last_success_ts"))
    if not last_hb:
        out.append(("watchdog_silence",
                    "No heartbeat ever recorded — pipeline has never run successfully"))
        return out
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
