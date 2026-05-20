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
