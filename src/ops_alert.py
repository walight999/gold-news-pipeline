"""Independent ops-alert channel — a direct Telegram DM to the operator.

This is the ONLY delivery path that does not depend on LINE. Health alerts
normally push to LINE (LINE_HEALTH_TARGET), but the failure mode that matters
most — LINE itself down (token expired, channel disabled, or the free-tier
monthly quota exhausted, as happened 2026-07-18) — is exactly when a
LINE-delivered "LINE is failing" alert cannot arrive. A LINE outage silently
swallows its own alarm, so the Week Ahead vanished for a week with no warning.

So critical health alerts AND the LINE-quota early-warning ALSO fan out here,
over a transport LINE can't take down: a direct Telegram Bot API `sendMessage`
to the operator's private chat.

Env-gated + best-effort, exactly like telegram_news / macro_push:
  - OPS_TG_BOT_TOKEN — a Telegram bot token (may reuse @FinisitNews_bot's; the
    token identifies the bot, the chat_id below scopes it to a private DM).
  - OPS_TG_CHAT_ID   — the operator's PRIVATE chat id (a DM the operator has
    already opened with the bot, NOT the public subscriber group).
Unset → send_ops_alert() is a no-op returning False; never raises. This keeps
the ops channel decoupled from the public CHUM news bot (telegram_news.py),
whose webhooks fan out to all subscribers — ops alarms must stay private.
"""
from __future__ import annotations

import logging
import os
from typing import Any

import httpx
from tenacity import RetryError, retry, stop_after_attempt, wait_exponential

log = logging.getLogger(__name__)

# Health warning types that must reach the operator over the LINE-independent
# channel. All CRITICAL warnings (mirrored for defense-in-depth), plus
# `line_quota_high` — normally ROUTINE (it must NOT cost a LINE flex at 80%,
# exactly when quota is scarce) but it is the early warning that would have
# prevented the 2026-07-18 exhaustion, and Telegram costs nothing.
_ALWAYS_MIRROR: set[str] = {"line_quota_high"}


def _config() -> tuple[str, str] | None:
    token = os.environ.get("OPS_TG_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("OPS_TG_CHAT_ID", "").strip()
    if not token or not chat_id:
        return None
    return token, chat_id


def is_configured() -> bool:
    return _config() is not None


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=8),
       reraise=True)
def _post_message(token: str, chat_id: str, text: str) -> dict[str, Any]:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text[:4000], "disable_web_page_preview": True}
    with httpx.Client(timeout=15.0) as c:
        r = c.post(url, json=payload)
    if r.status_code >= 500 or r.status_code == 429:
        raise httpx.HTTPStatusError(f"telegram {r.status_code}", request=r.request, response=r)
    return {"status": r.status_code, "body": r.text}


def send_ops_alert(text: str) -> bool:
    """Send a plain-text ops alert to the operator's private Telegram chat.
    Returns True on HTTP 200, False if unconfigured or on any failure.
    Best-effort: never raises (a failed ops ping must not crash the watchdog)."""
    cfg = _config()
    if cfg is None:
        return False
    token, chat_id = cfg
    try:
        resp = _post_message(token, chat_id, text)
        return resp.get("status") == 200
    except RetryError as e:
        log.warning("ops alert failed after retries: %s", e)
        return False
    except httpx.HTTPError as e:
        log.warning("ops alert http error: %s", e)
        return False
    except Exception as e:  # noqa: BLE001 — an ops ping must never crash the caller
        log.warning("ops alert unexpected error: %s", e)
        return False


def should_mirror(warning_type: str) -> bool:
    """A warning is mirrored to the ops channel when it is CRITICAL (so the
    operator hears it even if LINE is the thing that's down) or it is an
    always-mirror early-warning (LINE quota climbing)."""
    from .health import is_critical_warning
    return is_critical_warning(warning_type) or warning_type in _ALWAYS_MIRROR


def mirror_health(pairs: list[tuple], *, kind: str = "alert") -> bool:
    """Mirror health warnings to the ops Telegram channel.

    `pairs` items may be (source_id, warning_type) or (source_id, warning_type,
    message). Only warnings passing should_mirror() are sent; if none qualify
    (or the channel is unconfigured), this is a no-op returning False.

    `kind` = "alert" | "recovered" — controls the header/emoji.
    """
    mirror = [p for p in pairs if should_mirror(p[1])]
    if not mirror:
        return False
    if kind == "recovered":
        header = "✅ Pipeline recovered"
    else:
        header = "🚨 Pipeline alert (LINE-independent)"
    lines = [header]
    for p in mirror:
        sid, wtype = p[0], p[1]
        msg = p[2] if len(p) > 2 and p[2] else ""
        label = f"• {wtype}"
        # Show the source only when it adds info — pipeline-level rows
        # (heartbeat / LINE / workflow) are already named by the warning type.
        if sid and sid not in ("_pipeline_heartbeat", "_line_push", "_workflow_failures"):
            label += f" [{sid}]"
        if msg:
            label += f" — {msg}"
        lines.append(label)
    return send_ops_alert("\n".join(lines))
