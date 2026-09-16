# Daily Brief — one pool → three channels, one review page

`--mode daily_brief` turns the day's curated gold news into **three
@tradetongkam artifacts** and drops them on **one Notion page** so the operator
approves everything in one place each morning (instead of scanning 1000+
`social_feed` rows).

```
social_feed (breaking/alert, last 24h, deduped)   ← already gold-classified + Thai
        │  daily_brief.collect_brief_events
        ▼
one Claude call (Sonnet)  → { theme, tweets[3-4], fb_article, video_script }
        │  daily_brief.compose_brief   (tweets fit ≤280 + brand tags via tweet_writer)
        ▼
Notion review page (one to_do per tweet + one per article + one per video script)
        │  render_notion_blocks → post_to_notion
        ▼
operator ticks ✅ → copies FB article / video script out, Twitter posts via social_post
```

## The three artifacts

| Channel | What | Format |
|---|---|---|
| **Twitter** | 3-4 curated tweets, highest-signal non-duplicate angles | ≤280, one direction emoji 🔴/🟢/🟡, brand hashtags |
| **Facebook** | long-form Thai wrap | 4-6 paragraphs, no hashtags, no emoji |
| **Video** | TTS-ready spoken narration | 4-5 scenes marked `[ฉาก N — <visual cue>]`, ~75s |

All three follow no-ai-slop: no em-dash, source attribution (CNBC/WSJ/รอยเตอร์/…),
no engagement-bait. Tweets reuse `tweet_writer._fit` so none end mid-word.

## Model

Defaults to **`claude-sonnet-4-6`** (`DEFAULT_MODEL`), overridable with the
`BRIEF_MODEL` env/secret. This is deliberate: the brief runs **once a day** and
is public long-form brand copy, where Haiku garbled Thai words in testing
(`เญาปี่`, `หนี้เงิน`). Sonnet is fluent at ~a few cents/day. The per-event
`tweet_writer` (high volume) stays on Haiku.

## Env / secrets

| var | purpose |
|---|---|
| `GSHEET_ID`, `GSHEET_CREDS` | read `social_feed` (already set) |
| `ANTHROPIC_API_KEY` | compose the brief (already set) |
| `NOTION_TOKEN` | Notion internal-integration token — **UNSET ⇒ DRY RUN** |
| `NOTION_BRIEF_PARENT` | Notion page id the daily pages are created under |
| `BRIEF_MODEL` | optional model override |

**Best-effort + env-gated, like the rest of the pipeline:** no
`NOTION_TOKEN`/`NOTION_BRIEF_PARENT` → **dry run**: the markdown is written to
`snapshots/daily_brief_<YYYYMMDD>.md` (gitignored) and logged, nothing is
posted, exit 0. No Anthropic key → logs and exits 0. Nothing here crashes.

## Go-live (White) — turn the dry run into a live Notion page

1. **Create a Notion internal integration:** notion.so/my-integrations →
   *New integration* → copy the *Internal Integration Secret*.
2. **Make a parent page** in Notion (e.g. "Gold Daily Brief"), open its
   `•••` menu → *Connections* → add your integration so it can write there.
3. **Get the parent page id:** it's the 32-hex chunk in the page URL
   (`notion.so/<title>-<THIS_ID>`).
4. **Add two GitHub secrets** on `gold-news-pipeline`:
   `NOTION_TOKEN` = the integration secret, `NOTION_BRIEF_PARENT` = the page id.
5. Next `daily-brief` run (07:30 ICT, or trigger `workflow_dispatch`) posts a
   real page. Until then it keeps writing the local snapshot.

## Operator loop

Morning: open the day's Notion page → read the 3 sections → tick ✅ on the
tweets worth posting + article/script if good → the Twitter picks go out through
the existing `social_post` path; **the FB article auto-publishes once you tick
its checkbox** (Phase 2, below); the video script is copied out manually
(Phase 3 = video-tool handoff, TBD).

## Phase 2 — Facebook auto-publish (`--mode fb_post`)

When `daily_brief` posts the Notion page it also appends a `daily_brief_log`
sheet row (`date`, `page_id`, `page_url`, `fb_article`, `video_script`,
`fb_posted`, `notes`). `--mode fb_post` (hourly workflow `fb_post.yml`):

```
daily_brief_log (newest unposted row)
   → read the Notion page's "อนุมัติบทความ Facebook" checkbox (fb_publish.is_fb_approved)
   → if ticked: post the STORED fb_article to the FB Page (Graph API /feed)
   → stamp fb_posted with the post URL
```

The article text is read from the sheet row (not reconstructed from Notion) so
only the **checkbox state** comes from Notion — robust. Fail-closed: any read
error ⇒ treated as not-approved, nothing posts. One post per run.

**Go-live (White) — Facebook:**
1. Create a Meta app (developers.facebook.com) and connect your brand
   **Facebook Page**.
2. Generate a **long-lived Page access token** with `pages_manage_posts` +
   `pages_read_engagement` (Graph API Explorer → exchange for long-lived).
3. Add GH secrets on `gold-news-pipeline`: `FB_PAGE_ID` (the numeric Page id) +
   `FB_PAGE_TOKEN`. `NOTION_TOKEN` must already be set (Phase 1) so the approval
   checkbox can be read.
4. Until both FB secrets are set, `fb_post` no-ops (logs + exit 0).

Video auto-handoff is Phase 3 (tool not yet chosen — the script stays
tool-agnostic in the meantime).

## Local dry run

```bash
# needs GSHEET_CREDS (from creds.json) + ANTHROPIC_API_KEY in env/.env
python -m src.main --mode daily_brief
# → writes snapshots/daily_brief_<today>.md, logs "DRY RUN", exit 0
```
