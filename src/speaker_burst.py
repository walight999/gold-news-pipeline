"""One breaking card per central-bank speaker burst (2026-11-03).

A press conference or speech reaches the wires as a run of "Speaker: remark"
headlines a minute apart, and each one routed to its own breaking card:
Fed's Musalem 5 cards in 1 min (10-30), RBA 4 + Bullock 3 (11-03), BoJ Ueda 3,
BoK 3. breaking/alert only drop near-identical headlines (digest.same_story
near_only) because a NEW remark can be real news — so the fix here is to
bundle, not to drop:

  * same run: remarks from one speaker go out as ONE card; the extra headlines
    ride along in the classifier input so the Thai card covers all of them.
    The absorbed events get a sent_log row with route_type "merged" (not
    "breaking", so they don't eat the router's rate-limit slots).
  * later runs: a speaker who already got a card in the last `cooldown_min`
    minutes doesn't get another breaking card; the remark stays in
    event_state and the digest pool picks it up (subject to story dedup).

Only prefixes naming a central bank count. "Trump:" lines span unrelated
stories, and plain data lines ("US CPI: 2.8%") are left as they are.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any

from .store import Store
from .utils_time import parse_iso

MERGED_ROUTE = "merged"

# Institution token (lowercased) → canonical key prefix.
_INSTITUTIONS = {
    "fed": "fed", "fomc": "fed", "ecb": "ecb", "boj": "boj", "boe": "boe",
    "bok": "bok", "rba": "rba", "rbnz": "rbnz", "snb": "snb", "boc": "boc",
    "pboc": "pboc", "riksbank": "riksbank", "norges": "norges", "rbi": "rbi",
    "bundesbank": "bundesbank",
}
_LONG_NAMES = (
    ("reserve bank of australia", "RBA"), ("reserve bank of new zealand", "RBNZ"),
    ("federal reserve", "Fed"), ("bank of japan", "BoJ"), ("bank of england", "BoE"),
    ("bank of canada", "BoC"), ("bank of korea", "BoK"), ("swiss national bank", "SNB"),
)
_PREFIX_RE = re.compile(r"^\s*([^:]{2,60}?)\s*:\s+\S")
_TOKEN_RE = re.compile(r"[A-Za-z]+(?:'s)?")
_MAX_PREFIX_WORDS = 6


def speaker_key(title: str) -> str | None:
    """'fed:musalem' for "Fed's Musalem: …", 'rba' for "RBA: …",
    'rba:bullock' for both "RBA Governor Bullock: …" and "RBA's Bullock: …".
    None when the headline isn't a central-bank "Speaker: remark" line."""
    m = _PREFIX_RE.match((title or "").replace("’", "'"))
    if not m:
        return None
    prefix = m.group(1)
    for long, short in _LONG_NAMES:
        prefix = re.sub(re.escape(long), short, prefix, flags=re.IGNORECASE)
    if any(ch.isdigit() for ch in prefix):
        return None
    words = _TOKEN_RE.findall(prefix)
    if not words or len(words) > _MAX_PREFIX_WORDS:
        return None
    norm = [w[:-2] if w.lower().endswith("'s") else w for w in words]
    inst = next((_INSTITUTIONS[w.lower()] for w in norm if w.lower() in _INSTITUTIONS), None)
    if inst is None:
        return None
    last = norm[-1].lower()
    if last in _INSTITUTIONS:
        return inst
    return f"{inst}:{last}"


def recent_speaker_keys(store: Store, now: datetime, minutes: float) -> set[str]:
    """Speaker keys of breaking / alert / merged cards sent in the last
    `minutes` (sent_log has the ids, event_state the titles)."""
    cutoff = now - timedelta(minutes=minutes)
    keys: set[str] = set()
    for r in store.all_rows("sent_log"):
        if str(r.get("route_type")) not in ("breaking", "alert", MERGED_ROUTE):
            continue
        ts = parse_iso(r.get("sent_ts"))
        if ts is None or ts < cutoff:
            continue
        row = store.get("event_state", (str(r.get("event_id") or ""),))
        k = speaker_key(str((row or {}).get("title") or ""))
        if k:
            keys.add(k)
    return keys


def plan(candidates: list[tuple[str, str]], cooldown_keys: set[str]
         ) -> tuple[dict[str, list[str]], dict[str, str], set[str]]:
    """Split this run's breaking/alert candidates [(event_id, title)], in
    send order, into:
      extras    {primary_id: [absorbed ids]}   — primary carries the others
      absorbed  {absorbed_id: primary_id}
      deferred  {event_id}                     — speaker still in cooldown
    Candidates without a speaker key aren't touched."""
    extras: dict[str, list[str]] = {}
    absorbed: dict[str, str] = {}
    deferred: set[str] = set()
    primary_of: dict[str, str] = {}
    for eid, title in candidates:
        k = speaker_key(title)
        if not k:
            continue
        if k in cooldown_keys:
            deferred.add(eid)
        elif k in primary_of:
            p = primary_of[k]
            extras.setdefault(p, []).append(eid)
            absorbed[eid] = p
        else:
            primary_of[k] = eid
    return extras, absorbed, deferred


def merged_summary(summary: str, extra_titles: list[str]) -> str:
    """Classifier input for a bundled card: the primary's summary plus the
    other remarks from the same speaker."""
    if not extra_titles:
        return summary
    lines = "\n".join(f"- {t}" for t in extra_titles)
    head = (summary or "").strip()
    return (f"{head}\n\n" if head else "") + f"More remarks from the same speaker in this release:\n{lines}"


def merged_row(event_id: str, primary_id: str, sent_ts: str) -> dict[str, Any]:
    return {"event_id": event_id, "route_type": MERGED_ROUTE, "sent_ts": sent_ts,
            "line_status": f"into:{primary_id}"}
