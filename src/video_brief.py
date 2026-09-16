"""Video brief (Phase 3) — daily_brief's video_script → a faceless vertical
reel (charts/b-roll + Thai AI voice + karaoke subtitles) via a cloud render API
(JSON2Video), for one-tick approval before it posts as a FB Reel.

Format chosen with White (2026-09-16): FACELESS (no AI avatar — the brand's
no-ai-slop rule bans AI faces), Thai AI TTS, center-screen karaoke subtitles.
JSON2Video does TTS + subtitles + assembly in ONE API call, so the pipeline just
builds the movie JSON and submits it.

Env-gated + best-effort, like the rest of the pipeline:
  - JSON2VIDEO_KEY unset → DRY RUN: the movie JSON is written to snapshots/ and
    logged, nothing is rendered, exit 0.
Nothing here crashes the schedule.

⚠ The JSON2Video payload below follows their v2 "movies" schema to the best of
the docs; the DRY-RUN snapshot exists so the exact payload can be eyeballed and
corrected against the live API on the first real render (env-gated until then).
"""
from __future__ import annotations

import logging
import os
import re
from typing import Any

log = logging.getLogger("video_brief")

# Vertical reel canvas.
WIDTH, HEIGHT = 1080, 1920
# Thai neural voices (Azure, exposed through JSON2Video). Override with BRIEF_VOICE.
DEFAULT_VOICE = "th-TH-PremwadeeNeural"
API_URL = "https://api.json2video.com/v2/movies"

_SCENE_RE = re.compile(r"^\[\s*ฉาก\s*(\d+)\s*[:：\-]?\s*(.*?)\s*\]$")


# --------------------------------------------------------------------------
# 1. Director — parse the script into scenes
# --------------------------------------------------------------------------

def parse_scenes(script: str) -> list[dict[str, Any]]:
    """Split a video_script into scenes. A scene begins at a '[ฉาก N: cue]'
    marker line; every non-empty line until the next marker is its narration.
    Lines before the first marker are ignored. Returns
    [{"n": int, "cue": str, "narration": str}]."""
    scenes: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None
    for raw in (script or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        m = _SCENE_RE.match(line)
        if m:
            if cur:
                scenes.append(cur)
            cur = {"n": int(m.group(1)), "cue": m.group(2).strip(), "narration": ""}
        elif cur is not None:
            cur["narration"] = (cur["narration"] + " " + line).strip()
    if cur:
        scenes.append(cur)
    # Drop scenes that ended up with no spoken line (a stray marker).
    return [s for s in scenes if s["narration"]]


# --------------------------------------------------------------------------
# 2. Build the JSON2Video movie payload (TTS + subtitles + assembly)
# --------------------------------------------------------------------------

def build_payload(scenes: list[dict[str, Any]], *, title: str,
                  voice: str | None = None) -> dict[str, Any]:
    """Assemble the JSON2Video movie JSON: one scene per parsed scene, each a
    branded background + a `voice` element (Thai TTS of the narration). A single
    global `subtitles` element renders center-screen karaoke captions from the
    movie's voices. Backgrounds are a dark brand gradient in v1; v1.1 swaps in a
    per-scene chart image (from price_feed) or Pexels b-roll keyed off `cue`."""
    voice = voice or os.environ.get("BRIEF_VOICE", DEFAULT_VOICE)
    movie_scenes = []
    for s in scenes:
        movie_scenes.append({
            "comment": f"ฉาก {s['n']}: {s['cue']}"[:120],
            "background-color": "#0B1F3A",
            "elements": [
                # v1.1 TODO: replace with {"type":"image","src": chart_or_broll_url}
                {"type": "text",
                 "text": s["cue"],
                 "style": "004",
                 "position": "top-center",
                 "settings": {"font-size": "42", "font-color": "#8AA0BF"}},
                {"type": "voice", "text": s["narration"], "voice": voice},
            ],
        })
    return {
        "comment": title[:120],
        "resolution": "custom",
        "width": WIDTH,
        "height": HEIGHT,
        "quality": "high",
        "scenes": movie_scenes,
        # Global karaoke captions auto-generated from the voice tracks.
        "elements": [{
            "type": "subtitles",
            "settings": {
                "style": "classic",
                "position": "center-center",
                "font-family": "Sarabun",
                "font-size": 56,
                "word-color": "#FFD34D",     # highlighted word (karaoke)
                "line-color": "#FFFFFF",
                "outline-width": 4,
                "outline-color": "#000000",
                "max-words-per-line": 6,
            },
        }],
    }


# --------------------------------------------------------------------------
# 3. Submit to JSON2Video (env-gated) and wait for the MP4
# --------------------------------------------------------------------------

def submit_and_wait(payload: dict[str, Any], *, api_key: str,
                    poll_sec: int = 10, timeout_sec: int = 600) -> str | None:
    """POST the movie to JSON2Video, poll until done, return the MP4 URL (or
    None on failure/timeout). Best-effort — never raises to the run loop."""
    import time

    import httpx

    headers = {"x-api-key": api_key, "Content-Type": "application/json"}
    try:
        with httpx.Client(timeout=30) as c:
            r = c.post(API_URL, headers=headers, json=payload)
            r.raise_for_status()
            project = (r.json().get("project") or r.json().get("id") or "")
            if not project:
                log.warning("video_brief: no project id in submit response")
                return None
            waited = 0
            while waited < timeout_sec:
                time.sleep(poll_sec)
                waited += poll_sec
                g = c.get(API_URL, headers=headers, params={"project": project})
                g.raise_for_status()
                body = g.json()
                movie = body.get("movie") or body
                status = movie.get("status")
                if status == "done":
                    return movie.get("url")
                if status == "error":
                    log.warning("video_brief: render error: %s", movie.get("message"))
                    return None
            log.warning("video_brief: render timed out after %ss", timeout_sec)
            return None
    except Exception:  # noqa: BLE001 — render is best-effort
        log.exception("video_brief: submit/poll failed")
        return None
