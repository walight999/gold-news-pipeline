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


def _tweet_handle(t: dict[str, Any]) -> str:
    author = _pick(t, ["author", "user"]) or {}
    if isinstance(author, dict):
        h = _pick(author, ["userName", "screen_name", "username"])
        if h:
            return str(h)
    return str(_pick(t, ["username", "screenName"]) or "x")


def _tweet_to_entry(t: dict[str, Any]) -> dict[str, Any] | None:
    # Skip retweets / replies — we want each account's own breaking lines.
    if _pick(t, ["isRetweet", "retweeted"]) in (True, "true"):
        return None
    text = _pick(t, ["text", "full_text", "rawContent", "content"]) or ""
    text = " ".join(str(text).split())
    if not text:
        return None
    handle = _tweet_handle(t)
    url = _pick(t, ["url", "twitterUrl", "tweetUrl"])
    if not url:
        tid = _pick(t, ["id", "id_str", "tweetId"])
        if tid:
            url = f"https://x.com/{handle}/status/{tid}"
    if not url:
        return None
    return {
        "source_id": f"x_{handle.lower()}",
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
                 max_per_handle: int = 8, timeout: float = 90.0) -> list[dict[str, Any]]:
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
        with httpx.Client(timeout=timeout) as c:
            r = c.post(ENDPOINT, params={"token": token}, json=payload)
        r.raise_for_status()
        items = r.json()
    except Exception as e:  # noqa: BLE001 — Apify is best-effort, never block the run
        log.warning("apify fetch failed: %s", e)
        return []
    entries: list[dict[str, Any]] = []
    for t in items if isinstance(items, list) else []:
        if isinstance(t, dict):
            e = _tweet_to_entry(t)
            if e:
                entries.append(e)
    log.info("apify: %d tweet entries from %d handles", len(entries), len(handles))
    return entries
