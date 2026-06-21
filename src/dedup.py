"""Dedup / event clustering.

cluster_key = topic_bucket + entity + direction_label + time_window(15m).
event_id    = deterministic hash(cluster_key) — survives restarts.

Headline NOT in key (kills cross-source dedup if it were). Headline similarity
is a SECONDARY check only — used to reject low-confidence matches that share
topic+entity by coincidence.
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from typing import Any

from rapidfuzz import fuzz

from .normalizer import Item
from .utils_time import iso_utc, time_bucket

log = logging.getLogger(__name__)

# Phase 2.3 (2026-05-23): bumped from 15→60 min after observing ongoing
# stories (Israel-Iran, Fed speech with follow-ups) get split into two
# event_ids ~20 min apart, producing duplicate BREAKING pushes. 60 min
# is wide enough to absorb story development without over-merging
# unrelated CPI releases on the same day (different topic_bucket).
CLUSTER_WINDOW_MIN = 60
HEADLINE_SIM_FLOOR = 35


@dataclass
class Event:
    event_id: str
    cluster_key: str
    topic_bucket: str
    entity: str
    direction_label: str
    items: list[Item] = field(default_factory=list)

    @property
    def first_seen_ts(self):
        return min(i.first_seen_ts for i in self.items)

    @property
    def last_seen_ts(self):
        return max(i.first_seen_ts for i in self.items)

    @property
    def source_list(self) -> list[str]:
        seen, out = set(), []
        for it in self.items:
            if it.source_id not in seen:
                seen.add(it.source_id)
                out.append(it.source_id)
        return out

    @property
    def source_count(self) -> int:
        return len(self.source_list)

    @property
    def independent_source_count(self) -> int:
        """Count of DISTINCT organizations covering the event.

        Phase 2.3 (2026-05-23) — was previously source_class which
        collapsed every newsdesk into a single "aggregator" bucket.
        Now BBC + AlJazeera + CNBC + MarketWatch + Yahoo = 5 orgs,
        while Investing.com×3 endpoints = 1 org. Real confirmation
        diversity instead of pseudo-diversity.

        Falls back to source_id when organization is unset (e.g. older
        items still in event_state from before the schema bump)."""
        orgs = set()
        for i in self.items:
            org = (getattr(i, "organization", "") or "").strip()
            if not org:
                org = i.source_id
            orgs.add(org)
        return len(orgs)

    @property
    def representative_title(self) -> str:
        # Prefer Tier 0 → 1 → 2 → 3, then earliest first_seen.
        ranked = sorted(self.items, key=lambda i: (i.tier, i.first_seen_ts))
        return ranked[0].title if ranked else ""

    @property
    def representative_summary(self) -> str:
        # The highest-priority item that actually HAS a summary — the top item
        # is often an X tweet whose summary is "" even when an RSS source in the
        # same cluster carries the full text. Fall back to "" if none has one.
        ranked = sorted(self.items, key=lambda i: (i.tier, i.first_seen_ts))
        for it in ranked:
            if (it.summary or "").strip():
                return it.summary
        return ""

    @property
    def classify_summary(self) -> str:
        """Richer context for the classifier — up to 3 DISTINCT non-empty
        source summaries (priority order, ~800-char budget) joined together, so
        the model sees corroboration + extra detail instead of one source. The
        classifier prompt truncates further. Empty for tweet-only clusters."""
        ranked = sorted(self.items, key=lambda i: (i.tier, i.first_seen_ts))
        seen: set[str] = set()
        parts: list[str] = []
        total = 0
        for it in ranked:
            s = (it.summary or "").strip()
            if not s:
                continue
            norm = s[:120].lower()
            if norm in seen:
                continue
            seen.add(norm)
            parts.append(s)
            total += len(s)
            if len(parts) >= 3 or total > 800:
                break
        return "  ".join(parts)


def detect_topic_and_entity(text: str, kw_config: dict[str, Any]) -> tuple[str, str]:
    """Return (topic_bucket, entity).
    Topic: highest keyword-hit count; ties broken by base_impact desc (impactful wins).
    Entity: first entity from the chosen topic's entity list that appears in text;
    else the topic's primary entity."""
    t = text.lower()
    candidates: list[tuple[int, int, str]] = []  # (hits, base_impact, topic)
    for topic, cfg in kw_config["topics"].items():
        hits = sum(1 for kw in cfg["keywords"] if kw in t)
        if hits > 0:
            candidates.append((hits, int(cfg.get("base_impact", 0)), topic))
    if not candidates:
        return "other", "global"
    candidates.sort(key=lambda c: (-c[0], -c[1]))
    topic = candidates[0][2]
    ents = kw_config["topics"][topic].get("entities", ["global"])
    chosen_entity = next((e for e in ents if e in t), ents[0])
    return topic, chosen_entity


def detect_direction(text: str, kw_config: dict[str, Any]) -> str:
    """Keyword-based only in Phase 1. Default 'neutral'."""
    t = text.lower()
    scores: dict[str, int] = {}
    for direction, words in kw_config.get("direction_keywords", {}).items():
        scores[direction] = sum(1 for w in words if w in t)
    if not scores or max(scores.values()) == 0:
        return "neutral"
    return max(scores.items(), key=lambda kv: kv[1])[0]


def cluster_key_for(item: Item, kw_config: dict[str, Any]) -> tuple[str, str, str, str, str]:
    """Return (cluster_key, topic, entity, direction, bucket)."""
    text = f"{item.title} {item.summary}"
    topic, entity = detect_topic_and_entity(text, kw_config)
    direction = detect_direction(text, kw_config)
    anchor = item.published_ts or item.first_seen_ts
    bucket = time_bucket(anchor, CLUSTER_WINDOW_MIN)
    key = f"{topic}|{entity}|{direction}|{bucket}"
    return key, topic, entity, direction, bucket


def _event_id(cluster_key: str) -> str:
    return hashlib.sha256(cluster_key.encode("utf-8")).hexdigest()[:20]


def cluster(items: list[Item], kw_config: dict[str, Any]) -> list[Event]:
    """Group items into Events.

    Within the same cluster_key bucket, if a candidate item's headline is
    < HEADLINE_SIM_FLOOR similar to ANY existing item in the cluster AND there
    are already 2+ items from other sources, we open a NEW event with a key
    suffix (still deterministic) — this guards against accidental topic/entity
    collisions on unrelated stories.
    """
    by_key: dict[str, Event] = {}
    for it in items:
        ck, topic, entity, direction, _ = cluster_key_for(it, kw_config)
        existing = by_key.get(ck)
        if existing is None:
            ev = Event(event_id=_event_id(ck), cluster_key=ck,
                       topic_bucket=topic, entity=entity, direction_label=direction, items=[it])
            by_key[ck] = ev
            continue
        # Secondary headline-similarity check.
        if existing.source_count >= 2:
            max_sim = max(fuzz.token_set_ratio(it.title, x.title) for x in existing.items)
            if max_sim < HEADLINE_SIM_FLOOR:
                # Distinct story; open a sibling event with deterministic suffix.
                ck2 = f"{ck}#alt-{it.url_hash[:6]}"
                ev2 = Event(event_id=_event_id(ck2), cluster_key=ck2,
                            topic_bucket=topic, entity=entity, direction_label=direction, items=[it])
                by_key[ck2] = ev2
                continue
        existing.items.append(it)
    return list(by_key.values())


def serialize_event_for_store(ev: Event, score: float, status: str) -> dict[str, Any]:
    return {
        "event_id": ev.event_id,
        "cluster_key": ev.cluster_key,
        "topic_bucket": ev.topic_bucket,
        "entity": ev.entity,
        "direction_label": ev.direction_label,
        "first_seen_ts": iso_utc(ev.first_seen_ts),
        "last_seen_ts": iso_utc(ev.last_seen_ts),
        "source_list": ",".join(ev.source_list),
        "source_count": ev.source_count,
        "score": round(score, 3),
        "status": status,
    }
