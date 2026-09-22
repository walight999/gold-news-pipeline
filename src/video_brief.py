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

_CARD_RE = re.compile(r"['\"“”‘’]([^'\"“”‘’]{2,60})['\"“”‘’]")


def extract_card_text(cue: str) -> str | None:
    """A quoted string in the cue (e.g. 'US 10Y 5%') = a typographic data card."""
    m = _CARD_RE.search(cue or "")
    return m.group(1).strip() if m else None


def broll_query(cue: str) -> str:
    """English keywords for a Pexels macro b-roll search, taken from the Latin
    runs in the cue (proper nouns like 'Federal Reserve', 'Trading Floor'). Falls
    back to a Thai→English macro map, then a generic default. Never returns a
    gold/chart query — the prompt keeps cues macro-only."""
    toks = re.findall(r"[A-Za-z][A-Za-z.&/]*(?:\s+[A-Za-z][A-Za-z.&/]*)*", cue or "")
    q = " ".join(t.strip() for t in toks if len(t.strip()) > 2).strip()
    if not q:
        for th, en in (("ธนาคารกลาง", "central bank"), ("ตลาดหุ้น", "stock market"),
                       ("ตลาด", "financial markets"), ("เศรษฐกิจ", "economy")):
            if th in (cue or ""):
                q = en
                break
    q = re.sub(r"\s+", " ", q).strip()
    return q or "financial markets"


def resolve_visuals(scenes: list[dict[str, Any]], *,
                    pexels_key: str | None = None) -> list[dict[str, str]]:
    """Pick a background per scene: a typographic CARD if the cue quotes a public
    number, else Pexels B-ROLL (if a key is set and a clip is found), else the
    cue TEXT on gradient. Never uses XAU/USD charts or internal desk imagery."""
    from . import pexels

    out: list[dict[str, str]] = []
    for s in scenes:
        cue = s.get("cue", "")
        card = extract_card_text(cue)
        if card:
            out.append({"type": "card", "value": card})
            continue
        url = (pexels.search_broll(broll_query(cue), api_key=pexels_key)
               if pexels_key else None)
        out.append({"type": "broll", "value": url} if url
                   else {"type": "text", "value": cue})
    return out


def build_payload(scenes: list[dict[str, Any]], *, title: str,
                  voice: str | None = None,
                  visuals: list[dict[str, str]] | None = None) -> dict[str, Any]:
    """Assemble the JSON2Video movie JSON: one scene per parsed scene with a
    background (b-roll video / typographic card / gradient text per `visuals`) +
    a `voice` element (Thai TTS). One global `subtitles` element renders
    center-screen karaoke captions from the movie's voices."""
    # `or` chain (not get's default) so an empty BRIEF_VOICE secret still falls
    # back to DEFAULT_VOICE instead of sending "".
    voice = voice or os.environ.get("BRIEF_VOICE") or DEFAULT_VOICE
    movie_scenes = []
    for i, s in enumerate(scenes):
        vis = (visuals[i] if visuals and i < len(visuals)
               else {"type": "text", "value": s["cue"]})
        elements: list[dict[str, Any]] = []
        if vis.get("type") == "broll" and vis.get("value"):
            bg = "#000000"
            elements.append({"type": "video", "src": vis["value"],
                             "resize": "cover", "muted": True, "volume": 0})
        elif vis.get("type") == "card":
            bg = "#0B1F3A"
            # A text element's `settings` are REAL CSS property names applied
            # as-is (JSON2Video docs) — so it's `color` not `font-color`, and
            # `font-size` needs a unit; unknown keys are silently ignored (the
            # card would render in the default colour).
            elements.append({"type": "text", "text": vis.get("value", ""),
                             "position": "top-center",
                             "settings": {"font-size": "72px", "color": "#FFD34D",
                                          "font-family": "Sarabun"}})
        else:
            bg = "#0B1F3A"
            elements.append({"type": "text", "text": vis.get("value", s["cue"]),
                             "position": "top-center",
                             "settings": {"font-size": "42px", "color": "#8AA0BF"}})
        elements.append({"type": "voice", "text": s["narration"], "voice": voice})
        movie_scenes.append({
            "comment": f"ฉาก {s['n']}: {s['cue']}"[:120],
            "background-color": bg,
            "elements": elements,
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

    headers = {"x-api-key": (api_key or "").strip(),
               "Content-Type": "application/json"}
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
