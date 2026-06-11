"""Social feed export — every NEWS push also lands as a row in a Google Sheet
worksheet (`social_feed`) carrying structured fields + a ready-to-post Thai
tweet draft, so a downstream autopost (Make / Zapier → X/Twitter) can consume it.

Scope: NEWS only (breaking / alert / digest a.k.a. newsupdate / eod recap).
Calendar + upcoming are intentionally excluded (owned by the GAS bot, and less
suited to social).

Design notes:
- Append-only. The state Store.flush() clears+rewrites whole tabs, which would
  clobber an external `posted` flag and break "watch new rows" automations —
  so the feed uses Store.append_feed() (native append, never clears).
- The tweet draft is a DRAFT for review, not auto-fired. The `posted` column is
  left blank for the autopost step to stamp.
- The draft obeys the no-ai-slop discipline for public copy: no em-dash, exactly
  one direction emoji, no AI-isms, source attribution, factual.
"""
from __future__ import annotations

import re
from typing import Any

from .utils_time import iso_utc, now_utc, to_ict

FEED_TAB = "social_feed"
FEED_HEADERS = [
    "ts_utc", "ts_ict", "type", "category", "tone", "impact_level",
    "headline_th", "summary_th", "impact_th", "source", "url",
    "tweet_text", "approved", "posted",
]
# Approval flow: `approved` is the human gate — left blank by the pipeline; the
# operator types "yes" in the Sheet to release a draft. The Make autopost
# scenario searches for approved=yes AND posted empty, posts to X, then stamps
# `posted`. So nothing reaches Twitter without an explicit per-row yes.

TAGS = "#ทองคำ #XAUUSD"
TWEET_LIMIT = 280
URL_WEIGHT = 23          # Twitter wraps any URL to a fixed 23-char t.co cost.

# Gold-context tone → single direction emoji. dovish / risk-off lift gold;
# hawkish / risk-on weigh on it. Exactly one emoji per tweet (no-ai-slop).
_BULLISH_TONES = {"dovish", "risk_off"}
_BEARISH_TONES = {"hawkish", "risk_on"}

_TOPIC_TH = {
    "inflation": "เงินเฟ้อ",
    "jobs": "การจ้างงาน",
    "rate_policy": "ดอกเบี้ย",
    "geopolitics": "ภูมิรัฐศาสตร์",
    "usd_yields": "ดอลลาร์/บอนด์",
    "gold_flow": "ฟันด์โฟลว์ทอง",
}


def _tone_emoji(tone: str) -> str:
    t = (tone or "neutral").lower()
    if t in _BULLISH_TONES:
        return "🟢"
    if t in _BEARISH_TONES:
        return "🔴"
    return "🟡"


def _sanitize(text: str) -> str:
    """Strip the top AI-slop tells from generated Thai copy before it ships as
    a public draft: em/en dashes → space, collapse whitespace."""
    s = (text or "").replace("—", " ").replace("–", " ").replace("―", " ")
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _trim(s: str, n: int) -> str:
    s = (s or "").strip()
    if n <= 1:
        return ""
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def build_tweet(headline_th: str | None, impact_th: str | None,
                source: str, url: str, tone: str) -> str:
    """Assemble a ≤280-char Thai tweet draft. URL costs a fixed 23 chars
    (t.co); Thai characters count as 1 each."""
    emoji = _tone_emoji(tone)
    head = _sanitize(headline_th or "")
    impact = _sanitize(impact_th or "")
    src = _sanitize(source or "")

    url = (url or "").strip()
    url_cost = (URL_WEIGHT + 1) if url else 0          # +1 newline
    footer = TAGS + (f" · {src}" if src else "")
    footer_cost = len(footer) + 2                       # +2 blank line
    budget = TWEET_LIMIT - url_cost - footer_cost

    block = (emoji + " " + head).strip() if head else emoji
    if impact:
        if len(block) + 2 + len(impact) <= budget:
            block = block + "\n\n" + impact
        else:
            room = budget - len(block) - 2
            if room > 12:
                block = block + "\n\n" + _trim(impact, room)
    if len(block) > budget:
        block = _trim(block, budget)

    tweet = block + "\n\n" + footer
    if url:
        tweet += "\n" + url
    return tweet


def record_news_event(*, route: str, category: str, tone: str,
                      impact_level: str, headline_th: str | None,
                      body_th: list[str] | None, impact_th: str | None,
                      source: str, url: str) -> dict[str, Any]:
    """Build a feed record for a single pushed breaking/alert/digest event."""
    now = now_utc()
    summary_th = _sanitize(" ".join(body_th or []))
    tweet = build_tweet(headline_th, impact_th, source, url, tone)
    return {
        "ts_utc": iso_utc(now),
        "ts_ict": to_ict(now).strftime("%Y-%m-%d %H:%M:%S"),
        "type": route,
        "category": category or "",
        "tone": tone or "",
        "impact_level": impact_level or "",
        "headline_th": _sanitize(headline_th or ""),
        "summary_th": summary_th,
        "impact_th": _sanitize(impact_th or ""),
        "source": source or "",
        "url": url or "",
        "tweet_text": tweet,
        "approved": "",
        "posted": "",
    }


def record_recap(stats: dict[str, Any], date_label: str) -> dict[str, Any]:
    """Build a single end-of-day recap feed record."""
    now = now_utc()
    top_topics = stats.get("top_topics") or []
    top_th = _TOPIC_TH.get(top_topics[0][0], top_topics[0][0]) if top_topics else ""
    b = stats.get("breaking_n", 0)
    a = stats.get("alert_n", 0)
    headline = f"สรุปข่าวทอง {date_label}"
    parts = [f"Breaking {b}", f"Alert {a}"]
    if top_th:
        parts.append(f"เด่น: {top_th}")
    impact = " · ".join(parts)
    tweet = build_tweet(f"📊 {headline}", impact, "", "", "neutral")
    return {
        "ts_utc": iso_utc(now),
        "ts_ict": to_ict(now).strftime("%Y-%m-%d %H:%M:%S"),
        "type": "recap",
        "category": "Daily Recap",
        "tone": "neutral",
        "impact_level": "",
        "headline_th": headline,
        "summary_th": impact,
        "impact_th": "",
        "source": "",
        "url": "",
        "tweet_text": tweet,
        "approved": "",
        "posted": "",
    }


def _to_row(rec: dict[str, Any]) -> list[Any]:
    return [rec.get(c, "") for c in FEED_HEADERS]


def flush(store, records: list[dict[str, Any]]) -> int:
    """Append the collected records to the social_feed worksheet. Never raises
    — the feed is secondary to the LINE push, so a Sheets hiccup here must not
    fail the news run. Returns the number of rows appended (0 on no-op/error)."""
    if not records:
        return 0
    try:
        store.append_feed(FEED_TAB, FEED_HEADERS, [_to_row(r) for r in records])
        return len(records)
    except Exception:  # noqa: BLE001 — feed is best-effort by design
        import logging
        logging.getLogger("social_feed").exception("social_feed append failed")
        return 0


# ---------------------------------------------------------------------------
# Posting side — pipeline posts approved drafts straight to X (Make has no
# native X connector). Operator gates each row by typing yes in `approved`.
# ---------------------------------------------------------------------------

_YES = {"yes", "y", "true", "1", "✓", "approve", "approved"}


def _is_yes(v: Any) -> bool:
    return str(v or "").strip().lower() in _YES


def x_post(text: str) -> str:
    """Post a single tweet via the X API v2 (user-context OAuth 1.0a) and return
    its URL. Requires the 4 X app credentials in env. Raises on failure so the
    caller can leave the row unposted for the next run to retry."""
    import os
    import tweepy  # lazy — only needed in the social_post run

    client = tweepy.Client(
        consumer_key=os.environ["X_API_KEY"],
        consumer_secret=os.environ["X_API_SECRET"],
        access_token=os.environ["X_ACCESS_TOKEN"],
        access_token_secret=os.environ["X_ACCESS_TOKEN_SECRET"],
    )
    resp = client.create_tweet(text=text)
    tweet_id = resp.data["id"]
    return f"https://x.com/i/web/status/{tweet_id}"


def post_pending(store, poster=x_post, limit: int = 5) -> int:
    """Find rows where `approved` is yes AND `posted` is empty, post each via
    `poster(text)`, and write the returned URL back into `posted`. Per-row
    failures are logged and left unposted (retried next run). Returns the count
    actually posted. `limit` caps posts per run (X free-tier friendliness)."""
    import logging
    log = logging.getLogger("social_feed")

    headers, rows = store.read_feed(FEED_TAB)
    if not rows or "posted" not in headers or "tweet_text" not in headers:
        return 0
    posted_col = headers.index("posted") + 1

    n = 0
    for r in rows:
        if n >= limit:
            break
        if not _is_yes(r.get("approved")):
            continue
        if str(r.get("posted") or "").strip():
            continue
        text = str(r.get("tweet_text") or "").strip()
        if not text:
            continue
        try:
            url = poster(text)
        except Exception:  # noqa: BLE001 — one bad tweet must not stop the rest
            log.exception("X post failed row=%s", r.get("_row"))
            continue
        try:
            store.set_feed_cell(FEED_TAB, r["_row"], posted_col, url or "posted")
        except Exception:  # noqa: BLE001
            log.exception("mark-posted failed row=%s (tweet WAS posted: %s)", r.get("_row"), url)
        n += 1
    return n
