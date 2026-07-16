"""Phase A: XAUUSD daily brief composer.

Composes a 3x daily brief (07/15/23 ICT) using gold-news-pipeline's existing
infrastructure — line_client.py for push, line_flex.py for layout, scoped news
from event_state, plus a price snapshot. Runs as GHA cron (xau_daily_brief.yml).

PARALLEL-RUN MODE during P1 — do NOT kill Make scenario 5656446 until output
verified equivalent over 7 days. See MIGRATION.md Phase A.

Cron times (UTC):  0 0,8,16 * * *   → ICT 07:00, 15:00, 23:00 (UTC+7)
Manual trigger:    workflow_dispatch
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone, timedelta
from typing import Any

ICT = timezone(timedelta(hours=7))
log = logging.getLogger(__name__)


def compose_brief(slot: str | None = None) -> dict[str, Any]:
    """Build the brief payload for the current slot.

    slot: 'morning' | 'mid' | 'evening' (auto-detected from current ICT hour if None)
    Returns a dict consumable by send_brief() — has keys: title, summary, sections, slot, ts.
    """
    now_ict = datetime.now(ICT)
    hour = now_ict.hour
    if slot is None:
        slot = "morning" if hour < 12 else "mid" if hour < 21 else "evening"

    # Try to gather signals from existing pipeline state (best-effort, degrades gracefully)
    headlines: list[dict] = []
    price_snapshot: dict | None = None
    macro_thesis: str | None = None
    try:
        from src.store import get_recent_events     # type: ignore
        events = get_recent_events(limit=5)
        for e in events:
            headlines.append({
                "title": e.get("title_th") or e.get("title"),
                "score": e.get("score", 0),
                "source": e.get("source"),
            })
    except Exception as e:
        log.warning("store import/fetch failed: %s", e)

    try:
        from src.price_feed import get_xau_spot     # type: ignore
        price_snapshot = get_xau_spot()
    except Exception as e:
        log.warning("price_feed failed: %s", e)

    # Macro thesis placeholder — Phase C will wire to actual signal model
    macro_thesis = "(Phase A — Macro thesis composer not yet wired; using stub)"

    return {
        "title":    f"XAU Daily Brief · {slot.capitalize()} · {now_ict.strftime('%a %d %b %H:%M ICT')}",
        "slot":     slot,
        "ts":       now_ict.isoformat(),
        "price":    price_snapshot,
        "thesis":   macro_thesis,
        "headlines": headlines,
        "phase":    "A",
        "note":     "PARALLEL-RUN — Make scenario 5656446 still primary. This is verification stream.",
    }


def send_brief(brief: dict[str, Any], target: str | None = None) -> bool:
    """Push brief via existing line_client.py with a Flex bubble.

    target: optional LINE userId/groupId override (default LINE_BRIEF_TARGET or LINE_NEWS_TARGET)
    Returns True if pushed; False on degradation.
    """
    target = target or os.getenv("LINE_BRIEF_TARGET") or os.getenv("LINE_NEWS_TARGET")
    if not target:
        log.error("no LINE target configured (LINE_BRIEF_TARGET or LINE_NEWS_TARGET)")
        return False

    try:
        from src.line_client import push_flex      # type: ignore
    except Exception as e:
        log.error("line_client import failed: %s", e)
        return False

    headlines = brief.get("headlines", [])
    price = brief.get("price") or {}

    # Build a simple Flex bubble — header + price + headline list + thesis
    flex = {
        "type": "bubble",
        "size": "mega",
        "header": {
            "type": "box", "layout": "vertical", "backgroundColor": "#b8870f",
            "paddingAll": "14px",
            "contents": [
                {"type": "text", "text": "🥇 XAU Daily Brief", "weight": "bold", "color": "#fff", "size": "lg"},
                {"type": "text", "text": brief["title"], "color": "#fff", "size": "xs"},
            ],
        },
        "body": {
            "type": "box", "layout": "vertical", "spacing": "md",
            "contents": _body_contents(brief, headlines, price),
        },
        "footer": {
            "type": "box", "layout": "vertical",
            "contents": [{"type": "text", "text": brief.get("note", ""), "size": "xxs", "color": "#999", "wrap": True}],
        },
    }

    try:
        push_flex(target, altText=f"XAU {brief['slot']} brief", flex=flex)
        log.info("brief pushed: slot=%s headlines=%d", brief["slot"], len(headlines))
        return True
    except Exception as e:
        log.error("push_flex failed: %s", e)
        return False


def _body_contents(brief: dict, headlines: list, price: dict) -> list:
    out: list = []
    if price and price.get("price"):
        out.append({
            "type": "box", "layout": "baseline", "spacing": "sm",
            "contents": [
                {"type": "text", "text": "Spot", "size": "sm", "color": "#888"},
                {"type": "text", "text": f"${price['price']:.2f}", "weight": "bold", "size": "lg"},
            ],
        })
    if brief.get("thesis"):
        out.append({"type": "text", "text": brief["thesis"], "size": "sm", "color": "#444", "wrap": True})
    if headlines:
        out.append({"type": "separator", "margin": "md"})
        out.append({"type": "text", "text": "Top headlines", "size": "xs", "color": "#888", "weight": "bold"})
        for h in headlines[:5]:
            out.append({
                "type": "text",
                "text": f"• {(h.get('title') or '')[:120]}",
                "size": "xs", "color": "#333", "wrap": True, "margin": "sm",
            })
    if not out:
        out.append({"type": "text", "text": "(no data this cycle — pipeline stub)", "size": "sm", "color": "#999"})
    return out


def main() -> int:
    """Entry point for GHA workflow + manual run."""
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    brief = compose_brief()
    ok = send_brief(brief)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
