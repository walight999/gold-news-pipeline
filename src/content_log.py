"""Content quality ledger — every news card the pipeline sends (and every
digest candidate the classifier rejects) lands as one reviewable row in the
`content_log` worksheet, with blank feedback columns the operator fills in.

Why this exists (2026-08-13): months of operation produced zero record of what
the channel actually SAID. sent_log keeps only event_id + status; social_feed
keeps Thai copy but only for delivered cards, mixed into the X publishing
queue, with no room for corrections. So there was nowhere to note "this
headline had a typo", "this translation missed the name", "this verdict was
wrong" — and no visibility into what the relevance gate silently dropped,
which is exactly the operator's fear when a new filter ships.

Design:
- Append-only via Store.append_feed (same pattern as social_feed): flush()
  clears+rewrites SCHEMAS tabs, which would clobber the operator's feedback —
  this tab is never loaded, never rewritten, rows only accumulate.
- `decision` = sent | rejected. Rejected rows cost nothing extra — the
  classifier call already happened; logging the outcome makes the filter
  auditable from the sheet instead of buried in Actions logs.
- Feedback columns (operator-owned, pipeline always writes ""):
    fb_ok    — y = card was fine / n = something wrong (blank = not reviewed)
    fb_type  — typo | translation | tone | display | impact | irrelevant |
               missed (for rejected rows that SHOULD have been sent) | other
    fb_fix   — the corrected text, when the fix is worth writing out
    fb_note  — free text
- Impact outcome is NOT duplicated here: join event_id → calibration_log
  (xau_return_5m/15m/30m) when analyzing. One source of truth per number.
- Best-effort like social_feed: a Sheets hiccup here must never fail the run.
"""
from __future__ import annotations

from typing import Any

from .utils_time import now_utc, to_ict

CONTENT_TAB = "content_log"
CONTENT_HEADERS = [
    "ts_ict", "route", "decision", "event_id", "topic_bucket", "score",
    "category", "tone", "impact_level", "headline_th", "body_th", "impact_th",
    "en_title", "source", "flags",
    "fb_ok", "fb_type", "fb_fix", "fb_note",
]

# body_th bullets are joined for the cell; keep the joiner unambiguous so an
# analysis pass can split them back.
_BULLET_JOIN = " | "


def record_card(*, route: str, decision: str, event_id: str,
                topic_bucket: str = "", score: float | str = "",
                category: str = "", tone: str = "", impact_level: str = "",
                headline_th: str | None = None,
                body_th: list[str] | None = None,
                impact_th: str | None = None,
                en_title: str = "", source: str = "",
                flags: str = "") -> dict[str, Any]:
    """Build one content_log row. `flags` carries pipeline-side context the
    operator should see when judging the card: `fallback` (literal translate),
    `degraded` (whole-round outage mode), `rejected:<reason>`."""
    return {
        "ts_ict": to_ict(now_utc()).strftime("%Y-%m-%d %H:%M:%S"),
        "route": route,
        "decision": decision,
        "event_id": event_id or "",
        "topic_bucket": topic_bucket or "",
        "score": score,
        "category": category or "",
        "tone": tone or "",
        "impact_level": impact_level or "",
        "headline_th": (headline_th or "").strip(),
        "body_th": _BULLET_JOIN.join(
            s for b in (body_th or []) if (s := (b or "").strip())),
        "impact_th": (impact_th or "").strip(),
        "en_title": (en_title or "").strip(),
        "source": source or "",
        "flags": flags or "",
        "fb_ok": "", "fb_type": "", "fb_fix": "", "fb_note": "",
    }


def _to_row(rec: dict[str, Any]) -> list[Any]:
    return [rec.get(c, "") for c in CONTENT_HEADERS]


def flush(store, records: list[dict[str, Any]]) -> int:
    """Append collected records. Never raises — the ledger is secondary to the
    news push. Returns rows appended (0 on no-op/error)."""
    if not records:
        return 0
    try:
        store.append_feed(CONTENT_TAB, CONTENT_HEADERS, [_to_row(r) for r in records])
        return len(records)
    except Exception:  # noqa: BLE001 — ledger is best-effort by design
        import logging
        logging.getLogger("content_log").exception("content_log append failed")
        return 0
