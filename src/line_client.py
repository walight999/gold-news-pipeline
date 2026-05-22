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

# LINE free-tier limit. Counter resets on the 1st of every month (Asia/Bangkok).
LINE_FREE_TIER_QUOTA = 500
LINE_PUSH_SOURCE_ID = "_line_push"


def record_line_outcome(store, status_code: int) -> None:
    """Stamp the LINE push health row. Increments the monthly counter on
    success (status 200) and consecutive_errors on failure. Used by:
      - watchdog → line_push_failing warning (5+ consecutive failures)
      - watchdog → line_quota_high warning (>80% of monthly cap)
      - EOD recap → optional quota display
    """
    if store is None:
        return
    import json as _json
    from .utils_time import iso_utc, now_ict, now_utc
    ts = iso_utc(now_utc())
    cur_month = now_ict().strftime("%Y-%m")

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

    if status_code == 200:
        consec = 0
        counters["count"] = int(counters.get("count", 0)) + 1
        row["last_success_ts"] = ts
    else:
        consec += 1

    row["source_id"] = LINE_PUSH_SOURCE_ID
    row["last_attempt_ts"] = ts
    row["consecutive_errors"] = str(consec)
    row["items_last_hour"] = _json.dumps(counters)
    row["updated_at"] = ts
    store.upsert("source_state", row)


def get_line_quota_status(store) -> dict[str, int | str]:
    """Returns {"month": "YYYY-MM", "count": N, "limit": 500, "pct": int}.
    Used by watchdog + EOD recap. Empty dict if no row yet."""
    import json as _json
    if store is None:
        return {}
    row = store.get("source_state", (LINE_PUSH_SOURCE_ID,)) or {}
    blob = row.get("items_last_hour")
    if not blob:
        return {"month": "", "count": 0, "limit": LINE_FREE_TIER_QUOTA, "pct": 0}
    try:
        d = _json.loads(blob)
        if not isinstance(d, dict):
            return {}
    except (_json.JSONDecodeError, TypeError, ValueError):
        return {}
    count = int(d.get("count", 0))
    pct = int(count / LINE_FREE_TIER_QUOTA * 100) if LINE_FREE_TIER_QUOTA else 0
    return {"month": d.get("month", ""), "count": count,
            "limit": LINE_FREE_TIER_QUOTA, "pct": pct}


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
            return {"status": 0, "body": str(e)}

    def push(self, target: str, text: str) -> dict[str, Any]:
        return self._send(target, [{"type": "text", "text": text[:4900]}])

    def push_flex(self, target: str, alt_text: str, contents: dict[str, Any]) -> dict[str, Any]:
        """Send a Flex Message. `contents` is a bubble or carousel dict."""
        msg = {"type": "flex", "altText": alt_text[:400], "contents": contents}
        return self._send(target, [msg])
