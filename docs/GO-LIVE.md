# Go-Live Runbook — @tradetongkam content pipeline

Everything is **built, tested (533 green), and the workflows are active**. Nothing
posts yet because the external service keys aren't set — each feature is env-gated
and no-ops until its key exists. This is the single ordered checklist to switch it on.

## What one Notion page drives (once keys are set)

```
news → daily_brief → ONE Notion review page (07:30 ICT)
   tick "Tweet N"          → posts to X, lead tweet carries the artwork
   tick "อนุมัติบทความ Facebook" → Facebook photo post (artwork + caption)
   tick "อนุมัติบทพูด video"     → renders + posts a Facebook Reel
   4. Artwork section       → copy prompt → gen in ChatGPT web → drop image in page
```

## Status of each key

| Key(s) | State | Unlocks | Who |
|---|---|---|---|
| `X_API_KEY` + 3 | ✅ already a GH secret | tweets → X | — |
| `GSHEET_*`, `ANTHROPIC`, `GEMINI` | ✅ already set | brief compose + sheet | — |
| `NOTION_TOKEN` + `NOTION_BRIEF_PARENT` | ❌ needed | the review page + ALL approvals | **White** |
| `FB_PAGE_ID` + `FB_PAGE_TOKEN` | ❌ needed | FB article + Reel | **White** |
| `JSON2VIDEO_KEY` | ❌ needed | video reel | **White** (paid) |
| `PEXELS_API_KEY` | optional | reel b-roll (else cards) | **White** (free) |
| `OPENAI_API_KEY` | not used | artwork is manual via ChatGPT web | — |

## Step-by-step (in the order that unlocks the most, fastest)

### 1. Notion — the keystone (≈5 min, free) → unlocks review + tweets→X + artwork

1. notion.so/my-integrations → **New integration** → copy the *Internal Integration Secret*.
2. Create a Notion page (e.g. "Gold Daily Brief") → its `•••` → **Connections** → add your integration.
3. Copy the page id: the 32-hex chunk in the page URL (`notion.so/<title>-<THIS_ID>`).
4. Set the secrets (see "Setting secrets safely" below):
   `NOTION_TOKEN` = the secret · `NOTION_BRIEF_PARENT` = the page id.
5. Trigger it: `gh workflow run daily-brief` (or wait for 07:30 ICT). A page appears with
   Tweets / FB article / video script / **4. Artwork** (prompt).

> After this, **tweets → X already work** (X creds are set): open the page, tick
> Tweet N, and within the hour `tweet-post` posts them.

### 2. Artwork (manual, uses your ChatGPT subscription — no key)

On the daily page's **4. Artwork** section: copy the prompt → generate in ChatGPT
(web) → drag the image into the page. `fb_post` + `tweet_post` attach it
automatically (FB photo + lead tweet). Nothing to set up.

### 3. Facebook (Meta app + Page token) → unlocks FB article + Reel posting

1. developers.facebook.com → create an app, connect your **Facebook Page**.
2. Generate a **long-lived Page access token** with `pages_manage_posts`
   (+ video permissions for Reels), Graph API Explorer → exchange for long-lived.
3. Set `FB_PAGE_ID` (numeric Page id) + `FB_PAGE_TOKEN`.

### 4. Video reel (JSON2Video, paid) — optional, do last

1. Sign up at json2video.com → API key → set `JSON2VIDEO_KEY`.
2. (Optional) `PEXELS_API_KEY` (free, pexels.com/api) for macro b-roll; else scenes
   use typographic cards + gradient. (Optional) `BRIEF_VOICE` to pick a Thai voice.

## Setting secrets safely

**Do not paste tokens into the chat.** In this session, prefix with `!` so it runs
on your machine and the value never reaches me — `gh secret set` prompts hidden:

```
! cd C:\Users\usEr\gold-news-pipeline
! gh secret set NOTION_TOKEN
! gh secret set NOTION_BRIEF_PARENT
! gh secret set FB_PAGE_ID
! gh secret set FB_PAGE_TOKEN
! gh secret set JSON2VIDEO_KEY
```
Verify names only (never values): `gh secret list`.

## First-live verification (do WITH me once keys are set)

These paths have never run against a live credential — expect a small fix on the
first run (all are best-effort + fall back, so nothing breaks hard):

- [ ] `daily-brief` → a Notion page is created (Notion write / block shape)
- [ ] tick Tweet N → `tweet-post` posts to X with the dropped artwork (X media upload)
- [ ] tick FB article → `fb-post` makes a photo post (`post_photo`, `get_page_image`)
- [ ] tick video → `video-brief` renders (JSON2Video payload) → `reel-post` (Reels 3-phase)

Trigger any manually: `gh workflow run <name>` (daily-brief / tweet-post / fb-post /
video-brief / reel-post). Watch logs: `gh run list` / `gh run view`.

## Housekeeping
- [ ] Delete the archived Notion proto page (marked 🗑️) — open it → `•••` → Delete.
- [ ] (Reliability) GitHub `schedule:` cron is throttled. For on-time daily firing,
  add cron-job.org jobs hitting each workflow's `workflow_dispatch` (like the news
  pipeline already does) — see `docs/DISPATCHER-CRON.md`.
