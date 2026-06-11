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
| `posted` | left blank — your autopost stamps this after posting |

## The tweet draft

Built to the no-ai-slop discipline for public copy:
- no em-dash (stripped from the generated Thai),
- exactly one direction emoji (🟢 gold-bullish tone, 🔴 bearish, 🟡 neutral),
- source attribution, factual, `#ทองคำ #XAUUSD`,
- trimmed to fit 280 (URL counts as 23, Thai chars as 1).

It is a **draft for review**, not auto-fired. Review (or let an approval step
gate it), then post.

## Wiring Make → Twitter (suggested)

1. **Trigger** — Google Sheets → *Watch Rows* on the `social_feed` worksheet.
2. **Filter** — only rows where `posted` is empty (skip already-posted).
   Optionally filter `impact_level = HIGH` if you only want the big ones.
3. **(optional) Approval** — route `tweet_text` to LINE/Slack for a yes/no
   before posting (recommended at first, to tune voice).
4. **Action** — X/Twitter → *Create a Tweet* with `{{tweet_text}}`.
5. **Write-back** — Google Sheets → *Update Row*, set `posted` = the tweet URL
   or `yes`, so it isn't reposted.

Alternative: the feed is plain Sheets, so Zapier / Apps Script / the Agent HQ
X-API path (`docs/PATH-B-SETUP.md`) can consume it the same way.

## Notes

- Best-effort: a Sheets hiccup on the feed never fails the LINE news push.
- The feed is **not** auto-purged (you likely want social history). If it ever
  needs trimming, add a `social_feed` purge to `run_maintain()`.
