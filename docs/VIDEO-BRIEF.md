# Video Brief (Phase 3) — faceless auto-reel from the daily brief

Turns the daily brief's `video_script` into a **faceless vertical reel** (Thai AI
voice + center-screen karaoke subtitles over charts/b-roll) with a cloud render
API, for **one-tick approval** before it posts as a Facebook Reel.

Format chosen with White (2026-09-16): **faceless** (the brand's `no-ai-slop`
rule bans AI faces, so no talking avatar), **Thai AI TTS**, **karaoke captions**.

```
daily_brief_log.video_script  (already TTS-friendly Thai, [ฉาก N: cue] markers)
   │  video_brief.parse_scenes           → [{n, cue, narration}]
   ▼
   video_brief.build_payload             → JSON2Video "movie" JSON
   │  (one scene each: branded bg + Thai TTS voice; one global karaoke subtitles)
   ▼
   video_brief.submit_and_wait           → renders TTS + subtitles + assembly → MP4
   ▼
   [Phase 3b] MP4 → Notion review + video checkbox → --mode reel_post → FB Reel
```

**Why JSON2Video:** it does **TTS + auto-subtitles + assembly in ONE API call**,
so the pipeline only builds the movie JSON — the least code for "minimum human
work." Alternative: Creatomate + a separate Google/Azure TTS step (more wiring).

## What's shipped (this phase)

- `video_brief.parse_scenes()` — splits the script on `[ฉาก N: cue]` markers
  (tolerates `:` / `-` / space). **Verified on the real generated script (5
  scenes).**
- `video_brief.build_payload()` — vertical 1080×1920, per-scene Thai `voice`
  (TTS) element + one global center-screen karaoke `subtitles` element.
  Voice defaults to `th-TH-PremwadeeNeural` (override `BRIEF_VOICE`).
- `--mode video_brief` — reads the newest `daily_brief_log` row's `video_script`,
  parses + builds the payload, **always writes `snapshots/video_payload_<date>.json`**
  for inspection; `JSON2VIDEO_KEY` unset → DRY RUN (no render). With the key set,
  submits + polls JSON2Video for the MP4 URL.

## ⚠ Needs live verification

The `build_payload` JSON follows JSON2Video's v2 "movies" schema **to the best of
the docs** — the dry-run snapshot exists so the exact payload can be eyeballed and
corrected on the **first real render** (element field names / voice ids / subtitle
settings may need a tweak once tested against a live key). Nothing renders until
`JSON2VIDEO_KEY` is set.

## Go-live (White)

1. Create a **JSON2Video** account → API key. Add GH secret `JSON2VIDEO_KEY`.
   (Optional `BRIEF_VOICE` to pick a Thai voice.)
2. Trigger `--mode video_brief` (or its workflow) → check
   `snapshots/video_payload_<date>.json` + the rendered MP4. Adjust payload if the
   first render errors (field-name mismatches are the likely fix).

## Visual policy (IMPORTANT)

This is a **macro-explainer** channel, not a gold-chart channel. **Never** put a
**XAU/USD price chart, candlestick, or any trading-signal chart** in the public
video — the desk's gold charts (e.g. the ones pushed to LINE) are **internal
data** and must not appear. The `video_script` prompt enforces this: scene cues
describe macro-explainer visuals only (central-bank buildings, trading-floor /
markets b-roll, news desks, or typographic cards of PUBLIC numbers like
"US 10Y 5%").

## Phase 3b — SHIPPED

**Visuals** (`video_brief.resolve_visuals`): per scene, a **typographic data card**
if the cue quotes a public number (e.g. `'US 10Y 5%'`), else **Pexels b-roll**
(`pexels.search_broll`, free API, `PEXELS_API_KEY`) keyed off the cue's English
nouns (Fed building, trading floor), else the cue text on gradient. `PEXELS_API_KEY`
unset → cards + gradient only. **Never** queries gold/charts (macro nouns only).

**Review → post loop:**
```
video_brief renders MP4 → stamps daily_brief_log.video_url + attaches the MP4 to
the brief's Notion page (fb_publish.attach_video_to_notion)
   │  operator watches → ticks "อนุมัติบทพูด video"
   ▼
--mode reel_post (hourly reel_post.yml): newest row with video_url + empty
reel_posted + ticked video checkbox → fb_publish.post_reel() publishes the MP4 as
a FB Reel (3-phase /{page}/video_reels) → stamps reel_posted. Fail-closed.
```

### Go-live (White) — Phase 3b
- **Pexels** (optional, for b-roll): free key at pexels.com/api → GH secret
  `PEXELS_API_KEY`. Without it, scenes use cards/gradient (still a valid reel).
- **FB Reels:** reuse `FB_PAGE_ID` + `FB_PAGE_TOKEN` (needs `pages_manage_posts`
  + video permissions; Meta app review may apply to video/Reels).

> ⚠ `post_reel` follows the documented `/{page}/video_reels` 3-phase (start →
> upload-by-`file_url` → finish) flow but is **UNVERIFIED against a live token** —
> the resumable Reels flow is finicky; `reel_post` leaves the row unposted (retries)
> on failure, so verify + adjust on the first real post. Same for
> `attach_video_to_notion` (a raw .mp4 external video block may need an embeddable
> host).
