"""Normalize entries to the canonical schema and set first_seen_ts (freshness anchor)."""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from .utils_time import now_utc

log = logging.getLogger(__name__)

# Items older than this are dropped at normalize-time, BEFORE clustering /
# scoring / Claude classification. Cuts off the long tail of evergreen
# articles (e.g. "5 ways to protect savings" - 744d ago) before they cost
# us Claude tokens. The dedup window is 15min so anything past 48h could
# never join an active cluster anyway.
STALE_DROP_HOURS = 48


@dataclass
class Item:
    source_id: str
    tier: int
    role: str
    title: str
    summary: str
    url: str
    published_ts: datetime | None
    first_seen_ts: datetime  # set on first encounter; anchor for freshness
    source_class: str = "aggregator"   # legacy field — kept for backward compat
    organization: str = ""             # 2.3+: unique-org diversity for confirmation

    @property
    def url_hash(self) -> str:
        return hashlib.sha256(self.url.encode("utf-8", errors="replace")).hexdigest()[:16]


def normalize(entries: list[dict[str, Any]], stale_drop_hours: int = STALE_DROP_HOURS) -> list[Item]:
    """Convert raw fetched entries to canonical Items. Drops items whose
    published_ts is older than `stale_drop_hours` so the classifier never
    sees ancient evergreen content. Entries with no published_ts pass
    through (treated as fresh — RSS feeds without dates are usually live).
    """
    items: list[Item] = []
    anchor = now_utc()
    cutoff = anchor - timedelta(hours=stale_drop_hours)
    dropped_stale = 0
    for e in entries:
        if not e.get("url"):
            continue
        pub = e.get("published_ts")
        if pub is not None and pub < cutoff:
            dropped_stale += 1
            continue
        items.append(Item(
            source_id=e["source_id"],
            tier=int(e["tier"]),
            role=e["role"],
            title=e["title"],
            summary=e["summary"],
            url=e["url"],
            published_ts=pub,
            first_seen_ts=anchor,
            source_class=e.get("source_class", "aggregator"),
            organization=e.get("organization") or e["source_id"],
        ))
    if dropped_stale:
        log.info("normalize: dropped %d stale items (>%dh old)",
                 dropped_stale, stale_drop_hours)
    return items
