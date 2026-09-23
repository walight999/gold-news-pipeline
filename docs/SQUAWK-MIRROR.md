# First Squawk live mirror (`--mode squawk_mirror`)

Mirror **First Squawk** (`@FirstSquawk`, a real-time financial squawk wire) into
`@tradetongkam` as Thai tweets — the brand's social posts ride the live wire
instead of a once-a-day compose. This fixes the two weaknesses of `daily_brief`:
staleness, and the "did the Fed/BoJ actually hike, or is it expected?" ambiguity
(a live headline states the *actual* event as it prints).

Built 2026-09-22 at White's request. Runs **alongside** `daily_brief`, does not
replace it.

```
@FirstSquawk (X)
   │  apify_source.fetch_tweets   (reuses the existing X scraper)
   ▼
gold-relevant filter  (squawk.keywords: gold / USD / Fed / yields / …)
   │  drop off-brand macro (oil, equities, sports, politics)
   ▼
dedup vs squawk_log  +  daily cap
   │
   ▼
tweet_writer.compose_tweet  → Thai @tradetongkam voice (facts re-voiced, our words)
   │
   ▼
social_feed.x_post → @tradetongkam   +   log the row in squawk_log
```

## Why it's different from the existing First Squawk usage

`@FirstSquawk` is **already** in `sources.yaml → x_accounts`, so it already feeds
the news pipeline (→ LINE alerts + `social_feed` drafts). But that path needs a
breaking/alert **score** and a **manual `approved`=yes** to post. `squawk_mirror`
is a **direct, auto, realtime** path: every gold-relevant FS headline is re-voiced
and posted (up to the cap), no score gate, no manual tick. Its dedup is its own
`squawk_log` tab, so it never double-posts with `social_feed` in practice (that
path is manual and stays unapproved).

## Editorial line (no-ai-slop)

We take the **facts** in an FS headline (a rate decision, a data print, a level)
and re-express them in the brand's analytical Thai voice with a gold angle. We
**never copy FS wording**, add no FS attribution (the brand's X is its own
channel), no link, no emoji — same discipline as every `@tradetongkam` tweet.
Facts are public and reported by everyone; the expression is ours.

## Config (`config/sources.yaml → squawk:`)

| key | default | meaning |
|---|---|---|
| `enabled` | `true` | `false` ⇒ mode is a no-op |
| `handle` | `FirstSquawk` | the X account to mirror |
| `cap_per_day` | `20` | hard ceiling on tweets/day (X is $0.015/post) |
| `max_items` | `5` | scrape at most this many per run (Apify cost guard) |
| `since_minutes` | `20` | look-back per run (> cron gap so nothing is missed) |
| `model` | `claude-sonnet-4-6` | re-voice model (Sonnet for fluent Thai; these auto-post unreviewed). Override via `SQUAWK_MODEL` |
| `keywords` | (list) | gold-mover filter (gold/USD/Fed/yields **+ geopolitics/safe-haven**); omit to use module `DEFAULT_KEYWORDS` |

## Editorial quality bar (the composer prompt enforces this)

The re-voicing is done by `tweet_writer.compose_tweet` on **Sonnet** (Haiku
garbles free-standing Thai). The prompt writes as a Thai market analyst, not a
translator, and each post answers only what applies: (1) เกิดอะไรขึ้น (2) ตลาด
ควรสนใจอะไร (3) เกี่ยวกับทองอย่างไร. It **varies sentence structure** (no
"เหตุผลคือ / ประเด็นสำคัญคือ / จุดที่ต้องจับตาคือ" template every post), does **not
extrapolate** one headline into a grand narrative, says "ผลต่อทองจำกัด" instead of
forcing a gold link, bans literal-translation Thai, and self-checks "คนไทยอ่าน
ครั้งเดียวเข้าใจไหม?" before returning. Target: 5s know what happened, 10s know the
market/gold impact, no re-read.

## Env / secrets (all already set on the repo)

| var | purpose | unset ⇒ |
|---|---|---|
| `APIFY_TOKEN` | scrape `@FirstSquawk` | no scrape → **no-op** |
| `ANTHROPIC_API_KEY` | re-voice to Thai | composer None → **nothing posts** (never posts raw English) |
| `X_API_*` (4) | post to `@tradetongkam` | poster raises → row left unposted, retried |
| `GSHEET_ID` / `GSHEET_CREDS` | dedup + cap in `squawk_log` | — |

## Cost

Apify ~cents/run (kaitoeasyapi $0.18/1k tweets) + **$0.015 per posted tweet**,
hard-bounded by `cap_per_day`. At the default cap of 20 that's ≤ $0.30/day of X
spend. When the day's cap is already reached the run skips the scrape entirely,
so it doesn't even pay Apify.

## Schedule — no cron-job.org job needed

**Primary: it piggybacks the 5-min news cron.** `run_once` (mode `cron`/`event`),
which the cron-job.org dispatcher already fires every 5 min, calls
`squawk_mirror.mirror` at the end — guarded by a `min_interval_min` (15) cost gate
in `source_state._squawk`, so it actually scrapes ~every 15 min, not every tick.
This is the same trick as the `content_review` weekend piggyback: **on-time
cadence via the dispatcher that already runs, with zero new cron-job.org jobs.**

**Fallback:** the standalone `.github/workflows/squawk_mirror.yml` (`*/15` +
`workflow_dispatch`) still exists as a throttled backstop / manual trigger. Both
paths share the same `squawk_log` dedup + daily cap, so they never double-post
(the piggyback additionally rate-limits itself via the `_squawk` interval guard;
the standalone relies on GitHub's own throttle + the shared cap).

## Go-live / test

Everything is env-gated and already has its secrets, so it's live once the
workflow ships. To exercise it on demand:

```bash
gh workflow run squawk_mirror.yml        # or trigger from the Actions tab
```

Watch the run log for `squawk_mirror: posted N tweet(s)` and check
`@tradetongkam` + the `squawk_log` sheet tab. `cap_per_day` bounds the blast
radius of the first live run.

## Idempotency & safety

- **Dedup:** each mirrored headline is logged in `squawk_log` by its FS tweet id;
  re-runs (or the overlapping `since_minutes` window) never re-post it.
- **Crash-safe:** the log row is appended immediately after each successful post,
  so a crash mid-run can't make an already-posted headline re-post next run.
- **Cap:** counts *today's* (ICT) posted rows; at/over the cap the run no-ops.
- **Best-effort:** a scrape / compose / post failure on one item is logged and
  skipped; nothing here raises to the scheduler.
