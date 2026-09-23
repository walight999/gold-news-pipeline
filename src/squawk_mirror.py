"""First Squawk live mirror → @tradetongkam Thai tweets (`--mode squawk_mirror`).

White wants the brand's X posts to ride on First Squawk (@FirstSquawk), a
real-time financial squawk that breaks macro headlines the SECOND they happen
(e.g. "*FED RAISES RATES 25BPS"). Mirroring the live wire fixes the two problems
of the once-a-day daily_brief: staleness, and the "did it happen or is it
expected" ambiguity — a live headline states the actual event.

Scope (White, 2026-09-22): GOLD-RELEVANT ONLY (gold / USD / Fed / yields), runs
ALONGSIDE daily_brief (does not replace it), AUTO-post with a per-day cap.

Editorial line (no-ai-slop): we take the FACTS in a First Squawk headline and
re-express them in @tradetongkam's own analytical Thai voice with a gold angle —
we never copy their wording. Facts (a rate decision, a data print) are public and
reported by everyone; the expression is ours. No FS attribution on the tweet (the
brand's X is its own channel), no link, no emoji.

Env-gated + best-effort, like the rest of the pipeline:
  - APIFY_TOKEN unset  → no scrape → no-op (exit 0)
  - ANTHROPIC unset    → composer returns None → nothing posts (we never post the
                         raw English headline)
  - X creds unset      → poster raises → row left unposted, retried next run
Nothing here raises to the scheduler.
"""
from __future__ import annotations

import logging
import os
import re
from functools import lru_cache
from typing import Any, Callable

# Public auto-posts, no human review → fluent Thai matters. Sonnet by default
# (Haiku garbles free-standing Thai — the daily_brief lesson); override via
# config `squawk.model` or the SQUAWK_MODEL env.
DEFAULT_COMPOSE_MODEL = "claude-sonnet-4-6"

from . import apify_source, social_feed, tweet_writer
from .utils_time import iso_utc, now_utc, parse_iso, to_ict

log = logging.getLogger("squawk_mirror")

MIRROR_TAB = "squawk_log"
MIRROR_HEADERS = ["ts_utc", "ts_ict", "fs_id", "fs_text", "tweet_text", "posted"]

DEFAULT_HANDLE = "FirstSquawk"

# Gold-mover relevance filter (White: ทอง/USD/Fed/yields). A First Squawk
# headline is kept only if it contains one of these (case-insensitive). Tunable
# via config `squawk.keywords`. Deliberately gold-centric — First Squawk covers
# ALL macro/geopolitics, most of which is off-brand for a gold channel.
DEFAULT_KEYWORDS = [
    # gold itself
    "gold", "xau", "bullion", "precious metal",
    # the Fed / rate policy
    "fed", "fomc", "powell", "rate hike", "rate cut", "rate decision",
    "interest rate", "rates", "basis point", "bps", "hawkish", "dovish",
    "dot plot", "monetary policy",
    # the dollar / yields
    "dollar", "greenback", "dxy", "yield", "yields", "treasury", "treasuries",
    "10-year", "10y", "bond",
    # inflation / data that moves the Fed
    "inflation", "cpi", "pce", "ppi", "payroll", "payrolls", "nfp",
    "jobless", "unemployment",
    # other central banks that move gold via USD/JPY & EUR
    "boj", "bank of japan", "ecb", "lagarde",
    # geopolitics / safe-haven drivers (a top gold catalyst — and @tradetongkam's
    # own voice leans heavily on these). "war" is deliberately omitted: as a
    # word-prefix it would catch "warning"/"warehouse"; the concept is covered by
    # conflict/military/missile/sanction/nuclear + the actor & country names.
    "safe haven", "safe-haven", "geopolit", "conflict", "military", "missile",
    "sanction", "nuclear", "tariff", "oil", "opec", "iran", "israel", "gaza",
    "ukraine", "russia", "hormuz", "tehran", "trump", "middle east",
]

_STATUS_RE = re.compile(r"/status/(\d+)")


# Terms that must match as a WHOLE word, not a prefix — short stems that would
# otherwise swallow an unrelated word. "gold" is the big one: as a prefix it
# matches "Goldman" (Sachs) and "golden", pulling bank stories into a gold feed.
_WHOLE_WORD_ONLY = {"gold"}


@lru_cache(maxsize=32)
def _kw_pattern(keywords: tuple[str, ...]) -> re.Pattern:
    """Compile the keyword list into ONE case-insensitive regex. Each keyword
    starts at a word boundary (`\\b`); by default it needn't END on one, so a
    singular stem also matches its plural (`yield`→`yields`, `sanction`→
    `sanctions`, `basis point`→`basis points`) while mid-word noise is rejected
    (`war` never matches `toward`/`forward`). Terms in `_WHOLE_WORD_ONLY` get a
    trailing `(?![a-z])` so they match the exact word only (`gold` ✓, but not
    `goldman`/`golden`)."""
    parts = [re.escape(k) + (r"(?![a-z])" if k in _WHOLE_WORD_ONLY else "")
             for k in keywords]
    return re.compile(r"\b(?:" + "|".join(parts) + r")", re.IGNORECASE)


def is_relevant(text: str, keywords: list[str]) -> bool:
    """True if the headline mentions any gold-mover keyword at a word boundary."""
    if not keywords:
        return False
    return bool(_kw_pattern(tuple(keywords)).search(text or ""))


def _fs_id(entry: dict[str, Any]) -> str:
    """A stable per-headline id for dedup: the tweet's snowflake id from its URL,
    or the URL itself as a fallback."""
    url = str(entry.get("url") or "")
    m = _STATUS_RE.search(url)
    return m.group(1) if m else url


def _seen_and_today_count(rows: list[dict[str, Any]], today_ict: str) -> tuple[set[str], int]:
    """(ids already mirrored, count mirrored *today* in ICT) from squawk_log."""
    seen: set[str] = set()
    today = 0
    for r in rows:
        fid = str(r.get("fs_id") or "").strip()
        if fid:
            seen.add(fid)
        ts = str(r.get("ts_ict") or "")
        if ts[:10] == today_ict and str(r.get("posted") or "").strip():
            today += 1
    return seen, today


def _compose(text: str, composer: Callable[..., str | None], model: str) -> str | None:
    """Re-express a First Squawk English headline as a @tradetongkam Thai tweet.
    Returns None if the composer is unavailable — we then skip (never post the
    raw English line)."""
    try:
        return composer(headline_th=None, body_th=None, impact_th=None,
                        category=None, en_title=text, en_summary=None, model=model)
    except Exception:  # noqa: BLE001 — composer is best-effort
        log.exception("squawk_mirror: compose failed")
        return None


def mirror(store, *, token: str, cfg: dict[str, Any] | None = None,
           composer: Callable[..., str | None] = tweet_writer.compose_tweet,
           poster: Callable[[str], str] = social_feed.x_post,
           now=None) -> int:
    """Scrape First Squawk, keep gold-relevant + unseen headlines up to the daily
    cap, re-voice each into Thai, post to X, and log it. Returns count posted.

    Best-effort throughout: a scrape/compose/post failure on one item never stops
    the others and never raises to the scheduler."""
    if not token:
        log.info("squawk_mirror: no APIFY_TOKEN — skipping")
        return 0
    cfg = cfg or {}
    handle = str(cfg.get("handle") or DEFAULT_HANDLE)
    cap = int(cfg.get("cap_per_day", 20))
    since_minutes = int(cfg.get("since_minutes", 20))
    max_items = int(cfg.get("max_items", 5))
    keywords = [str(k).lower() for k in (cfg.get("keywords") or DEFAULT_KEYWORDS)]
    model = str(cfg.get("model") or os.environ.get("SQUAWK_MODEL") or DEFAULT_COMPOSE_MODEL)

    now = now or now_utc()
    today_ict = to_ict(now).strftime("%Y-%m-%d")
    try:
        _, rows = store.read_feed(MIRROR_TAB)
    except Exception:  # noqa: BLE001
        log.exception("squawk_mirror: read squawk_log failed")
        rows = []
    seen, today_count = _seen_and_today_count(rows, today_ict)
    if today_count >= cap:
        log.info("squawk_mirror: daily cap %d already reached (%d) — skipping",
                 cap, today_count)
        return 0

    entries = apify_source.fetch_tweets(token, [handle],
                                        since_minutes=since_minutes,
                                        max_per_handle=max_items)
    # Oldest-first so the daily cap fills in chronological order. Undated entries
    # fall back to `now` (sorted last) — and never break the sort on a None.
    entries.sort(key=lambda e: e.get("published_ts") or now)

    posted = 0
    n_dup = n_irrelevant = n_relevant = n_compose_fail = 0
    for e in entries:
        if today_count + posted >= cap:
            log.info("squawk_mirror: hit daily cap %d — stopping", cap)
            break
        fid = _fs_id(e)
        if not fid or fid in seen:
            n_dup += 1
            continue
        text = str(e.get("title") or "").strip()
        if not text or not is_relevant(text, keywords):
            n_irrelevant += 1
            log.debug("squawk_mirror: drop (off-topic): %s", text[:100])
            continue
        n_relevant += 1
        tweet = _compose(text, composer, model)
        if not tweet:
            # No composer / model down — don't post the raw English headline.
            n_compose_fail += 1
            continue
        try:
            url = poster(tweet)
        except Exception:  # noqa: BLE001 — one bad post must not stop the rest
            log.exception("squawk_mirror: X post failed for fs_id=%s", fid)
            continue
        seen.add(fid)
        posted += 1
        # Audit line: the posted Thai text (a public tweet) so a run is reviewable
        # from the Actions log without opening X (which blocks unauthenticated reads).
        log.info("squawk_mirror: POSTED %s | %s", url, " ".join(tweet.split())[:150])
        # Log immediately after each post (crash-safe: a crash mid-loop can't make
        # an already-posted headline re-post next run).
        row = [iso_utc(now), to_ict(now).strftime("%Y-%m-%d %H:%M:%S"),
               fid, text[:280], tweet, url or "posted"]
        try:
            store.append_feed(MIRROR_TAB, MIRROR_HEADERS, [row])
        except Exception:  # noqa: BLE001
            log.exception("squawk_mirror: log append failed (tweet WAS posted: %s)", url)

    log.info("squawk_mirror: posted %d (cap %d, today was %d) from %d scraped "
             "[dup=%d off-topic=%d relevant=%d compose_fail=%d]",
             posted, cap, today_count, len(entries),
             n_dup, n_irrelevant, n_relevant, n_compose_fail)
    # When nothing posted, surface what we saw so the filter/compose can be judged
    # from the run log (FS posts are public tweets — safe to log).
    if posted == 0 and entries:
        for e in entries[:12]:
            log.info("squawk_mirror:   saw | %s", str(e.get("title") or "")[:120])
    return posted
