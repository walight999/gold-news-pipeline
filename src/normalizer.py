"""Normalize entries to the canonical schema and set first_seen_ts (freshness anchor)."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .utils_time import now_utc


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
    source_class: str = "aggregator"   # Phase-2 independent_source_count input

    @property
    def url_hash(self) -> str:
        return hashlib.sha256(self.url.encode("utf-8", errors="replace")).hexdigest()[:16]


def normalize(entries: list[dict[str, Any]]) -> list[Item]:
    items: list[Item] = []
    anchor = now_utc()
    for e in entries:
        if not e.get("url"):
            continue
        items.append(Item(
            source_id=e["source_id"],
            tier=int(e["tier"]),
            role=e["role"],
            title=e["title"],
            summary=e["summary"],
            url=e["url"],
            published_ts=e.get("published_ts"),
            first_seen_ts=anchor,
            source_class=e.get("source_class", "aggregator"),
        ))
    return items
