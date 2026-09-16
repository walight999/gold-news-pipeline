# Post Artwork

Branded editorial **artwork** (e.g. a **daily economic-calendar poster**) to
attach to Twitter / Facebook posts.

## PRIMARY flow — manual ChatGPT (uses the operator's subscription, no API cost)

Chosen 2026-09-16: the operator has a ChatGPT subscription and generates artwork
in the **ChatGPT web UI** (covered by the subscription) rather than the paid API.

```
daily_brief embeds the artwork PROMPT (the day's economic-calendar poster, built
from FF calendar events) as a copyable code block in the Notion review page,
under a "4. Artwork" section
   → operator copies it → generates in ChatGPT (web) → drags the image into the
     same Notion page
   → at post time, fb_post / tweet_post read the FIRST image block off the page
     (fb_publish.get_page_image), download the bytes, and attach it to the FB
     photo post + the lead tweet
```

No OpenAI API key, no extra hosting — Notion holds the image; the posters read it
back at post time. `image_gen.calendar_artwork_prompt` builds the prompt.

## OPTIONAL — automatic OpenAI API path (dormant)

`--mode artwork` + `image_gen.generate()` can auto-generate via the OpenAI Images
API (raw HTTP, `gpt-image-1`/`dall-e-3`) + host on Drive, IF `OPENAI_API_KEY` is
set. Left env-gated and OFF (the operator prefers the subscription/web path). The
posters fall back to a Drive-hosted `artwork` file id if one is present.

## Brand guardrail (IMPORTANT)

The brand's `no-ai-slop` rule **bans AI-generated human faces**. Every prompt
appends `image_gen.BRAND_STYLE`, which forbids **realistic faces/people, real
logos/trademarks, and gibberish text** — the output is illustrative/infographic
(charts as line-art, central-bank columns, clocks, flag color-blocks), never a
fake person. Artwork (graphics) is allowed; faces are not.

## Slice 1 — SHIPPED (generation)

- `image_gen.calendar_artwork_prompt(events, date_label)` — pure prompt builder
  from the day's high/medium-impact `calendar.CalEvent`s (time + country + title).
- `image_gen.brief_artwork_prompt(theme)` — pure prompt builder from the daily
  brief's theme.
- `image_gen.generate(prompt, api_key, size, model)` — one image → PNG bytes
  (handles both gpt-image-1 `b64_json` and dall-e-3 `url`). `save_png()`.
- `--mode artwork` — fetches today's calendar, builds the calendar prompt, and
  (with the key) generates + saves `snapshots/artwork_<date>.png`. `OPENAI_API_KEY`
  unset → DRY RUN (logs the prompt, no spend). **Verified end-to-end**: real FF
  calendar → real prompt with the day's events + brand guardrail.

### Env

| var | purpose |
|---|---|
| `OPENAI_API_KEY` | image generation — **UNSET ⇒ DRY RUN** |
| `IMAGE_MODEL` | default `gpt-image-1` (override e.g. `dall-e-3`) |
| `IMAGE_SIZE` | default `1024x1024` (both models support it) |

> `gpt-image-1` may require OpenAI org verification; if it 403s, set
> `IMAGE_MODEL=dall-e-3`.

## Slice 2 — SHIPPED (host + attach)

**Hosting = Google Drive** via the existing service account (`drive_upload.py`,
reuses `GSHEET_CREDS`, no new vendor):
- `upload_png(data, name)` → uploads + makes anyone-with-link → returns the Drive
  **file id**; `view_url(id)` for the Notion embed; `download(id)` for an
  **authenticated** byte fetch (reliable — Drive public hotlinks are flaky for
  FB/X fetchers, so posters download bytes + upload them directly).

**Flow:**
```
--mode artwork: calendar prompt → OpenAI image → save snapshot → Drive upload
   → stamp daily_brief_log.artwork (file id) → attach image to the Notion page
   ↓  operator sees the artwork next to the approval checkboxes
fb_post: if the row has `artwork`, download bytes from Drive → post as a PHOTO
   (fb_publish.post_photo, /{page}/photos, article = caption); else text post.
```
`daily_brief_log` gained an `artwork` column. Workflow `artwork.yml` (00:55 UTC,
after the brief). `social_feed.x_post(text, media=…)` now accepts image bytes
(v1.1 media upload) so the tweet path is ready for artwork too.

### Env (slice 2)

| var | purpose |
|---|---|
| `GSHEET_CREDS` | Drive upload/download (already set; uses `drive` scope) |
| `NOTION_TOKEN` | attach the artwork image block to the review page |
| `FB_PAGE_ID`+`FB_PAGE_TOKEN` | photo post (reused from Phase 2) |

> ⚠ `drive_upload` (service-account public share may hit org policy) and
> `post_photo` are **UNVERIFIED against live creds/token** — best-effort, flagged;
> `fb_post` falls back to a text post if the photo path fails. Verify on first run.

## Twitter — SHIPPED (`--mode tweet_post`)

The daily-brief tweets now auto-post to X with the artwork:
- `daily_brief_log` gained `tweets` (JSON list) + `tweets_posted` (csv of posted
  indices). `log_brief` stores the tweet list.
- `fb_publish.checked_tweet_indices(page_id, token)` reads which **'Tweet N'**
  checkboxes are ticked on the Notion page (fail-closed).
- `--mode tweet_post` (hourly `tweet_post.yml`): for the newest row, posts each
  ticked-but-unposted tweet via `social_feed.x_post`, **attaching the day's
  artwork (downloaded from Drive) to the LEAD tweet only**, and appends the index
  to `tweets_posted` so incremental ticks work. Env-gated X creds + `NOTION_TOKEN`.

So the full one-Notion-page review now drives all channels: tick **Tweet N** →
X (lead tweet carries the artwork); tick **FB article** → Facebook photo post;
tick **video** → FB Reel.
