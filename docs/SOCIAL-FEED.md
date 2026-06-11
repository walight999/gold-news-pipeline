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

## Wiring Make → Twitter (approval flow — the Sheet IS the review surface)

Chosen design (2026-06-11): review-before-post, gated by the `approved` column.
The operator reviews drafts in the Sheet and types `yes` in `approved` for the
ones to publish. Make picks those up on a schedule and posts.

**Prereqs (user, one-time, needs your login):**
1. A **Google Sheets** connection in Make (OAuth your Google account).
2. An **X/Twitter** connection in Make — needs an X developer app with *write*
   access (X API free tier allows ~1,500 posts/month).

**Scenario (`social-autopost`, scheduled every ~15 min):**
1. **Google Sheets → Search Rows** on `social_feed`, filter:
   `approved = yes` AND `posted` is empty. (Search Rows, not Watch Rows, because
   approval is an *edit* to an existing row, which Watch-new-rows wouldn't catch.)
2. **(optional) Router** — only `impact_level = HIGH` if you want the big ones.
3. **X/Twitter → Create a Tweet** with `{{tweet_text}}`.
4. **Google Sheets → Update Row** — set `posted` = the new tweet URL (or `yes`)
   so it is never reposted.

Operator loop: glance at the Sheet → type `yes` in `approved` on the rows worth
posting → Make posts them within 15 min and stamps `posted`. Nothing reaches X
without an explicit per-row `yes`.

Alternative consumers: the feed is plain Sheets, so Zapier / Apps Script / the
Agent HQ X-API path (`docs/PATH-B-SETUP.md`) can read it the same way.

## Notes

- Best-effort: a Sheets hiccup on the feed never fails the LINE news push.
- The feed is **not** auto-purged (you likely want social history). If it ever
  needs trimming, add a `social_feed` purge to `run_maintain()`.
