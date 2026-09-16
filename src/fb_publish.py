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


def is_fb_approved(page_id: str, token: str,
                   label: str = FB_APPROVE_LABEL) -> bool:
    """Read the FB article approval checkbox off the brief's Notion page.
    Returns False (fail-closed) on any error — we never auto-post on doubt."""
    if not page_id or not token:
        return False
    try:
        return _todo_checked(_notion_children(page_id, token), label)
    except Exception:  # noqa: BLE001 — approval read is best-effort, fail closed
        log.exception("fb_publish: notion approval read failed")
        return False


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
