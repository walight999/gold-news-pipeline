"""Pexels stock b-roll lookup for the video brief (Phase 3b).

Given an English search query (derived from a scene's visual cue), returns a
portrait stock VIDEO url for the reel background. Free API, env-gated on
PEXELS_API_KEY. Best-effort — returns None on any miss so the caller falls back
to a typographic card / gradient background.

Only generic MACRO b-roll is ever requested (central-bank buildings, trading
floors, markets) — never a gold price chart or any internal desk imagery
(see docs/VIDEO-BRIEF.md visual policy).
"""
from __future__ import annotations

import logging

log = logging.getLogger("pexels")

SEARCH_URL = "https://api.pexels.com/videos/search"


def search_broll(query: str, *, api_key: str,
                 max_height: int = 1920) -> str | None:
    """Return a portrait stock-video file URL for `query`, or None. Picks the
    largest portrait mp4 no taller than `max_height` (keeps the render light)."""
    if not (query and api_key):
        return None
    import httpx

    try:
        with httpx.Client(timeout=20) as c:
            r = c.get(SEARCH_URL,
                      headers={"Authorization": (api_key or "").strip()},
                      params={"query": query, "orientation": "portrait",
                              "per_page": 3, "size": "medium"})
            r.raise_for_status()
            videos = r.json().get("videos") or []
            best_url, best_h = None, 0
            for v in videos:
                for f in v.get("video_files", []):
                    if f.get("file_type") != "video/mp4":
                        continue
                    h = f.get("height") or 0
                    w = f.get("width") or 0
                    if h <= w:                       # want portrait
                        continue
                    if h <= max_height and h > best_h and f.get("link"):
                        best_url, best_h = f["link"], h
            return best_url
    except Exception:  # noqa: BLE001 — b-roll is optional, degrade gracefully
        log.exception("pexels: search failed for %r", query)
        return None
