"""Facebook Page auto-publish for the daily brief (Phase 2).

The daily brief lands on a Notion page whose FB article carries an approval
checkbox ("อนุมัติบทความ Facebook"). This module reads that checkbox back and,
if it is ticked, posts the stored article to the brand Facebook Page via the
Meta Graph API, then the caller stamps the row as posted.

Env-gated + best-effort, like the rest of the pipeline:
  - FB_PAGE_ID + FB_PAGE_TOKEN unset → no-op (the caller logs + exits 0)
  - NOTION_TOKEN unset               → can't read approval → treated as not-approved
Nothing here raises to the run loop.

Go-live (White): create a Meta app + Facebook Page, generate a long-lived Page
access token with `pages_manage_posts` + `pages_read_engagement`, then set the
GH secrets FB_PAGE_ID + FB_PAGE_TOKEN. See docs/DAILY-BRIEF.md.
"""
from __future__ import annotations

import logging

log = logging.getLogger("fb_publish")

GRAPH_VERSION = "v21.0"
NOTION_VERSION = "2022-06-28"
FB_APPROVE_LABEL = "อนุมัติบทความ Facebook"
VIDEO_APPROVE_LABEL = "อนุมัติบทพูด video"


def _notion_children(page_id: str, token: str) -> list[dict]:
    """All top-level blocks of a Notion page (paginated)."""
    import httpx

    headers = {"Authorization": f"Bearer {token}",
               "Notion-Version": NOTION_VERSION}
    out: list[dict] = []
    cursor = None
    with httpx.Client(timeout=30) as c:
        while True:
            params = {"page_size": 100}
            if cursor:
                params["start_cursor"] = cursor
            r = c.get(f"https://api.notion.com/v1/blocks/{page_id}/children",
                      headers=headers, params=params)
            r.raise_for_status()
            j = r.json()
            out.extend(j.get("results", []))
            if not j.get("has_more"):
                break
            cursor = j.get("next_cursor")
    return out


def _todo_checked(blocks: list[dict], label: str) -> bool:
    """True if a to_do block whose text contains `label` is checked."""
    for b in blocks:
        if b.get("type") != "to_do":
            continue
        rt = b.get("to_do", {}).get("rich_text", [])
        text = "".join(x.get("plain_text", "") for x in rt)
        if label in text:
            return bool(b.get("to_do", {}).get("checked"))
    return False


def is_approved(page_id: str, token: str, label: str) -> bool:
    """Read an approval checkbox (`label`) off the brief's Notion page. Returns
    False (fail-closed) on any error — we never auto-post on doubt."""
    if not page_id or not token:
        return False
    try:
        return _todo_checked(_notion_children(page_id, token), label)
    except Exception:  # noqa: BLE001 — approval read is best-effort, fail closed
        log.exception("fb_publish: notion approval read failed")
        return False


def is_fb_approved(page_id: str, token: str,
                   label: str = FB_APPROVE_LABEL) -> bool:
    """The FB article checkbox — thin wrapper over is_approved."""
    return is_approved(page_id, token, label)


def post_to_page(message: str, *, page_id: str, token: str) -> str | None:
    """Post `message` to the Facebook Page feed. Returns the post URL, or None
    on failure (caller leaves the row unposted to retry next run)."""
    import httpx

    if not (message and page_id and token):
        return None
    try:
        with httpx.Client(timeout=30) as c:
            r = c.post(f"https://graph.facebook.com/{GRAPH_VERSION}/{page_id}/feed",
                       data={"message": message, "access_token": token})
            r.raise_for_status()
            post_id = r.json().get("id", "")
            return f"https://www.facebook.com/{post_id}" if post_id else None
    except Exception:  # noqa: BLE001 — one bad post must not stop the run
        log.exception("fb_publish: graph post failed")
        return None


def post_photo(image: bytes, *, page_id: str, token: str,
               message: str = "") -> str | None:
    """Post a photo (uploaded bytes) with a caption to the FB Page. Uploading the
    bytes directly is more reliable than a `url` (Drive hotlinks are flaky for
    FB's fetcher). Returns the post URL, or None."""
    if not (image and page_id and token):
        return None
    import httpx

    try:
        with httpx.Client(timeout=60) as c:
            r = c.post(f"https://graph.facebook.com/{GRAPH_VERSION}/{page_id}/photos",
                       data={"message": message, "access_token": token},
                       files={"source": ("artwork.png", image, "image/png")})
            r.raise_for_status()
            j = r.json()
            pid = j.get("post_id") or j.get("id", "")
            return f"https://www.facebook.com/{pid}" if pid else None
    except Exception:  # noqa: BLE001 — one bad post must not stop the run
        log.exception("fb_publish: photo post failed")
        return None


def attach_image_to_notion(page_id: str, token: str, image_url: str,
                           caption: str = "🎨 artwork (ตรวจก่อนโพส)") -> bool:
    """Append the artwork as a Notion image block (+ caption) to the brief page
    so the operator sees it before approving. Best-effort."""
    if not (page_id and token and image_url):
        return False
    import httpx

    headers = {"Authorization": f"Bearer {token}",
               "Notion-Version": NOTION_VERSION, "Content-Type": "application/json"}
    children = [
        {"object": "block", "type": "paragraph",
         "paragraph": {"rich_text": [{"type": "text", "text": {"content": caption}}]}},
        {"object": "block", "type": "image",
         "image": {"type": "external", "external": {"url": image_url}}},
    ]
    try:
        with httpx.Client(timeout=30) as c:
            c.patch(f"https://api.notion.com/v1/blocks/{page_id}/children",
                    headers=headers, json={"children": children}).raise_for_status()
        return True
    except Exception:  # noqa: BLE001
        log.exception("fb_publish: attach image to notion failed")
        return False


def attach_video_to_notion(page_id: str, token: str, mp4_url: str,
                           caption: str = "🎬 วิดีโอที่เรนเดอร์แล้ว (ดูก่อนติ๊กอนุมัติ)") -> bool:
    """Append the rendered MP4 as a video block (+ caption) to the brief's Notion
    page so the operator can watch it before ticking the video approval box.
    Best-effort — never raises."""
    if not (page_id and token and mp4_url):
        return False
    import httpx

    headers = {"Authorization": f"Bearer {token}",
               "Notion-Version": NOTION_VERSION, "Content-Type": "application/json"}
    children = [
        {"object": "block", "type": "paragraph",
         "paragraph": {"rich_text": [{"type": "text", "text": {"content": caption}}]}},
        {"object": "block", "type": "video",
         "video": {"type": "external", "external": {"url": mp4_url}}},
    ]
    try:
        with httpx.Client(timeout=30) as c:
            c.patch(f"https://api.notion.com/v1/blocks/{page_id}/children",
                    headers=headers, json={"children": children}).raise_for_status()
        return True
    except Exception:  # noqa: BLE001
        log.exception("fb_publish: attach video to notion failed")
        return False


def post_reel(video_url: str, *, page_id: str, token: str,
              description: str = "") -> str | None:
    """Publish a hosted MP4 as a Facebook Reel via the 3-phase video_reels flow
    (start → upload-by-file_url → finish). Returns a reel marker/URL, or None.

    ⚠ UNVERIFIED against a live token — the Reels resumable flow is finicky; this
    follows the documented shape and the caller keeps the row unposted on None so
    it retries. Verify + adjust on the first real post."""
    if not (video_url and page_id and token):
        return None
    import httpx

    base = f"https://graph.facebook.com/{GRAPH_VERSION}/{page_id}/video_reels"
    try:
        with httpx.Client(timeout=60) as c:
            # Phase 1 — start: get a video_id + upload_url
            s = c.post(base, data={"upload_phase": "start", "access_token": token})
            s.raise_for_status()
            sj = s.json()
            video_id = sj.get("video_id")
            upload_url = sj.get("upload_url")
            if not (video_id and upload_url):
                log.warning("post_reel: no video_id/upload_url in start response")
                return None
            # Phase 2 — upload the hosted file by URL
            u = c.post(upload_url,
                       headers={"Authorization": f"OAuth {token}",
                                "file_url": video_url})
            u.raise_for_status()
            # Phase 3 — finish + publish
            f = c.post(base, data={"upload_phase": "finish", "video_id": video_id,
                                   "video_state": "PUBLISHED", "description": description,
                                   "access_token": token})
            f.raise_for_status()
            return f"https://www.facebook.com/reel/{video_id}"
    except Exception:  # noqa: BLE001 — one bad reel must not stop the run
        log.exception("fb_publish: reel publish failed")
        return None
