# CLAUDE.md — gold-news-pipeline

Project memory for Claude. Read this first when working in this repo.

## What this is

Gold/XAUUSD news intelligence pipeline. RSS + X/Twitter + ForexFactory calendar
→ score/dedup/route → **Thai LINE Flex alerts** + a **social feed → X autopost**.
Runs on GitHub Actions, state in Google Sheets, output to LINE + X. Owner trades
gold; output language is **Thai**.

- **Repo:** github.com/walight999/gold-news-pipeline (PUBLIC, MIT)
- **Sheet (state + social_feed):** docs.google.com/spreadsheets/d/136Lsfx0OshKXBmi1PMXWNX1w66udrZuzKX7RSL5AONQ
- **Brand X account:** @tradetongkam

## Data flow

```
RSS (16 sources) + Apify X (7 accounts) + ForexFactory
        │  fetcher.py (conditional GET) / apify_source.py
        ▼
normalize → dedup.cluster → scorer.score → router.decide
        │
        ├─► LINE: breaking / alert / digest(=newsupdate) / eod_recap / health
        │     news_alert.classify_and_rewrite (Claude Haiku → Thai), line_flex, line_client
        │     → LINE_NEWS_TARGET = "U160…(1:1),C7b49469…(group 'News Update')"
        │
        └─► social_feed sheet tab: tweet_writer composes a @tradetongkam-voice
              draft → operator types `yes` in `approved` → social_post posts to X
```

Economic **calendar + upcoming** are NOT here anymore — they moved to the GAS
project **newsupdate-linebot** (`C:\Users\usEr\newsupdate-linebot\Code.gs`,
gist 972fa38d) because GitHub cron is too unreliable for T-15 timing. The pipeline's
`calendar_daily` workflow is **disabled** (GAS owns the daily calendar).
`calendar_check` is **active in POST-only mode** (`calendar.pre_release_enabled:
false`) — it sends Released News (FRED actuals + XAU reaction), which GAS can't
do; never re-enable its pre-release side or it duplicates the GAS T-15 cards. FF data is pushed INTO that GAS via
`ff_gas_thisweek`/`ff_gas_weekly` (the `GAS_WEBAPP_*` secrets).

## Working conventions (IMPORTANT)

- **Never push to `main` directly** — the harness blocks it. Always: branch →
  commit → `gh pr create` → `gh pr merge --squash --delete-branch` → back to main.
- **Run `python -m pytest tests/ -q` before every commit.** Keep it green.
- End commit messages with `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.
- Thai test strings: write test files with the Write tool (UTF-8), NOT bash
  heredocs (Windows cp874 mangles Thai).
- Best-effort side features (social_feed, apify, tweet_writer) must **never**
  crash the news run — wrap in try/except, return None/[] on failure.

## CLI modes (`python -m src.main --mode X`)

`cron` (default, RSS+Apify+route, every 5 min) · `event` (high-freq window) ·
`digest` · `eod_recap` (23:00 ICT) · `weekly_preview` (Sat) · `verify_sources`
(weekly health probe) · `maintain` (purge) · `watchdog` (self-monitor) ·
`social_post` (post approved drafts to X, cron */20) · `social_seed` (append one
test draft) · `calendar_daily` (**retired — GAS owns it**) · `calendar_check` (**post-only: Released News**).

## Key modules

- `main.py::run_once` — the cron pipeline (fetch→normalize→cluster→score→route→send).
- `apify_source.py` — scrapes X accounts (config `x_accounts`), tweets become
  RSS-shaped entries (tier=2, role=trader_macro). Gated by `APIFY_TOKEN`.
- `news_alert.py` — Claude Haiku classify + Thai rewrite for LINE. **Do not
  repurpose for social** — keep LINE and social independent. Also owns
  `explain_calendar_release()` — short Thai "what this print means for gold"
  for Released-News cards (Claude Haiku → Gemini → None, cached in
  translation_cache `cl…` keys).
- **News Update (digest) — 6 windows, full detail (2026-06-22).** 6 rounds/day
  (04:30/08:30/12:30/16:30/20:30/00:30 ICT, `schedule.yaml::digest.slots_ict`).
  Each round scans the WHOLE 4h window from `event_state` via
  `digest.collect_window_events` (NOT just this run's fetch), sends at most
  `max_cards` (4) events, ONE full-detail Flex bubble each (headline + body
  bullets + source ref) via `line_flex.news_update_carousel`. `event_state`
  gained `title`/`summary`/`url` columns so a stored event renders without a
  re-fetch; re-classifying it is a translation_cache hit. `sent_log` digest
  rows dedup an event across rounds + against breaking/alert.
- `tweet_writer.py` — separate Claude call composing the @tradetongkam-voice
  tweet (no emoji, analytical, `#ทองวันนี้ #ข่าวทอง #เทรดทอง #ทองคำ`).
- `social_feed.py` — append-only `social_feed` sheet writer + the approval-gated
  `post_pending` (posts via tweepy when `approved`=yes & `posted` empty).
- `store.py` — Google Sheets state. `flush()` clears+rewrites whole tabs (so the
  social feed uses `append_feed`/`set_feed_cell` instead, never clobbered).
- `router.py` — ≥4.5 breaking, ≥3.5 alert (if official or ≥2 orgs), ≥2.5 digest.

## Config

- `config/sources.yaml` — `sources:` (RSS) + `x_accounts:` (Apify handles,
  intervals, cost guard). `config/keywords.yaml` (topics/scoring),
  `config/schedule.yaml` (slots, rate limits, quiet hours).

## Secrets (GitHub Actions)

`GSHEET_ID`, `GSHEET_CREDS`, `LINE_CHANNEL_TOKEN`, `LINE_NEWS_TARGET`,
`ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, `FRED_API_KEY`, `APIFY_TOKEN`,
`GAS_WEBAPP_URL`, `GAS_WEBAPP_TOKEN`, and X: `X_API_KEY` `X_API_SECRET`
`X_ACCESS_TOKEN` `X_ACCESS_TOKEN_SECRET` (OAuth 1.0a, @tradetongkam, Read+Write).

**Classifier provider chain** (`news_alert.classify_and_rewrite`): Claude Haiku
(primary) → **Gemini `gemini-2.0-flash`** (secondary, `GEMINI_API_KEY`, free tier,
same prompt+JSON contract) → literal Google-Translate fallback (`category="Other"`,
never cached). The Gemini tier exists so an Anthropic outage / monthly spend cap
no longer drops card quality — it just switches model. Optional `GEMINI_MODEL`
overrides the model id.
⚠️ `LINE_CHANNEL_TOKEN` was pasted in chat 2026-06-11 — rotation pending.

## Cost model (~ a few $/month)

- GitHub Actions: free (public repo, but `schedule:` cron is throttled to ~1 run
  every 2-4h — don't rely on tight intervals). Real 5-min cadence comes from an
  **external scheduler hitting `workflow_dispatch`** (cron-job.org), NOT the
  `schedule:` block. If `gh run list` shows only `schedule` events and morning
  (22:00-06:00 UTC) coverage drops, the external dispatcher is dead — see
  **`docs/DISPATCHER-CRON.md`** (setup + the 2026-06-23 GAS-dispatcher outage).
- Claude Haiku (rewrite + tweet voice): ~$1-3/mo.
- Apify X scraper (kaitoeasyapi): ~$0.18/1k tweets, min-interval guard → ~$2-10/mo
  of the $30 budget.
- X API posting (pay-per-use since 2026-02): **$0.015/post WITHOUT a link**,
  $0.20 WITH a link → drafts are deliberately link-free + source-free.

## Social autopost loop (operator)

News → draft lands in `social_feed` (tweet_text, @tradetongkam voice) → operator
reviews, types `yes` in `approved` for the ones to publish → `social-post` cron
(~20 min) posts to X at $0.015 and writes the tweet URL into `posted`. Nothing
posts without an explicit per-row `yes`. Docs: `docs/SOCIAL-FEED.md`.

## State as of 2026-06-11

All LIVE: news→LINE, FF-prefetch (302-redirect fixed), group delivery, social
feed + X autopost (PR #1-8), @tradetongkam tweet voice (PR #9), Apify X
fast-news (PR #10-11). Next: observe real output 1-2 days + tune handles/voice;
rotate the LINE token.
