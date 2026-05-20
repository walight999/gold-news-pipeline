"""Routing + rate-limit + LINE payload formatting.

Score → route:
  >= 4.5 : breaking (rate-limited unless always-pass)
  3.5–4.4: alert if official OR source_count >= 2 (rate-limited)
  2.5–3.4: digest
  1.5–2.4: archive only
  < 1.5  : ignore

Rate-limit: max 5 breaking+alert per 15m. Overflow → digest (NEVER dropped).
Always-pass:
  1) Tier 0 official scheduled release (CPI/NFP/FOMC)
  2) score-5 events that are "confirmed": official source OR source_count >= 2
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from enum import Enum
from typing import Any

from .dedup import Event
from .store import Store
from .utils_time import now_utc, parse_iso

log = logging.getLogger(__name__)

OFFICIAL_SOURCE_IDS = {"fed", "bls", "ecb", "treasury"}
SCHEDULED_RELEASE_TOPICS = {"inflation", "jobs", "rate_policy"}


class Route(str, Enum):
    BREAKING = "breaking"
    ALERT = "alert"
    DIGEST = "digest"
    ARCHIVE = "archive"
    IGNORE = "ignore"


@dataclass
class Decision:
    event: Event
    score: float
    route: Route
    reason: str
    always_pass: bool = False


def _is_official(ev: Event) -> bool:
    return any(sid in OFFICIAL_SOURCE_IDS for sid in ev.source_list)


def _is_confirmed(ev: Event) -> bool:
    """Phase 1 definition: official source OR source_count >= 2.
    NOTE: source_count counts feeds, not independent sources (syndication
    may inflate). Phase 2 replaces with independent_source_count."""
    return _is_official(ev) or ev.source_count >= 2


def base_route(ev: Event, score: float) -> tuple[Route, str]:
    if score >= 4.5:
        return Route.BREAKING, "score>=4.5"
    if score >= 3.5:
        if _is_official(ev) or ev.source_count >= 2:
            return Route.ALERT, "score>=3.5 & (official or confirmed)"
        return Route.ARCHIVE, "score>=3.5 but unconfirmed"
    if score >= 2.5:
        return Route.DIGEST, "2.5<=score<3.5"
    if score >= 1.5:
        return Route.ARCHIVE, "1.5<=score<2.5"
    return Route.IGNORE, "score<1.5"


def is_always_pass(ev: Event, score: float, scheduled_release_topics: set[str]) -> bool:
    # 1) Tier 0 official scheduled release
    if _is_official(ev) and ev.topic_bucket in scheduled_release_topics:
        return True
    # 2) score-5 confirmed
    if score >= 5.0 and _is_confirmed(ev):
        return True
    return False


def _count_recent_breaking_or_alert(store: Store, window_min: int) -> int:
    cutoff = now_utc() - timedelta(minutes=window_min)
    n = 0
    for row in store.all_rows("sent_log"):
        rt = row.get("route_type")
        if rt not in (Route.BREAKING.value, Route.ALERT.value):
            continue
        ts = parse_iso(row.get("sent_ts"))
        if ts and ts >= cutoff:
            n += 1
    return n


def decide(
    events: list[Event],
    scores: dict[str, float],
    store: Store,
    rate_limit_window_min: int = 15,
    rate_limit_max: int = 5,
) -> list[Decision]:
    decisions: list[Decision] = []
    used = _count_recent_breaking_or_alert(store, rate_limit_window_min)
    # Sort so always-pass events claim slots first.
    def _priority(ev: Event):
        s = scores.get(ev.event_id, 0.0)
        ap = is_always_pass(ev, s, SCHEDULED_RELEASE_TOPICS)
        return (0 if ap else 1, -s, -ev.source_count)

    for ev in sorted(events, key=_priority):
        s = scores.get(ev.event_id, 0.0)
        base, reason = base_route(ev, s)
        ap = is_always_pass(ev, s, SCHEDULED_RELEASE_TOPICS)
        if base in (Route.BREAKING, Route.ALERT):
            if ap:
                decisions.append(Decision(ev, s, base, f"{reason}; always-pass", always_pass=True))
                # always-pass bypasses cap (does not consume a slot for others either)
                continue
            if used >= rate_limit_max:
                decisions.append(Decision(ev, s, Route.DIGEST,
                                          f"{reason}; rate-limit overflow → digest"))
                continue
            used += 1
            decisions.append(Decision(ev, s, base, reason))
        else:
            decisions.append(Decision(ev, s, base, reason))
    return decisions


# ---------- Formatting ----------

def _map_name(text: str, name_map: dict[str, str]) -> str:
    out = text
    for en, th in (name_map or {}).items():
        # Only suffix in parens the first time the en token appears.
        idx = out.lower().find(en.lower())
        if idx >= 0:
            out = out[: idx + len(en)] + f" ({th})" + out[idx + len(en) :]
            break
    return out


def format_breaking(ev: Event, score: float, kw_config: dict[str, Any]) -> str:
    nm = kw_config.get("name_map", {})
    title = _map_name(ev.representative_title, nm)
    summary = ev.representative_summary[:240]
    src = ",".join(ev.source_list)
    return (
        f"⚡ BREAKING — score {score:.1f}\n"
        f"{title}\n"
        f"- {summary}\n"
        f"- Source: {src}"
    )


def format_alert(ev: Event, score: float, kw_config: dict[str, Any]) -> str:
    nm = kw_config.get("name_map", {})
    title = _map_name(ev.representative_title, nm)
    summary = ev.representative_summary[:240]
    src = ",".join(ev.source_list)
    return (
        f"🔔 ALERT — score {score:.1f}\n"
        f"{title}\n"
        f"- {summary}\n"
        f"- Source: {src}"
    )
