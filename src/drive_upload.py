"""Google Drive image host for post artwork (Slice 2), reusing the pipeline's
existing service account (GSHEET_CREDS) — no new vendor.

Uploads PNG bytes, makes the file anyone-with-link readable, and returns the
Drive file id. Posters DOWNLOAD the bytes back (authenticated — reliable) and
upload them straight to FB/X, because Drive public hotlinks are flaky for
third-party fetchers; the `view_url` is only for the Notion review embed.

Env-gated on GSHEET_CREDS (already set). Best-effort — returns None on failure.
"""
from __future__ import annotations

import json
import logging
import os

log = logging.getLogger("drive_upload")

_SCOPES = ["https://www.googleapis.com/auth/drive"]
_UPLOAD_URL = "https://www.googleapis.com/upload/drive/v3/files?uploadType=media"
_FILES_URL = "https://www.googleapis.com/drive/v3/files"


def _access_token() -> str | None:
    """Mint an OAuth token from the service account creds (google-auth ships with
    gspread, so no new dependency)."""
    raw = os.environ.get("GSHEET_CREDS")
    if not raw:
        return None
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.service_account import Credentials
        creds = Credentials.from_service_account_info(json.loads(raw), scopes=_SCOPES)
        creds.refresh(Request())
        return creds.token
    except Exception:  # noqa: BLE001
        log.exception("drive_upload: token mint failed")
        return None


def view_url(file_id: str) -> str:
    """A shareable Drive view URL (for the Notion review embed)."""
    return f"https://drive.google.com/uc?export=view&id={file_id}"


def upload_png(data: bytes, *, name: str) -> str | None:
    """Upload PNG bytes to Drive, make it anyone-with-link readable, return the
    file id (or None). Best-effort."""
    if not data:
        return None
    token = _access_token()
    if not token:
        return None
    import httpx

    try:
        with httpx.Client(timeout=60) as c:
            up = c.post(_UPLOAD_URL,
                        headers={"Authorization": f"Bearer {token}",
                                 "Content-Type": "image/png"},
                        content=data)
            up.raise_for_status()
            file_id = up.json().get("id")
            if not file_id:
                return None
            # rename + make public (best-effort; upload already succeeded)
            c.patch(f"{_FILES_URL}/{file_id}",
                    headers={"Authorization": f"Bearer {token}",
                             "Content-Type": "application/json"},
                    json={"name": name})
            c.post(f"{_FILES_URL}/{file_id}/permissions",
                   headers={"Authorization": f"Bearer {token}",
                            "Content-Type": "application/json"},
                   json={"role": "reader", "type": "anyone"})
            return file_id
    except Exception:  # noqa: BLE001 — hosting is best-effort
        log.exception("drive_upload: upload failed")
        return None


def download(file_id: str) -> bytes | None:
    """Authenticated download of the file bytes (reliable, unlike hotlinking)."""
    if not file_id:
        return None
    token = _access_token()
    if not token:
        return None
    import httpx

    try:
        with httpx.Client(timeout=60) as c:
            r = c.get(f"{_FILES_URL}/{file_id}",
                      headers={"Authorization": f"Bearer {token}"},
                      params={"alt": "media"})
            r.raise_for_status()
            return r.content
    except Exception:  # noqa: BLE001
        log.exception("drive_upload: download failed")
        return None
