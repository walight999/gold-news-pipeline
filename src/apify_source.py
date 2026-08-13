"""Apify-powered X/Twitter fast-news source.

Scrapes a curated set of high-signal macro/gold X accounts (which break
market-moving headlines minutes before RSS) and turns recent tweets into the
SAME entry shape as RSS, so they flow through normalize → dedup → score → route
and reach BOTH the LINE alerts and the social feed. Cross-source clustering with
RSS is a bonus: an X break + an RSS confirmation counts as 2 independent orgs,
which lifts routing confidence.

Actor: kaitoeasyapi cheapest tweet scraper (~$0.18 / 1,000 results, no rate
limits). A min-interval guard in main.py caps how often this runs so overlapping
`since:` windows don't overpay.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from .utils_time import now_utc

log = logging.getLogger("apify_source")

ACTOR = "kaitoeasyapi~twitter-x-data-tweet-scraper-pay-per-result-cheapest"
ENDPOINT = f"https://api.apify.com/v2/acts/{ACTOR}/run-sync-get-dataset-items"


def _pick(d: dict[str, Any], keys: list[str]) -> Any:
    for k in keys:
        v = d.get(k)
        if v not in (None, ""):
            return v
    return None


def _parse_dt(v: Any) -> datetime | None:
    if v in (None, ""):
        return None
    if isinstance(v, (int, float)):
        secs = v / 1000 if v > 2e12 else v
        try:
            return datetime.fromtimestamp(secs, tz=timezone.utc)
        except Exception:  # noqa: BLE001
            return None
    s = str(v).strip()
    for fmt in ("%a %b %d %H:%M:%S %z %Y", "%Y-%m-%dT%H:%M:%S.%fZ",
                "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S"):
        try:
            dt = datetime.strptime(s, fmt)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except Exception:  # noqa: BLE001
            pass
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:  # noqa: BLE001
        return None


def _tweet_handle(t: dict[str, Any]) -> str | None:
    """The tweet's author handle, or None when the record carries no author.

    None is meaningful: the actor's billing-notice record has no author, and
    that is how we tell it apart from a real tweet. Callers must not paper over
    it with a placeholder — see `_tweet_to_entry`."""
    author = _pick(t, ["author", "user"]) or {}
    if isinstance(author, dict):
        h = _pick(author, ["userName", "screen_name", "username"])
        if h:
            return str(h)
    h = _pick(t, ["username", "screenName"])
    return str(h) if h else None


def _looks_like_real_tweet_id(tid: Any) -> bool:
    """Snowflake ids are large positive integers. The actor stamps its notice
    record with -1."""
    try:
        return int(str(tid).strip()) > 0
    except (TypeError, ValueError):
        return False


def _tweet_to_entry(t: dict[str, Any], tier: int = 2) -> dict[str, Any] | None:
    # Skip retweets / replies — we want each account's own breaking lines.
    if _pick(t, ["isRetweet", "retweeted"]) in (True, "true"):
        return None
    text = _pick(t, ["text", "full_text", "rawContent", "content"]) or ""
    text = " ".join(str(text).split())
    if not text:
        return None
    # The kaitoeasyapi actor bills a minimum charge per call even when the query
    # matches nothing, and signals that by returning a NOTICE record rather than
    # an empty list ("...we returned N pieces of mock data"). It has no author
    # and a tweet id of -1, so it used to land as source_id `x_x` with url
    # .../status/-1. That put 119 junk rows into event_state in 8 days, each one
    # paying for a Claude classification, and made `other` the largest topic
    # bucket in the stats. Filter on STRUCTURE (no author / non-snowflake id),
    # not on the notice wording, which the actor is free to reword.
    handle = _tweet_handle(t)
    tid = _pick(t, ["id", "id_str", "tweetId"])
    if handle is None or (tid is not None and not _looks_like_real_tweet_id(tid)):
        return None
    url = _pick(t, ["url", "twitterUrl", "tweetUrl"])
    if not url:
        if tid:
            url = f"https://x.com/{handle}/status/{tid}"
    if not url:
        return None
    return {
        "source_id": f"x_{handle.lower()}",
        # tier 2 = fast wire (like forexlive/benzinga). Required by normalize;
        # also drives dedup ranking (lower tier wins the representative slot).
        "tier": tier,
        "role": "trader_macro",     # required by normalize
        "title": text[:280],
        "summary": "",
        "url": str(url),
        "published_ts": _parse_dt(_pick(t, ["createdAt", "created_at", "date", "timestamp"])),
        # Tweets behave like wire copy; each handle is its own organization so
        # multiple accounts confirming the same story count as independent.
        "source_class": "wire",
        "organization": f"x_{handle.lower()}",
    }


def fetch_tweets(token: str, handles: list[str], since_minutes: int = 20,
                 max_per_handle: int = 8, tier: int = 2,
                 timeout: float = 90.0) -> list[dict[str, Any]]:
    """Return RSS-shaped entries for recent tweets from `handles`. Never raises
    — on any error returns []. Caller adds these to the raw entry pool."""
    if not token or not handles:
        return []
    since = (now_utc() - timedelta(minutes=since_minutes)).strftime("%Y-%m-%d_%H:%M:%S_UTC")
    payload = {
        "searchTerms": [f"from:{h} since:{since}" for h in handles],
        "maxItems": max_per_handle * len(handles),
        "sort": "Latest",
        "lang": "en",
    }
    try:
        # Token in the Authorization header, NOT the query string. httpx's
        # HTTPStatusError string embeds the full request URL, so a query-string
        # token would leak into this PUBLIC repo's Actions logs on any Apify
        # 4xx/5xx (429s are routine).
        with httpx.Client(timeout=timeout) as c:
            r = c.post(ENDPOINT, headers={"Authorization": f"Bearer {token}"}, json=payload)
        r.raise_for_status()
        items = r.json()
    except Exception as e:  # noqa: BLE001 — Apify is best-effort, never block the run
        # Defence in depth: redact the token from the error text too, in case a
        # future code path (or the SDK) ever echoes it.
        msg = str(e).replace(token, "***") if token else str(e)
        log.warning("apify fetch failed: %s", msg)
        return []
    entries: list[dict[str, Any]] = []
    raw = items if isinstance(items, list) else []
    for t in raw:
        if isinstance(t, dict):
            e = _tweet_to_entry(t, tier=tier)
            if e:
                entries.append(e)
    # Log the drop count. A call that returns records but yields zero entries is
    # the billing-notice case: we paid the minimum charge and got no news. Worth
    # seeing in the logs — silently swallowing it is how it ran for 8 days.
    log.info("apify: %d tweet entries from %d handles (%d records dropped)",
             len(entries), len(handles), len(raw) - len(entries))
    return entries
