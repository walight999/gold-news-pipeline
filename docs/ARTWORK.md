# Post Artwork (OpenAI image generation)

Generates branded editorial **artwork** to attach to Twitter / Facebook posts —
e.g. a **daily economic-calendar poster** built from the day's high-impact
ForexFactory events. Uses the OpenAI Images API over raw HTTP (no extra SDK),
env-gated on `OPENAI_API_KEY`, best-effort.

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

## Slice 2 — TODO (put the artwork ON the posts)

The generator is done; attaching it to live posts needs **hosting** so the same
image can be shown in Notion review + attached to the FB post + the tweet:

1. **Host** the PNG → a stable URL. Options (decision needed):
   - **Google Drive** via the existing service account (reuse `GSHEET_CREDS`, add
     `drive.file` scope, share anyone-with-link) — no new vendor.
   - **Cloudinary / imgbb / S3-compatible** — a dedicated image host.
2. **Attach to review:** add the artwork as a Notion image block on the brief page
   (next to the approval checkboxes).
3. **Attach to posts:**
   - **Facebook:** `POST /{page}/photos` with `url` + `message` (single-photo
     post) instead of `/feed`.
   - **Twitter:** upload media (tweepy `media_upload` / v2 media) → attach
     `media_ids` to the tweet.
4. **State:** store `artwork_url` on the `daily_brief_log` row so `fb_post` /
   the tweet path pick it up.

Generate the artwork ONCE per brief (consistency + cost) and reuse the hosted URL
across all channels.
