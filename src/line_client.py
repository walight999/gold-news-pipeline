"""LINE Messaging API push with retry/backoff + quota tracking.

Idempotency contract:
  - Caller checks sent_log BEFORE invoking push.
  - On failure (after retries) we DO NOT mark as sent — so the next run retries.

Health tracking:
  - record_line_outcome() stamps the `_line_push` row in source_state with
    consecutive_errors + monthly quota counter so the watchdog can detect
    silent LINE outages and 500-msg/month free-tier exhaustion.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

import httpx
from tenacity import RetryError, retry, stop_after_attempt, wait_exponential

log = logging.getLogger(__name__)

_PUSH_URL = "https://api.line.me/v2/bot/message/push"
_QUOTA_URL = "https://api.line.me/v2/bot/message/quota"
_QUOTA_CONSUMPTION_URL = "https://api.line.me/v2/bot/message/quota/consumption"

# Fallback monthly cap when LINE's own quota API hasn't been fetched yet. The
# REAL limit + usage come from refresh_line_quota_from_api (LINE is authoritative);
# this 500 is only used before the first successful API refresh. Counter resets on
# the 1st of every month (Asia/Bangkok).
LINE_FREE_TIER_QUOTA = 500
# How long a fetched LINE-API quota reading stays authoritative before
# get_line_quota_status falls back to the local estimate.
_QUOTA_API_FRESH_HOURS = 6
LINE_PUSH_SOURCE_ID = "_line_push"


def record_line_outcome(store, resp) -> None:
    """Stamp the LINE push health row. `resp` is the dict returned by
    push/push_flex (carries "status" and, for multi-recipient sends, a
    per-target "results" list); a bare int status is also accepted for
    back-compat.

    Counts the monthly quota per RECIPIENT that actually received the message.
    A multi-recipient broadcast consumes one quota unit per recipient, not 1
    per call — the old +1-per-call counter ran at ~half the real usage and
    would fire the 80% alarm far too late. (News sends are group-only since
    2026-07-18 — see main._group_targets — so they now bill 1.) Used by:
      - watchdog → line_push_failing warning (5+ consecutive total failures)
      - watchdog → line_quota_high warning (>80% of monthly cap)
      - EOD recap → optional quota display
    """
    if store is None:
        return
    import json as _json
    from .utils_time import iso_utc, now_ict, now_utc
    ts = iso_utc(now_utc())
    cur_month = now_ict().strftime("%Y-%m")

    # Status + how many recipients actually got it.
    if isinstance(resp, dict):
        status_code = int(resp.get("status", 0) or 0)
        results = resp.get("results")
        if results:
            success_n = sum(1 for r in results if r.get("status") == 200)
        else:
            success_n = 1 if status_code == 200 else 0
    else:
        status_code = int(resp or 0)
        success_n = 1 if status_code == 200 else 0

    row = store.get("source_state", (LINE_PUSH_SOURCE_ID,)) or {"source_id": LINE_PUSH_SOURCE_ID}
    consec = int(row.get("consecutive_errors") or 0)
    blob = row.get("items_last_hour")
    counters: dict = {}
    if blob:
        try:
            d = _json.loads(blob)
            if isinstance(d, dict):
                counters = d
        except (_json.JSONDecodeError, TypeError, ValueError):
            counters = {}

    # Monthly counter — auto-resets when the month rolls over.
    if counters.get("month") != cur_month:
        counters = {"month": cur_month, "count": 0}

    if success_n > 0:
        # At least one recipient delivered → LINE is alive; reset the failure
        # streak and bill each recipient that received the message.
        consec = 0
        counters["count"] = int(counters.get("count", 0)) + success_n
        row["last_success_ts"] = ts
    else:
        consec += 1

    row["source_id"] = LINE_PUSH_SOURCE_ID
    row["last_attempt_ts"] = ts
    row["consecutive_errors"] = str(consec)
    # Remember the last HTTP status so the watchdog can say WHY LINE is failing:
    # 429 = monthly quota exhausted (recovers on the 1st), 401/403 = token /
    # channel broken (needs a human). 0 = transport error (network/timeout).
    row["last_status"] = str(status_code)
    row["items_last_hour"] = _json.dumps(counters)
    row["updated_at"] = ts
    store.upsert("source_state", row)


def refresh_line_quota_from_api(store, token: str, timeout: float = 10.0) -> None:
    """Cache LINE's AUTHORITATIVE monthly quota + usage in the _line_push row so
    the health alert + quota gate reflect what LINE actually bills — not a local
    per-recipient estimate against a hardcoded 500.

    The local counter (record_line_outcome) can't match LINE in either direction:
    a push to a group is billed by LINE per member, and it never knew the real
    plan cap. That mismatch fired line_quota_high at "708/500 (141%)" while LINE's
    own API reported ~68% of a 35,000 plan. Best-effort — any error leaves the
    previously-cached values (or the local fallback) in place."""
    if store is None or not token:
        return
    import json as _json
    from .utils_time import iso_utc, now_utc
    headers = {"Authorization": f"Bearer {token}"}
    try:
        with httpx.Client(timeout=timeout) as c:
            q = c.get(_QUOTA_URL, headers=headers); q.raise_for_status()
            u = c.get(_QUOTA_CONSUMPTION_URL, headers=headers); u.raise_for_status()
        qd, ud = q.json(), u.json()
    except (httpx.HTTPError, ValueError) as e:  # network / auth / bad JSON
        log.warning("line quota API refresh failed: %s", e)
        return
    # type "limited" → value is the cap; "none" → unlimited plan (never alarm).
    limit = int(qd.get("value", 0)) if qd.get("type") == "limited" else 0
    usage = int(ud.get("totalUsage", 0))
    # COPY the row before mutating — store.get() hands back the LIVE dict in
    # store.data, and Store.upsert's no-op guard compares the incoming row to
    # that same object. Mutating it in place makes the guard see "no change", so
    # flush() skips the tab and the reading never persists (the #78 trap, 2026-08-13).
    row = dict(store.get("source_state", (LINE_PUSH_SOURCE_ID,)) or {"source_id": LINE_PUSH_SOURCE_ID})
    counters: dict = {}
    blob = row.get("items_last_hour")
    if blob:
        try:
            d = _json.loads(blob)
            if isinstance(d, dict):
                counters = d
        except (_json.JSONDecodeError, TypeError, ValueError):
            counters = {}
    counters["api_limit"] = limit
    counters["api_usage"] = usage
    counters["api_ts"] = iso_utc(now_utc())
    row["source_id"] = LINE_PUSH_SOURCE_ID
    row["items_last_hour"] = _json.dumps(counters)
    row["updated_at"] = iso_utc(now_utc())
    store.upsert("source_state", row)


def get_line_quota_status(store) -> dict[str, int | str]:
    """Returns {"month", "count", "limit", "pct", "source"}. Prefers LINE's own
    quota API reading (cached by refresh_line_quota_from_api, `source="api"`) and
    falls back to the local per-recipient estimate vs LINE_FREE_TIER_QUOTA
    (`source="local"`) when no fresh API reading exists. Empty dict if no row."""
    import json as _json
    from datetime import timedelta

    from .utils_time import now_utc, parse_iso
    if store is None:
        return {}
    row = store.get("source_state", (LINE_PUSH_SOURCE_ID,)) or {}
    blob = row.get("items_last_hour")
    if not blob:
        return {"month": "", "count": 0, "limit": LINE_FREE_TIER_QUOTA, "pct": 0, "source": "local"}
    try:
        d = _json.loads(blob)
        if not isinstance(d, dict):
            return {}
    except (_json.JSONDecodeError, TypeError, ValueError):
        return {}
    # Authoritative path: a recent LINE-API reading.
    api_ts = parse_iso(d.get("api_ts"))
    if api_ts and (now_utc() - api_ts) < timedelta(hours=_QUOTA_API_FRESH_HOURS):
        usage = int(d.get("api_usage") or 0)
        limit = int(d.get("api_limit") or 0)
        if limit <= 0:   # "none" = unlimited plan → never alarm
            return {"month": d.get("month", ""), "count": usage, "limit": 0, "pct": 0, "source": "api"}
        pct = int(usage / limit * 100)
        return {"month": d.get("month", ""), "count": usage, "limit": limit, "pct": pct, "source": "api"}
    # Fallback: local per-recipient estimate against the assumed free tier.
    count = int(d.get("count", 0))
    pct = int(count / LINE_FREE_TIER_QUOTA * 100) if LINE_FREE_TIER_QUOTA else 0
    return {"month": d.get("month", ""), "count": count,
            "limit": LINE_FREE_TIER_QUOTA, "pct": pct, "source": "local"}


# Quota-aware sender priority. LOWER number = higher value = shed LAST. The
# quota gate (quota_allows) sheds from the bottom up as usage climbs, so a
# scarce free tier spends its last messages on breaking/alert, not T-15 cards.
PRIORITY_CRITICAL = 0   # breaking / alert — never gated (also the recovery probe)
PRIORITY_CORE = 1       # digest / Released-News / eod_recap / scorecard / health
PRIORITY_BRIEFING = 2   # calendar_daily / weekly_preview (redundant with the above)
PRIORITY_REDUNDANT = 3  # calendar T-15 pre-release (calendar_daily + Released cover it)


def quota_allows(store, priority: int) -> tuple[bool, str]:
    """Should a push of this priority go out given current LINE quota state?

    Two independent signals, because the local monthly counter is unreliable on
    its own: it counts only DELIVERED messages, so during a 429 storm it stops
    climbing and can read far below LINE's server-side cap. The authoritative
    "we are out" signal is a real 429.

      - Hard exhaustion: last push got HTTP 429 THIS ICT month (the free tier
        resets on the 1st, so a July 429 must not gate August — we compare the
        counter's month, which is stamped on every attempt). Shed everything
        except breaking/alert: the rest would only burn ~15s of doomed retries,
        and the rare breaking/alert attempts double as the recovery probe that
        flips last_status back to 200.
      - Soft pressure: the local counter is climbing. Shed briefings at >=90%,
        the redundant T-15 cards at >=80% — early, before a hard 429.

    Returns (allowed, reason). Fail-open (allow) when store is None."""
    if store is None:
        return True, ""
    row = store.get("source_state", (LINE_PUSH_SOURCE_ID,)) or {}
    last_status = str(row.get("last_status") or "0")
    qs = get_line_quota_status(store)
    pct = int(qs.get("pct", 0) or 0)
    from .utils_time import now_ict
    cur_month = now_ict().strftime("%Y-%m")
    exhausted = last_status == "429" and qs.get("month") == cur_month
    if exhausted and priority >= PRIORITY_CORE:
        return False, "LINE quota exhausted (429) this month — conserving until reset"
    if pct >= 90 and priority >= PRIORITY_BRIEFING:
        return False, f"LINE quota {pct}% — shedding briefings to protect breaking/alert"
    if pct >= 80 and priority >= PRIORITY_REDUNDANT:
        return False, f"LINE quota {pct}% — shedding T-15 pre-release"
    return True, ""


def _split_targets(target: str | list[str] | None) -> list[str]:
    """Parse a comma-separated string or list into a clean dedup'd target list.

    Accepts:
      - "U123..."                          → ["U123..."]
      - "U123...,C456...,C789..."          → ["U123...", "C456...", "C789..."]
      - ["U123...", "C456..."]             → ["U123...", "C456..."]
      - "" / None / []                     → []

    Whitespace around each entry is stripped. Duplicate IDs are kept once
    (first occurrence wins). Used by LineClient.push / push_flex to support
    multi-recipient delivery from a single LINE_NEWS_TARGET env var.
    """
    if not target:
        return []
    items: list[str]
    if isinstance(target, str):
        items = [t.strip() for t in target.split(",")]
    else:
        items = [str(t).strip() for t in target]
    seen: set[str] = set()
    out: list[str] = []
    for t in items:
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out


@dataclass
class LineClient:
    token: str

    @classmethod
    def from_env(cls) -> "LineClient":
        return cls(token=os.environ["LINE_CHANNEL_TOKEN"])

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10),
           reraise=True)
    def _post_messages(self, target: str, messages: list[dict[str, Any]]) -> dict[str, Any]:
        payload = {"to": target, "messages": messages}
        headers = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}
        with httpx.Client(timeout=15.0) as c:
            r = c.post(_PUSH_URL, json=payload, headers=headers)
        if r.status_code >= 500 or r.status_code == 429:
            raise httpx.HTTPStatusError(f"LINE {r.status_code}", request=r.request, response=r)
        return {"status": r.status_code, "body": r.text}

    def _send(self, target: str, messages: list[dict[str, Any]]) -> dict[str, Any]:
        try:
            return self._post_messages(target, messages)
        except RetryError as e:
            log.warning("line push failed after retries: %s", e)
            return {"status": 0, "body": "retry_exhausted"}
        except httpx.HTTPError as e:
            log.warning("line push http error: %s", e)
            # Preserve the real HTTP status (429 = monthly quota exhausted,
            # 401 = token expired, 403 = channel disabled). Callers/health can
            # then tell "quota, resets on the 1st" apart from "auth broken",
            # instead of a blind status-0. HTTPStatusError carries .response;
            # transport errors (no response) stay status 0.
            resp = getattr(e, "response", None)
            status = getattr(resp, "status_code", 0) if resp is not None else 0
            return {"status": status, "body": str(e)}

    def _broadcast(self, target: str | list[str] | None,
                   messages: list[dict[str, Any]]) -> dict[str, Any]:
        """Send `messages` to one or many targets.

        Returns the original {"status", "body"} shape for backward compat.
        When multiple targets are given, aggregates:
          - status: 200 if ALL targets returned 200, else the first non-200 seen
          - body:   "multi:<ok>/<total>_ok" summary
          - results: list of per-target outcomes (extra field, ignored by old callers)

        Single-target callers see identical behaviour to the old _send().
        """
        targets = _split_targets(target)
        if not targets:
            return {"status": 0, "body": "no_targets"}
        if len(targets) == 1:
            return self._send(targets[0], messages)
        results: list[dict[str, Any]] = []
        worst_status = 200
        ok_count = 0
        for t in targets:
            r = self._send(t, messages)
            results.append({"to": t, **r})
            if r["status"] == 200:
                ok_count += 1
            elif worst_status == 200:
                worst_status = r["status"]
        return {
            "status": 200 if ok_count == len(targets) else worst_status,
            "body": f"multi:{ok_count}/{len(targets)}_ok",
            "results": results,
        }

    def push(self, target: str | list[str], text: str) -> dict[str, Any]:
        return self._broadcast(target, [{"type": "text", "text": text[:4900]}])

    def push_flex(self, target: str | list[str], alt_text: str,
                  contents: dict[str, Any]) -> dict[str, Any]:
        """Send a Flex Message. `contents` is a bubble or carousel dict."""
        msg = {"type": "flex", "altText": alt_text[:400], "contents": contents}
        return self._broadcast(target, [msg])
