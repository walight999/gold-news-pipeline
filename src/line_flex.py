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

# Health warning -> human-readable English text.
WARNING_MESSAGES: dict[str, str] = {
    "http_errors_streak":          "HTTP errors (3 consecutive failures)",
    "tier0_event_day_no_success":  "Tier-0 fetch failed during event window (>15 min)",
    "tier1_no_success":            "No successful fetch in 60+ min",
    "tier2_no_item":               "No new items in 30+ min",
}


def _trim(s: str, n: int) -> str:
    s = (s or "").strip()
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def score_to_impact(score: float) -> tuple[str, str, str]:
    """Map raw score → (label, bg_color, fg_color).

    Phase 1 routing thresholds:
        HIGH   >= 4.0   (most BREAKING)
        MEDIUM 3.0-3.9  (ALERT + top DIGEST)
        LOW    < 3.0    (DIGEST)
    Raw score is still persisted to event_state + calibration_log for tuning.
    """
    if score >= 4.0:
        return ("HIGH", "#DC2626", "#FFFFFF")
    if score >= 3.0:
        return ("MEDIUM", "#D97706", "#FFFFFF")
    return ("LOW", "#6B7280", "#FFFFFF")


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

def _read_link(url: str, src_name: str, color: str = "#2563EB") -> dict[str, Any]:
    """Compact, right-aligned link text — replaces full-width button."""
    return {
        "type": "text",
        "text": f"Read at {src_name} ↗",
        "action": {"type": "uri", "label": "open", "uri": url},
        "size": "xs", "color": color, "weight": "bold",
        "decoration": "underline", "align": "end",
    }


def _inline_tags(topic: str, direction: str) -> dict[str, Any]:
    """Small gray dotted tags appended below title — minimal visual weight."""
    return {
        "type": "text",
        "text": f"{topic}  ·  {direction}",
        "size": "xs", "color": "#9CA3AF",
    }


def _event_bubble(label: str, color: str, ev: Event, score: float, kw_cfg: dict[str, Any]) -> dict[str, Any]:
    title = _trim(ev.representative_title, 180)
    summary = _trim(ev.representative_summary, SUMMARY_LIMIT)
    src_name = _source_label(ev.source_list)
    age = _ago_label(_earliest_ts(ev))
    article_url = _pick_article_url(ev.items)
    primary_source_name = SOURCE_NAMES.get(ev.source_list[0], ev.source_list[0].title()) if ev.source_list else ""

    body_contents: list[dict[str, Any]] = [
        # Meta row: time + source (small gray)
        {"type": "text",
         "text": f"🕐 {age}  ·  📡 {src_name}".strip(" · "),
         "size": "xs", "color": "#6B7280", "wrap": True},
        # Big bold title
        {"type": "text", "text": title, "weight": "bold", "size": "lg",
         "wrap": True, "color": "#111827", "margin": "md"},
        # Inline tags right below title
        _inline_tags(ev.topic_bucket, ev.direction_label),
    ]
    if summary:
        body_contents.append({"type": "text", "text": summary, "size": "sm",
                              "wrap": True, "color": "#1F2937", "margin": "md"})
    if article_url:
        body_contents.append(_read_link(article_url, primary_source_name, color))

    impact_label, _, _ = score_to_impact(score)
    return {
        "type": "bubble", "size": "kilo",
        "header": _header(label, f"{impact_label} IMPACT", color),
        "body": {"type": "box", "layout": "vertical", "spacing": "sm",
                 "contents": body_contents, "paddingAll": "16px"},
    }


def breaking_bubble(ev: Event, score: float, kw_cfg: dict[str, Any]) -> dict[str, Any]:
    return _event_bubble("⚡ BREAKING", COLOR["breaking"], ev, score, kw_cfg)


def alert_bubble(ev: Event, score: float, kw_cfg: dict[str, Any]) -> dict[str, Any]:
    return _event_bubble("🔔 ALERT", COLOR["alert"], ev, score, kw_cfg)


# ---------- digest ----------

def _digest_event_row(ev: Event, score: float, kw_cfg: dict[str, Any]) -> dict[str, Any]:
    """Compact event row for the single long-bubble digest.

    Layout:
      [HIGH] Title in bold (wraps)
             ForexLive · 12m ago                            Read ↗
    """
    title = _trim(ev.representative_title, TOPIC_TITLE_LIMIT)
    src_name = _source_label(ev.source_list, max_n=2)
    primary_src = SOURCE_NAMES.get(ev.source_list[0], ev.source_list[0].title()) if ev.source_list else "source"
    age = _ago_label(_earliest_ts(ev))
    url = _pick_article_url(ev.items)
    impact_label, impact_bg, impact_fg = score_to_impact(score)

    meta_row_contents: list[dict[str, Any]] = [
        {"type": "text",
         "text": f"📡 {src_name}  ·  🕐 {age}".strip("  · "),
         "size": "xxs", "color": "#6B7280", "flex": 1},
    ]
    if url:
        meta_row_contents.append(_read_link(url, primary_src))

    return {
        "type": "box", "layout": "vertical", "spacing": "xs", "margin": "md",
        "contents": [
            # Title row: impact pill + bold title
            {"type": "box", "layout": "horizontal", "spacing": "sm",
             "alignItems": "center", "contents": [
                 _chip(impact_label, impact_bg, impact_fg),
                 {"type": "text", "text": title, "size": "sm",
                  "wrap": True, "color": "#111827", "flex": 1, "weight": "bold"},
            ]},
            # Meta + read-link row
            {"type": "box", "layout": "horizontal",
             "alignItems": "center", "contents": meta_row_contents},
        ],
    }


def digest_carousel(
    events: list[Event],
    scores: dict[str, float],
    slot: str,
    kw_cfg: dict[str, Any],
) -> dict[str, Any] | None:
    """Single LONG bubble: header then per-topic sections (no carousel).

    Name retained for backward compat with main.py — returns a bubble dict,
    LINE renders the same regardless of bubble vs carousel at this level.
    """
    if not events:
        return None
    groups: dict[str, list[Event]] = {}
    for ev in events:
        groups.setdefault(ev.topic_bucket, []).append(ev)
    sorted_topics = sorted(
        groups.keys(),
        key=lambda t: -max(scores.get(e.event_id, 0) for e in groups[t]),
    )

    total_events = 0
    sections: list[dict[str, Any]] = []
    for i, topic in enumerate(sorted_topics):
        evs = sorted(groups[topic], key=lambda e: -scores.get(e.event_id, 0))[:5]
        total_events += len(evs)
        if i > 0:
            sections.append({"type": "separator", "margin": "lg"})
        # Topic section heading
        sections.append({
            "type": "box", "layout": "horizontal", "margin": "lg",
            "alignItems": "center",
            "contents": [
                {"type": "text", "text": topic.upper(), "size": "sm",
                 "color": "#374151", "weight": "bold", "flex": 1},
                {"type": "text", "text": f"{len(evs)} item(s)", "size": "xs",
                 "color": "#9CA3AF", "align": "end", "flex": 0},
            ],
        })
        for ev in evs:
            sections.append(_digest_event_row(ev, scores.get(ev.event_id, 0), kw_cfg))

    return {
        "type": "bubble", "size": "giga",
        "header": _header(f"📰 {slot} ICT", f"{total_events} event(s)", COLOR["digest"]),
        "body": {"type": "box", "layout": "vertical", "spacing": "sm",
                 "contents": sections, "paddingAll": "16px"},
    }


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
