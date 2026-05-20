"""LINE Flex Message builders.

Design:
- BREAKING / ALERT = single bubble (one event = one push) with URL button.
- DIGEST           = carousel; one bubble PER TOPIC. Every event row is itself
                     clickable (whole-row URI action) so each item links out.
- HEALTH           = single bubble with human-readable warning lines.

Color tokens (white text on solid header):
  BREAKING #DC2626   ALERT #D97706   DIGEST #2563EB   HEALTH #6B7280
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .dedup import Event
from .normalizer import Item

COLOR = {
    "breaking": "#DC2626",
    "alert":    "#D97706",
    "digest":   "#2563EB",
    "health":   "#6B7280",
}

CARRIER_MAX_BUBBLES = 5
TOPIC_TITLE_LIMIT   = 80
SUMMARY_LIMIT       = 200

# Human-readable source names. Falls back to id.title() if not listed.
SOURCE_NAMES: dict[str, str] = {
    "fed":         "Federal Reserve",
    "bls":         "BLS",
    "ecb":         "ECB",
    "treasury":    "US Treasury",
    "bbc_world":   "BBC",
    "aljazeera":   "Al Jazeera",
    "cnbc":        "CNBC",
    "marketwatch": "MarketWatch",
    "forexlive":   "ForexLive",
    "fxstreet":    "FXStreet",
    "kitco":       "Kitco",
}

# Health warning -> Thai-friendly text.
WARNING_MESSAGES: dict[str, str] = {
    "http_errors_streak":          "HTTP errors 3+ ครั้งติด",
    "tier0_event_day_no_success":  "Tier-0 fetch fail ช่วงข่าว (>15 นาที)",
    "tier1_no_success":            "ไม่มี success fetch ใน 60+ นาที",
    "tier2_no_item":               "ไม่มี item ใหม่ใน 30+ นาที",
}


def _trim(s: str, n: int) -> str:
    s = (s or "").strip()
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def _source_label(source_ids: list[str], max_n: int = 3) -> str:
    names = [SOURCE_NAMES.get(s, s.replace("_", " ").title()) for s in source_ids[:max_n]]
    extra = len(source_ids) - max_n
    out = ", ".join(names)
    if extra > 0:
        out += f" +{extra}"
    return out


def _ago_label(dt: datetime | None) -> str:
    if dt is None:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    seconds = (datetime.now(timezone.utc) - dt).total_seconds()
    if seconds < 0:
        return "now"
    if seconds < 90:
        return f"{int(seconds)}s ago"
    if seconds < 3600:
        return f"{int(seconds // 60)}m ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h ago"
    return f"{int(seconds // 86400)}d ago"


def _map_name(text: str, name_map: dict[str, str]) -> str:
    out = text
    for en, th in (name_map or {}).items():
        idx = out.lower().find(en.lower())
        if idx >= 0:
            out = out[: idx + len(en)] + f" ({th})" + out[idx + len(en):]
            break
    return out


def _pick_article_url(items: list[Item]) -> str:
    for it in items:
        if it.url:
            return it.url[:1000]
    return ""


def _earliest_ts(ev: Event) -> datetime | None:
    pubs = [i.published_ts for i in ev.items if i.published_ts is not None]
    if pubs:
        return min(pubs)
    return ev.first_seen_ts


# ---------- visual primitives ----------

def _header(title: str, sub_label: str, color: str) -> dict[str, Any]:
    return {
        "type": "box", "layout": "horizontal",
        "backgroundColor": color, "paddingAll": "12px",
        "contents": [
            {"type": "text", "text": title, "color": "#FFFFFF", "weight": "bold", "size": "md", "flex": 4},
            {"type": "text", "text": sub_label, "color": "#FFFFFF", "size": "sm", "align": "end", "flex": 3},
        ],
    }


def _chip(text: str, bg: str, fg: str) -> dict[str, Any]:
    return {
        "type": "box", "layout": "vertical",
        "backgroundColor": bg, "cornerRadius": "12px",
        "paddingStart": "10px", "paddingEnd": "10px",
        "paddingTop": "3px", "paddingBottom": "3px",
        "contents": [{"type": "text", "text": text, "size": "xs", "color": fg, "weight": "bold"}],
    }


def _topic_chip_color(topic: str) -> tuple[str, str]:
    return {
        "inflation":   ("#FECACA", "#7F1D1D"),
        "jobs":        ("#FDE68A", "#78350F"),
        "rate_policy": ("#BFDBFE", "#1E3A8A"),
        "geopolitics": ("#FBCFE8", "#831843"),
        "usd_yields":  ("#C7D2FE", "#312E81"),
        "gold_flow":   ("#FEF08A", "#713F12"),
    }.get(topic, ("#E5E7EB", "#111827"))


def _direction_chip_color(direction: str) -> tuple[str, str]:
    return {
        "hawkish":  ("#F87171", "#FFFFFF"),
        "dovish":   ("#34D399", "#064E3B"),
        "risk_off": ("#FB923C", "#FFFFFF"),
        "risk_on":  ("#38BDF8", "#0C4A6E"),
        "neutral":  ("#9CA3AF", "#FFFFFF"),
    }.get(direction, ("#9CA3AF", "#FFFFFF"))


# ---------- breaking / alert ----------

def _event_bubble(label: str, color: str, ev: Event, score: float, kw_cfg: dict[str, Any]) -> dict[str, Any]:
    nm = kw_cfg.get("name_map", {}) or {}
    title = _trim(_map_name(ev.representative_title, nm), 160)
    summary = _trim(ev.representative_summary, SUMMARY_LIMIT)
    src_name = _source_label(ev.source_list)
    age = _ago_label(_earliest_ts(ev))
    topic_bg, topic_fg = _topic_chip_color(ev.topic_bucket)
    dir_bg, dir_fg = _direction_chip_color(ev.direction_label)
    article_url = _pick_article_url(ev.items)
    primary_source_name = SOURCE_NAMES.get(ev.source_list[0], ev.source_list[0].title()) if ev.source_list else ""

    body_contents: list[dict[str, Any]] = [
        # Time + source meta row (top, small)
        {"type": "box", "layout": "horizontal", "contents": [
            {"type": "text", "text": f"🕐 {age}" if age else "", "size": "xs", "color": "#6B7280", "flex": 1},
            {"type": "text", "text": f"📡 {src_name}", "size": "xs", "color": "#6B7280", "flex": 2, "align": "end", "wrap": True},
        ]},
        # Title - big and bold
        {"type": "text", "text": title, "weight": "bold", "size": "lg", "wrap": True, "color": "#111827", "margin": "md"},
    ]
    if summary:
        body_contents.append({"type": "text", "text": summary, "size": "sm", "wrap": True, "color": "#1F2937", "margin": "md"})
    # chips: topic + direction (drop entity - low info)
    body_contents.append({
        "type": "box", "layout": "horizontal", "spacing": "sm", "margin": "lg",
        "contents": [
            _chip(ev.topic_bucket, topic_bg, topic_fg),
            _chip(ev.direction_label, dir_bg, dir_fg),
        ],
    })

    bubble: dict[str, Any] = {
        "type": "bubble", "size": "kilo",
        "header": _header(label, f"score {score:.1f}", color),
        "body": {"type": "box", "layout": "vertical", "spacing": "md", "contents": body_contents, "paddingAll": "16px"},
    }
    if article_url:
        bubble["footer"] = {
            "type": "box", "layout": "vertical", "paddingAll": "10px",
            "contents": [{
                "type": "button", "style": "primary", "color": color, "height": "sm",
                "action": {"type": "uri",
                           "label": f"Read at {primary_source_name} →"[:40],
                           "uri": article_url},
            }],
        }
    return bubble


def breaking_bubble(ev: Event, score: float, kw_cfg: dict[str, Any]) -> dict[str, Any]:
    return _event_bubble("⚡ BREAKING", COLOR["breaking"], ev, score, kw_cfg)


def alert_bubble(ev: Event, score: float, kw_cfg: dict[str, Any]) -> dict[str, Any]:
    return _event_bubble("🔔 ALERT", COLOR["alert"], ev, score, kw_cfg)


# ---------- digest ----------

def _digest_event_row(ev: Event, score: float, kw_cfg: dict[str, Any]) -> dict[str, Any]:
    nm = kw_cfg.get("name_map", {}) or {}
    title = _trim(_map_name(ev.representative_title, nm), TOPIC_TITLE_LIMIT)
    src_name = _source_label(ev.source_list, max_n=2)
    age = _ago_label(_earliest_ts(ev))
    url = _pick_article_url(ev.items)

    row: dict[str, Any] = {
        "type": "box", "layout": "vertical", "spacing": "xs",
        "paddingAll": "8px", "cornerRadius": "6px",
        "backgroundColor": "#F9FAFB",
        "contents": [
            # title row with score + tap-arrow
            {"type": "box", "layout": "horizontal", "contents": [
                {"type": "text", "text": f"{score:.1f}", "size": "sm", "weight": "bold", "color": "#111827", "flex": 0},
                {"type": "text", "text": title, "size": "sm", "wrap": True, "color": "#111827", "margin": "md", "flex": 1, "weight": "bold"},
                {"type": "text", "text": "›" if url else "", "size": "md", "color": "#9CA3AF", "align": "end", "flex": 0},
            ]},
            {"type": "text", "text": f"📡 {src_name}  •  🕐 {age}".strip("  • "), "size": "xxs", "color": "#6B7280"},
        ],
    }
    if url:
        row["action"] = {"type": "uri", "label": "open", "uri": url}
    return row


def digest_carousel(
    events: list[Event],
    scores: dict[str, float],
    slot: str,
    kw_cfg: dict[str, Any],
) -> dict[str, Any] | None:
    """Group events by topic_bucket, one bubble per topic. Per-row taps open URL."""
    if not events:
        return None
    groups: dict[str, list[Event]] = {}
    for ev in events:
        groups.setdefault(ev.topic_bucket, []).append(ev)
    sorted_topics = sorted(
        groups.keys(),
        key=lambda t: -max(scores.get(e.event_id, 0) for e in groups[t]),
    )[:CARRIER_MAX_BUBBLES]

    bubbles: list[dict[str, Any]] = []
    for topic in sorted_topics:
        evs = sorted(groups[topic], key=lambda e: -scores.get(e.event_id, 0))[:5]
        topic_bg, topic_fg = _topic_chip_color(topic)
        body_rows: list[dict[str, Any]] = [
            {"type": "box", "layout": "horizontal", "alignItems": "center", "contents": [
                _chip(topic, topic_bg, topic_fg),
                {"type": "text", "text": f"{len(evs)} item(s)", "size": "xs", "color": "#6B7280",
                 "align": "end", "gravity": "center"},
            ]},
            {"type": "separator", "margin": "md"},
        ]
        for ev in evs:
            body_rows.append(_digest_event_row(ev, scores.get(ev.event_id, 0), kw_cfg))
        bubbles.append({
            "type": "bubble", "size": "kilo",
            "header": _header(f"📰 {slot} ICT", f"{len(evs)} in {topic}", COLOR["digest"]),
            "body": {"type": "box", "layout": "vertical", "contents": body_rows, "paddingAll": "14px", "spacing": "sm"},
        })
    return {"type": "carousel", "contents": bubbles}


# ---------- health ----------

def health_bubble(warnings: list[tuple[str, str]]) -> dict[str, Any]:
    lines: list[dict[str, Any]] = []
    for sid, wtype in warnings[:15]:
        src = SOURCE_NAMES.get(sid, sid)
        msg = WARNING_MESSAGES.get(wtype, wtype)
        lines.append({
            "type": "box", "layout": "vertical", "spacing": "xs", "margin": "sm",
            "contents": [
                {"type": "text", "text": f"📡 {src}", "size": "sm", "weight": "bold", "color": "#111827"},
                {"type": "text", "text": msg, "size": "xs", "color": "#4B5563", "wrap": True},
            ],
        })
    return {
        "type": "bubble", "size": "kilo",
        "header": _header("⚠️ HEALTH", f"{len(warnings)} warning(s)", COLOR["health"]),
        "body": {"type": "box", "layout": "vertical", "spacing": "sm", "paddingAll": "14px", "contents": lines},
    }


# ---------- alt text ----------

def alt_text_for_event(label: str, ev: Event, score: float) -> str:
    return _trim(f"{label} {score:.1f} {ev.representative_title}", 380)
