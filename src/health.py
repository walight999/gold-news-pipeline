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
from datetime import timedelta
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
    cutoff = now_utc() - timedelta(minutes=cooldown_min)
    for row in store.all_rows("health_log"):
        if row.get("source_id") != source_id or row.get("warning_type") != warning_type:
            continue
        if row.get("resolved_ts"):
            continue  # resolved means the next warning is allowed
        ts = parse_iso(row.get("warning_ts"))
        if ts and ts >= cutoff:
            return True
    return False


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
