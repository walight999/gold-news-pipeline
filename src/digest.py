"""Digest builder. Runs when now_ict is within ±5m of a configured slot.

Idempotent via sent_log entry keyed `digest|YYYY-MM-DD_HH:MM`.
"""
from __future__ import annotations

import logging
from typing import Any

from .dedup import Event
from .store import Store
from .utils_time import digest_sent_key

log = logging.getLogger(__name__)

MAX_EVENTS_DEFAULT = 10


def already_sent(store: Store, slot: str) -> bool:
    key = digest_sent_key(slot)
    row = store.get("sent_log", (f"digest:{key}", "digest"))
    return row is not None


# Statuses that mean the event was already pushed on its own (breaking/alert)
# — never re-surface those in a digest round.
_PUSHED_STATUSES = {"breaking", "alert"}


def _already_individually_sent(store: Store, event_id: str) -> bool:
    """True if this event_id already went out as breaking / alert / a prior
    digest round. Stops the same story repeating across the 6 daily windows
    and stops a breaking item being echoed in the next digest."""
    for route in ("breaking", "alert", "digest"):
        if store.get("sent_log", (event_id, route)) is not None:
            return True
    return False


def collect_window_events(
    store: Store,
    now: "datetime",
    window_hours: float,
    min_score: float,
    max_candidates: int = 30,
) -> list[dict[str, Any]]:
    """Gather the digest candidate pool for one window round, straight from
    event_state — so a round covers EVERY gold-relevant event first seen in
    the last `window_hours`, not just whatever the current cron run fetched.

    A row qualifies when ALL hold:
      - status is not breaking / alert (those were pushed individually)
      - score >= min_score
      - it carries a title (needed to render + re-classify)
      - first_seen_ts is within the window
      - it hasn't already gone out (breaking / alert / earlier digest round)

    Returns the rows ranked by score desc (newest first on ties), capped at
    `max_candidates`. The caller classifies down this list until it has enough
    keepers — the classifier + relevance gate is the real quality filter.
    """
    from datetime import timedelta

    from .utils_time import parse_iso

    cutoff = now - timedelta(hours=window_hours)
    out: list[tuple[float, float, dict[str, Any]]] = []
    for row in store.all_rows("event_state"):
        if str(row.get("status") or "").strip() in _PUSHED_STATUSES:
            continue
        title = str(row.get("title") or "").strip()
        if not title:
            continue
        try:
            score = float(row.get("score") or 0)
        except (TypeError, ValueError):
            continue
        if score < min_score:
            continue
        first_seen = parse_iso(row.get("first_seen_ts"))
        if first_seen is None or first_seen < cutoff:
            continue
        if _already_individually_sent(store, str(row.get("event_id") or "")):
            continue
        out.append((score, first_seen.timestamp(), row))
    out.sort(key=lambda t: (-t[0], -t[1]))
    return [r for _, _, r in out[:max_candidates]]


def mark_sent(store: Store, slot: str, line_status: int) -> None:
    from .utils_time import iso_utc, now_utc
    key = digest_sent_key(slot)
    store.upsert("sent_log", {
        "event_id": f"digest:{key}",
        "route_type": "digest",
        "sent_ts": iso_utc(now_utc()),
        "line_status": line_status,
    })


def _rank_events(events: list[Event], scores: dict[str, float]) -> list[Event]:
    return sorted(
        events,
        key=lambda e: (-scores.get(e.event_id, 0.0), -e.source_count, -e.first_seen_ts.timestamp()),
    )


def _group_by_topic(events: list[Event]) -> dict[str, list[Event]]:
    out: dict[str, list[Event]] = {}
    for ev in events:
        out.setdefault(ev.topic_bucket, []).append(ev)
    return out


def build_digest_text(
    events: list[Event],
    scores: dict[str, float],
    slot: str,
    max_events: int = MAX_EVENTS_DEFAULT,
    kw_config: dict[str, Any] | None = None,
) -> str:
    ranked = _rank_events(events, scores)[:max_events]
    if not ranked:
        return ""
    nm = (kw_config or {}).get("name_map", {})
    groups = _group_by_topic(ranked)
    lines = [f"📰 Digest {slot} ICT — {len(ranked)} event(s)"]
    for topic in sorted(groups.keys()):
        lines.append(f"\n[{topic}]")
        for ev in groups[topic]:
            title = ev.representative_title
            for en, th in nm.items():
                if en.lower() in title.lower():
                    title = title.replace(en, f"{en} ({th})")
                    break
            src = ",".join(ev.source_list)
            lines.append(f"- ({scores.get(ev.event_id, 0):.1f}) {title} — {src}")
    return "\n".join(lines)
