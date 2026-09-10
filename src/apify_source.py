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
import re
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from .utils_time import now_utc

log = logging.getLogger("apify_source")

ACTOR = "kaitoeasyapi~twitter-x-data-tweet-scraper-pay-per-result-cheapest"
ENDPOINT = f"https://api.apify.com/v2/acts/{ACTOR}/run-sync-get-dataset-items"

_TAG_RE = re.compile(r"<[^>]+>")


def run_actor(token: str, actor_id: str, payload: dict[str, Any],
              timeout: float = 90.0) -> list[Any]:
    """Run any Apify actor synchronously and return its dataset items.

    Generic sibling of the X-specific `fetch_tweets` — every new Apify source
    (Truth Social, Cloudflare-recovery) goes through here so the token handling
    and error-swallowing live in ONE place. Never raises: returns [] on any
    error, so a flaky third-party actor can never block the news run.

    `actor_id` accepts either the store form `owner/name` or the API form
    `owner~name`; both are normalised. Token travels in the Authorization
    header (never the query string) so an Apify 4xx/5xx can't leak it into this
    PUBLIC repo's Actions logs — same rule as fetch_tweets."""
    if not token or not actor_id:
        return []
    aid = actor_id.replace("/", "~")
    endpoint = f"https://api.apify.com/v2/acts/{aid}/run-sync-get-dataset-items"
    try:
        with httpx.Client(timeout=timeout) as c:
            r = c.post(endpoint, headers={"Authorization": f"Bearer {token}"}, json=payload)
        r.raise_for_status()
        items = r.json()
    except httpx.HTTPStatusError as e:  # log the response body — a 4xx says WHY the
        # input was rejected (wrong field name/value), which the exception string
        # alone hides. Body is header-token-free; redact anyway to be safe.
        body = ""
        try:
            body = e.response.text[:300]
        except Exception:  # noqa: BLE001
            pass
        if token:
            body = body.replace(token, "***")
        log.warning("apify actor %s HTTP %s: %s", actor_id,
                    e.response.status_code, body)
        return []
    except Exception as e:  # noqa: BLE001 — Apify is best-effort, never block the run
        msg = str(e).replace(token, "***") if token else str(e)
        log.warning("apify actor %s failed: %s", actor_id, msg)
        return []
    return items if isinstance(items, list) else []


def _strip_html(s: str) -> str:
    """Truth Social post bodies are HTML (`<p>…</p><a>…</a>`). Reduce to plain
    text so the classifier and dedup see the same shape as a tweet."""
    return _TAG_RE.sub(" ", s or "")


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
    raw = run_actor(token, ACTOR, payload, timeout=timeout)
    entries: list[dict[str, Any]] = []
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


# ---------------------------------------------------------------------------
# Truth Social (Trump & co.) — no public API; a market-mover source not carried
# by any RSS feed. Posts flow into the SAME pool as tweets (tier-2 wire), so
# they dedup/score/route identically and can confirm an RSS/X break.
#
# Third-party actor: the output field names differ per actor, so `_field_map`
# lets the operator remap without a code change, and every mapping is validated
# once cheaply via `--mode apify_probe truth` (see main.py). We map on STRUCTURE
# (no author / empty text ⇒ drop) exactly like the tweet path, so a billing
# notice or malformed record can't become a news event.
# ---------------------------------------------------------------------------

# Default field aliases. Covers the shapes seen across the common Truth Social
# actors (parsebird / automation-lab / tri_angle / muhammetakkurtt). Override
# any list via config `truth_social.field_map`.
_TRUTH_FIELDS: dict[str, list[str]] = {
    "text": ["content", "text", "body", "rawContent", "caption"],
    "url": ["url", "uri", "postUrl", "link"],
    "handle": ["username", "acct", "handle", "screen_name"],
    "created": ["created_at", "createdAt", "date", "published", "timestamp"],
    "id": ["id", "post_id", "statusId"],
}


def _truth_handle(p: dict[str, Any], fm: dict[str, list[str]]) -> str | None:
    """Author handle. Truth actors nest it under `account` OR flatten it; try
    both. None (like the tweet path) means we can't attribute it ⇒ not news."""
    acct = _pick(p, ["account", "author", "user"]) or {}
    if isinstance(acct, dict):
        h = _pick(acct, fm.get("handle", _TRUTH_FIELDS["handle"]))
        if h:
            return str(h)
    h = _pick(p, fm.get("handle", _TRUTH_FIELDS["handle"]))
    return str(h) if h else None


def _truth_to_entry(p: dict[str, Any], tier: int = 2,
                    field_map: dict[str, list[str]] | None = None) -> dict[str, Any] | None:
    fm = field_map or _TRUTH_FIELDS
    # Skip reblogs — we want each account's own posts (mirror of tweet isRetweet).
    if _pick(p, ["reblog", "isReblog", "reblogged"]) not in (None, "", False, "false"):
        return None
    # Noise filters (2026-09-10, from the live probe): Truth Social is mostly
    # political/personal, so pre-drop obvious non-broadcasts BEFORE they enter the
    # pool and cost a classifier call. High-precision — each is almost never a
    # gold/macro catalyst:
    #   - replies (in_reply_to_id set): conversation noise, not a broadcast.
    #   - "RT:"-prefixed content: a reblog the actor didn't flag structurally.
    if _pick(p, ["in_reply_to_id", "in_reply_to_account_id"]) not in (None, "", False):
        return None
    text = _strip_html(str(_pick(p, fm.get("text", _TRUTH_FIELDS["text"])) or ""))
    text = " ".join(text.split())
    if not text:
        return None
    if text[:4].upper().startswith(("RT:", "RT @")):
        return None
    # Link-only posts (content is just a URL, e.g. an Instagram/Rumble share):
    # no readable claim → nothing to classify or translate.
    if re.fullmatch(r"https?://\S+", text):
        return None
    handle = _truth_handle(p, fm)
    if handle is None:
        return None
    url = _pick(p, fm.get("url", _TRUTH_FIELDS["url"]))
    if not url:
        tid = _pick(p, fm.get("id", _TRUTH_FIELDS["id"]))
        if tid:
            url = f"https://truthsocial.com/@{handle}/{tid}"
    if not url:
        return None
    return {
        "source_id": f"truth_{handle.lower()}",
        "tier": tier,
        "role": "trader_macro",
        "title": text[:280],
        "summary": "",
        "url": str(url),
        "published_ts": _parse_dt(_pick(p, fm.get("created", _TRUTH_FIELDS["created"]))),
        "source_class": "wire",
        "organization": f"truth_{handle.lower()}",
    }


def fetch_truth_social(token: str, actor_id: str, handles: list[str],
                       max_per_handle: int = 8, tier: int = 2,
                       per_handle: bool = True, username_key: str = "username",
                       input_key: str = "profiles",
                       payload_extra: dict[str, Any] | None = None,
                       field_map: dict[str, list[str]] | None = None,
                       since_minutes: int | None = None,
                       timeout: float = 120.0) -> list[dict[str, Any]]:
    """RSS-shaped entries for recent Truth Social posts. Never raises → [].

    Two input conventions, config-driven so going live is a config + one probe
    run, not a code change:
    - `per_handle=True` (DEFAULT, matches parsebird/truth-social-scraper): the
      actor takes ONE `username` per run, so we call it once per handle with
      `{username_key: handle, maxPosts: max_per_handle, cleanContent: True}`.
      N handles ⇒ N actor calls (a handful of cents for 2 accounts).
    - `per_handle=False` (list actors): one call with `{input_key: [handles]}`.
    `payload_extra` merges into the input; `field_map` remaps output fields."""
    if not token or not actor_id or not handles:
        return []
    raw: list[Any] = []
    if per_handle:
        base: dict[str, Any] = {"maxPosts": max_per_handle, "cleanContent": True}
        if payload_extra:
            base.update(payload_extra)
        for h in handles:
            raw.extend(run_actor(token, actor_id, {**base, username_key: h},
                                 timeout=timeout))
    else:
        payload: dict[str, Any] = {input_key: list(handles),
                                   "maxPosts": max_per_handle * len(handles)}
        if payload_extra:
            payload.update(payload_extra)
        raw = run_actor(token, actor_id, payload, timeout=timeout)
    cutoff = (now_utc() - timedelta(minutes=since_minutes)) if since_minutes else None
    entries: list[dict[str, Any]] = []
    for p in raw:
        if not isinstance(p, dict):
            continue
        e = _truth_to_entry(p, tier=tier, field_map=field_map)
        if not e:
            continue
        # Freshness gate (best-effort): only when the post carries a timestamp.
        # A cron re-run over the same posts is otherwise deduped by sent_log.
        if cutoff and e.get("published_ts") and e["published_ts"] < cutoff:
            continue
        entries.append(e)
    if raw and not entries:
        # Records came back but the mapper dropped them all — almost always the
        # actor's output field names differ from the defaults. Log the first
        # record's keys so field_map can be fixed from the prod log (no token
        # needed locally).
        sample = raw[0] if isinstance(raw[0], dict) else {}
        log.warning("apify truth: %d records but 0 mapped — first-record keys: %s",
                    len(raw), list(sample.keys())[:20])
    log.info("apify truth: %d post entries from %d handles (%d records dropped)",
             len(entries), len(handles), len(raw) - len(entries))
    return entries


# ---------------------------------------------------------------------------
# Cloudflare-recovery — fetch a URL a normal httpx GET can no longer reach
# (Benzinga 403 since 2026-06-19, and any future feed that hides behind
# Cloudflare) through an Apify browser/unblocker actor, and hand the RAW body
# back so the EXISTING `parser.parse_feed` handles it. We never guess the news
# item shape here — only the actor's body field — because parse_feed already
# knows RSS/Atom.
# ---------------------------------------------------------------------------

def fetch_url_via_proxy(token: str, actor_id: str, url: str,
                        input_key: str = "url", url_as_object: bool = False,
                        url_as_list: bool = False,
                        body_field: list[str] | None = None,
                        payload_extra: dict[str, Any] | None = None,
                        timeout: float = 120.0) -> bytes | None:
    """Return the raw response body (bytes, for parse_feed) of `url` fetched via
    a Cloudflare-bypass Apify actor, or None on any failure.

    Input shape is config-driven to cover the three common actor conventions:
    - bare string  `{input_key: url}`               — url_as_list=False, url_as_object=False
      (DEFAULT; matches scrapeunblocker/scrapeunblocker: input `url`, output `html`)
    - list of str  `{input_key: [url]}`             — url_as_list=True,  url_as_object=False
      (ecomscrape/cloudflare-web-scraper: input `urls`)
    - list of obj  `{input_key: [{"url": url}]}`     — url_as_list=True,  url_as_object=True
      (apify/*-scraper `startUrls` convention)
    `body_field` lists the dataset field holding the page body (default tries the
    common names, `html` first). Validate once with `--mode apify_probe recover:<id>`."""
    if not token or not actor_id or not url:
        return None
    item: Any = {"url": url} if url_as_object else url
    payload: dict[str, Any] = {input_key: [item] if url_as_list else item}
    if payload_extra:
        payload.update(payload_extra)
    raw = run_actor(token, actor_id, payload, timeout=timeout)
    if not raw or not isinstance(raw[0], dict):
        log.warning("apify recover %s: actor returned no usable record for %s", actor_id, url)
        return None
    body = _pick(raw[0], body_field or ["html", "body", "content", "text", "data", "rawBody"])
    if not body:
        log.warning("apify recover %s: no body field in record for %s (keys=%s)",
                    actor_id, url, list(raw[0].keys())[:12])
        return None
    return body.encode("utf-8") if isinstance(body, str) else bytes(body)
