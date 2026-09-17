"""Digest builder. Runs when now_ict is within ±5m of a configured slot.

Idempotent via sent_log entry keyed `digest|YYYY-MM-DD_HH:MM`.
"""
from __future__ import annotations

import hashlib
import logging
import re
from typing import Any

from .dedup import Event
from .store import Store
from .utils_time import digest_sent_key

log = logging.getLogger(__name__)

MAX_EVENTS_DEFAULT = 10

_CONTENT_NORM = re.compile(r"[^a-z0-9]+")


def content_sig(title: str) -> str:
    """Normalized signature of a headline for CONTENT-level dedup.

    The event_id keys on a 60-min time bucket (dedup.CLUSTER_WINDOW_MIN), so the
    SAME wire headline re-posted across a bucket boundary — even from the same
    source ~50 min later — gets a FRESH event_id that the event_id-only digest
    dedup cannot recognize as already-sent. That is how a verbatim story ships
    twice (confirmed 2026-09-17: "Trump hopes Iran war nearing end…" sent as two
    cards, cluster_keys identical except T0800 vs T0900).

    Lowercase + collapse every non-alphanumeric run, so verbatim re-posts map to
    ONE sig while genuinely different headlines (different numbers/places, e.g.
    "Syzran refinery" vs "Yaroslavl refinery", "PPI INPUT" vs "PPI OUTPUT") keep
    distinct sigs and are never wrongly merged. Empty title → "" (no dedup)."""
    t = _CONTENT_NORM.sub(" ", (title or "").lower()).strip()
    return hashlib.sha256(t.encode("utf-8")).hexdigest()[:16] if t else ""


def _content_already_sent(store: Store, sig: str) -> bool:
    """True if a card with this content signature already went out (any prior
    round/day), regardless of event_id. Backstops the event_id dedup against the
    time-bucket-boundary duplicate above."""
    return bool(sig) and store.get("sent_log", (f"content:{sig}", "content")) is not None


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
    exclude_buckets: "set[str] | None" = None,
) -> list[dict[str, Any]]:
    """Gather the digest candidate pool for one window round, straight from
    event_state — so a round covers EVERY gold-relevant event first seen in
    the last `window_hours`, not just whatever the current cron run fetched.

    A row qualifies when ALL hold:
      - status is not breaking / alert (those were pushed individually)
      - topic_bucket is not in `exclude_buckets`
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

    excluded = exclude_buckets or set()
    cutoff = now - timedelta(hours=window_hours)
    out: list[tuple[float, float, dict[str, Any]]] = []
    for row in store.all_rows("event_state"):
        if str(row.get("status") or "").strip() in _PUSHED_STATUSES:
            continue
        # Bucket exclusion (config: digest.exclude_buckets, default ["other"]).
        # Measured 2026-08-13: `other` was 1611 of 3366 stored events and 560 of
        # 1326 digest-eligible rows (42% of the pool) — and across 342 digest
        # sends, EXACTLY ZERO of them was ever published. The classifier's
        # relevance gate rejected every single one, after paying for the call
        # and after it had already displaced a real candidate from the top-N
        # ranking. Wire-formatted single-stock earnings from the X sources are
        # the bulk of it (EXPEDIA/COSTCO/FARADAY FUTURE lines score ~1.0 because
        # they're tier-2 and fresh, not because they matter to gold).
        # This only narrows the DIGEST pool. breaking/alert route on score and
        # are classified individually, so a genuinely important story with novel
        # vocabulary that lands in `other` still reaches LINE by that path.
        if str(row.get("topic_bucket") or "").strip() in excluded:
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
        # Content backstop: same headline already sent under a DIFFERENT event_id
        # (60-min bucket boundary → fresh id the event_id dedup can't see).
        if _content_already_sent(store, content_sig(title)):
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
