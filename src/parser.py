"""Wraps feedparser. Returns a list of raw entries per source."""
from __future__ import annotations

import html
import logging
from datetime import datetime, timezone
from typing import Any

import feedparser

log = logging.getLogger(__name__)


def parse_feed(body: bytes, source: dict[str, Any]) -> list[dict[str, Any]]:
    if not body:
        return []
    feed = feedparser.parse(body)
    entries: list[dict[str, Any]] = []
    for e in feed.entries:
        published = _entry_dt(e)
        entries.append({
            "source_id": source["id"],
            "tier": source["tier"],
            "role": source["role"],
            "source_class": source.get("source_class", "aggregator"),
            "organization": source.get("organization") or source["id"],
            "title": _clean_text((e.get("title") or "").strip()),
            "summary": _strip_html(e.get("summary") or e.get("description") or ""),
            "url": e.get("link") or "",
            "published_ts": published,  # datetime or None
        })
    if feed.bozo:
        log.warning("feedparser bozo source=%s reason=%s", source["id"], feed.bozo_exception)
    return entries


def _entry_dt(e: Any) -> datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        v = e.get(key)
        if v:
            try:
                return datetime(*v[:6], tzinfo=timezone.utc)
            except (TypeError, ValueError):
                continue
    return None


def _strip_html(s: str) -> str:
    # Lightweight strip — Phase 1 doesn't need full HTML parsing.
    import re
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = re.sub(r"\s+", " ", s).strip()
    return _clean_text(s)[:500]


def _clean_text(s: str) -> str:
    """Decode HTML entities (&amp; &#39; &quot; etc) so they render naturally."""
    if not s:
        return s
    # Two passes catch double-encoded entities seen in some RSS feeds.
    return html.unescape(html.unescape(s))
