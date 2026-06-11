# Social Feed → Twitter autopost

Every **news** push (breaking / alert / digest a.k.a. newsupdate / eod recap)
also appends a row to a `social_feed` worksheet in the pipeline's Google Sheet
(`GSHEET_ID`), carrying structured fields **plus a ready-to-post Thai tweet
draft**. A downstream automation (Make / Zapier → X/Twitter) consumes it.

Calendar + upcoming are **not** in the feed (owned by the GAS bot, less suited
to social).

## Where

Same spreadsheet as the pipeline state (`GSHEET_ID`), a worksheet named
`social_feed`, created automatically on the first news push. It is **append-only**
— the pipeline never clears it (unlike the state tabs), so "watch new rows" stays
stable and the `posted` column you own is never overwritten.

## Columns

| col | meaning |
|---|---|
| `ts_utc` / `ts_ict` | when the item was pushed |
| `type` | `breaking` / `alert` / `digest` / `recap` |
| `category` | classifier category (Inflation / Central Bank / Geopolitics / …) |
| `tone` | hawkish / dovish / risk_on / risk_off / neutral |
| `impact_level` | HIGH / MEDIUM / LOW (per-event score) |
| `headline_th` / `summary_th` / `impact_th` | Thai content from the classifier |
| `source` | human-readable source name |
| `url` | article link |
| `tweet_text` | **ready-to-post Thai draft** (≤280, no-ai-slop applied) |
| `approved` | **human gate** — blank by default; type `yes` to release a draft |
| `posted` | left blank — the autopost stamps this (tweet URL) after posting |

## The tweet draft

Built to the no-ai-slop discipline for public copy:
- no em-dash (stripped from the generated Thai),
- exactly one direction emoji (🟢 gold-bullish tone, 🔴 bearish, 🟡 neutral),
- source attribution, factual, `#ทองคำ #XAUUSD`,
- trimmed to fit 280 (URL counts as 23, Thai chars as 1).

It is a **draft for review**, not auto-fired. Review (or let an approval step
gate it), then post.

## Posting to X (pipeline-direct, approval-gated)

Chosen design (2026-06-11): the **pipeline** posts approved drafts straight to X.
Make was dropped for posting — it no longer has a native "post to your X account"
connector (X removed free API write in 2023), and the third-party schedulers
(Late / Blotato / …) need an extra paid middleman. Posting from the pipeline via
the X API is cleaner and fully under our control. The Google Sheet stays as the
archive + review surface.

**Flow:** `social-post` mode (GHA cron `*/20`) → reads `social_feed` → for each
row where `approved` = yes AND `posted` is empty → posts `tweet_text` via the X
API (tweepy) → writes the tweet URL into `posted`. `SOCIAL_POST_LIMIT` (default 5)
caps posts per run. Per-tweet failures are logged and retried next run; nothing is
posted without an explicit per-row `yes`.

**Operator loop:** glance at the Sheet → type `yes` in `approved` on the rows
worth posting → within ~20 min (GitHub cron throttling makes it longer, fine for
social) the pipeline posts them and stamps `posted`.

**Secrets to add on `gold-news-pipeline` (GitHub → Settings → Secrets):**
from your X developer app (App permissions = Read **and** Write):

| secret | from X developer portal |
|---|---|
| `X_API_KEY` | API Key (Consumer Key) |
| `X_API_SECRET` | API Secret (Consumer Secret) |
| `X_ACCESS_TOKEN` | Access Token (created with Read+Write) |
| `X_ACCESS_TOKEN_SECRET` | Access Token Secret |

`GSHEET_ID` + `GSHEET_CREDS` are already set (the pipeline uses them).

Alternative consumers: the feed is plain Sheets, so Zapier / Apps Script can also
read it — but the built-in `social-post` mode needs nothing extra.

## Notes

- Best-effort: a Sheets hiccup on the feed never fails the LINE news push.
- The feed is **not** auto-purged (you likely want social history). If it ever
  needs trimming, add a `social_feed` purge to `run_maintain()`.
