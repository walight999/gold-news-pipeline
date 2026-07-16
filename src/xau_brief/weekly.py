"""Phase B: Weekly Phase 4 brief composer.

Replaces goldbot-line/Code.gs Phase 4 weekly brief (Sat 06/07/08/09 ICT).
Reads FF prefetch data (already populated by ff_gas_weekly.yml in this repo)
+ weekly macro thesis + price recap → multi-card Flex carousel for LINE.

PARALLEL-RUN during P2 — GAS Phase 4 still primary for 2 Saturdays, then disable
GAS triggers (see MIGRATION.md Phase B).

Cron: Sat 06/07/08/09 ICT = Sat 23 UTC Fri / Sun 00/01/02 UTC
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone, timedelta
from typing import Any

ICT = timezone(timedelta(hours=7))
log = logging.getLogger(__name__)


def compose_weekly(slot: str | None = None) -> dict[str, Any]:
    """Build the weekly Phase 4 brief payload.

    slot: 'opening' (06:00) | 'preview' (07:00) | 'deep' (08:00) | 'close' (09:00)
    Auto-derived from current ICT hour if None.
    """
    now_ict = datetime.now(ICT)
    hour = now_ict.hour
    if slot is None:
        slot = ("opening" if hour == 6 else
                "preview" if hour == 7 else
                "deep"    if hour == 8 else
                "close"   if hour == 9 else "preview")

    # Pull FF data (already prefetched by ff_gas_weekly.yml into local cache)
    ff_events: list[dict] = []
    try:
        from src.ff_scraper import load_cached_week    # type: ignore
        ff_events = load_cached_week()
    except Exception as e:
        log.warning("FF cache load failed: %s", e)

    # Pull recent macro thesis (from event_state)
    thesis = None
    try:
        from src.store import get_recent_events    # type: ignore
        events = get_recent_events(limit=3)
        if events:
            thesis = events[0].get("title_th") or events[0].get("title")
    except Exception as e:
        log.warning("thesis fetch failed: %s", e)

    # Filter high-impact events for the week
    high_impact = [e for e in ff_events if e.get("impact") in ("high", "red", "H")]

    return {
        "slot":           slot,
        "title":          f"📅 Weekly Phase 4 · {slot.capitalize()} · {now_ict.strftime('%a %d %b %H:%M ICT')}",
        "thesis":         thesis,
        "event_count":    len(ff_events),
        "high_impact":    high_impact[:8],
        "ts":             now_ict.isoformat(),
        "phase":          "B",
        "note":           "PARALLEL-RUN — GAS Phase 4 still primary. This is verification stream.",
    }


def send_weekly(brief: dict[str, Any], target: str | None = None) -> bool:
    """Push weekly brief via line_client.py as Flex carousel."""
    target = target or os.getenv("LINE_BRIEF_TARGET") or os.getenv("LINE_NEWS_TARGET")
    if not target:
        log.error("no LINE target configured")
        return False
    try:
        from src.line_client import push_flex    # type: ignore
    except Exception as e:
        log.error("line_client import failed: %s", e)
        return False

    # Build per-slot Flex based on what we have
    body_contents: list = []
    if brief.get("thesis"):
        body_contents.append({"type": "text", "text": "Thesis:", "size": "xs", "color": "#888", "weight": "bold"})
        body_contents.append({"type": "text", "text": brief["thesis"][:200], "size": "sm", "color": "#222", "wrap": True})
    body_contents.append({"type": "separator", "margin": "md"})
    body_contents.append({"type": "text", "text": f"High-impact events this week: {len(brief.get('high_impact', []))}", "size": "sm", "color": "#444"})
    for e in brief.get("high_impact", [])[:5]:
        body_contents.append({
            "type": "text",
            "text": f"• {e.get('time_ict', '?')} {e.get('currency', '')} {e.get('event', '')[:60]}",
            "size": "xs", "color": "#333", "wrap": True, "margin": "sm",
        })

    flex = {
        "type": "bubble", "size": "mega",
        "header": {
            "type": "box", "layout": "vertical", "backgroundColor": "#9b59b6",
            "paddingAll": "14px",
            "contents": [
                {"type": "text", "text": "📅 Weekly Phase 4", "weight": "bold", "color": "#fff", "size": "lg"},
                {"type": "text", "text": brief["title"], "color": "#fff", "size": "xs"},
            ],
        },
        "body": {"type": "box", "layout": "vertical", "spacing": "sm", "contents": body_contents},
        "footer": {
            "type": "box", "layout": "vertical",
            "contents": [{"type": "text", "text": brief["note"], "size": "xxs", "color": "#999", "wrap": True}],
        },
    }

    try:
        push_flex(target, altText=f"Weekly Phase 4 — {brief['slot']}", flex=flex)
        log.info("weekly Phase 4 pushed: slot=%s events=%d", brief["slot"], brief["event_count"])
        return True
    except Exception as e:
        log.error("push_flex failed: %s", e)
        return False


def main() -> int:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    brief = compose_weekly()
    ok = send_weekly(brief)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
