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

## Remaining — Phase 3b

- **Visuals v1.1:** replace the text-on-gradient background with a per-scene
  **XAU/USD chart image** (generated from `price_feed` data — on-brand) and/or
  **Pexels b-roll** keyed off each scene's `cue`.
- **Review → post loop:** attach the rendered MP4 to the brief's Notion page with
  a video approval checkbox (reuse the existing "อนุมัติบทพูด video" to_do), then
  `--mode reel_post` reads that checkbox (like `fb_post`) and publishes the MP4 as
  a **FB Reel** via the Graph API resumable-upload flow (`/{page}/video_reels`).
  Reels publishing needs `pages_manage_posts` + possibly Meta app review for video.

Until 3b lands, the MP4 URL is logged; the operator posts the reel manually.
