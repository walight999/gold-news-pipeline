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

from .calendar import CalEvent
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


def _impact_pill_small(score: float) -> dict[str, Any]:
    """Fixed-width pill displaying HIGH/MEDIUM/LOW.

    Width is pinned (sized for the longest label MEDIUM) so all three pills
    line up identically across rows — visual symmetry over text efficiency.
    """
    label, bg, fg = score_to_impact(score)
    return {
        "type": "box", "layout": "vertical",
        "backgroundColor": bg, "cornerRadius": "4px",
        "width": "62px",
        "paddingTop": "2px", "paddingBottom": "2px",
        "flex": 0,
        "contents": [{"type": "text", "text": label, "size": "xxs",
                       "color": fg, "weight": "bold", "align": "center"}],
    }


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
    contents: list[dict[str, Any]] = [
        {"type": "text", "text": title, "color": "#FFFFFF",
         "weight": "bold", "size": "md", "flex": 4},
    ]
    if sub_label:
        contents.append({
            "type": "text", "text": sub_label, "color": "#FFFFFF",
            "size": "sm", "align": "end", "flex": 3,
        })
    return {
        "type": "box", "layout": "horizontal",
        "backgroundColor": color, "paddingAll": "12px",
        "contents": contents,
    }


def _chip(text: str, bg: str, fg: str) -> dict[str, Any]:
    """Tight pill that hugs its text — `flex: 0` so it doesn't stretch in
    horizontal parents, and the inner text is center-aligned."""
    return {
        "type": "box", "layout": "vertical",
        "backgroundColor": bg, "cornerRadius": "10px",
        "paddingStart": "10px", "paddingEnd": "10px",
        "paddingTop": "2px", "paddingBottom": "2px",
        "flex": 0,
        "contents": [{"type": "text", "text": text, "size": "xs",
                       "color": fg, "weight": "bold", "align": "center"}],
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

def _source_link(src_name: str, age: str, url: str | None) -> dict[str, Any]:
    """Footer meta row: source link bottom-LEFT, time bottom-RIGHT (split layout).

    Source name itself acts as the article link — no separate Read button.
    """
    src_text: dict[str, Any] = {
        "type": "text", "text": f"📡 {src_name}" + (" ↗" if url else ""),
        "size": "xxs", "weight": "bold", "wrap": False, "flex": 1, "align": "start",
    }
    if url:
        src_text["color"] = "#2563EB"
        src_text["decoration"] = "underline"
        src_text["action"] = {"type": "uri", "label": "open", "uri": url}
    else:
        src_text["color"] = "#6B7280"

    contents: list[dict[str, Any]] = [src_text]
    if age:
        contents.append({
            "type": "text", "text": f"🕐 {age}",
            "size": "xxs", "color": "#6B7280", "flex": 0, "align": "end",
        })
    return {"type": "box", "layout": "horizontal",
            "alignItems": "center", "contents": contents}


def _event_bubble(label: str, color: str, ev: Event, score: float, kw_cfg: dict[str, Any]) -> dict[str, Any]:
    title = _trim(ev.representative_title, 200)
    summary = _trim(ev.representative_summary, SUMMARY_LIMIT)
    src_name = _source_label(ev.source_list)
    age = _ago_label(_earliest_ts(ev))
    article_url = _pick_article_url(ev.items)
    _, topic_fg = _topic_chip_color(ev.topic_bucket)
    dir_bg, dir_fg = _direction_chip_color(ev.direction_label)
    topic_text = ev.topic_bucket.replace("_", " ").title()

    body_contents: list[dict[str, Any]] = [
        # Topic + direction row — Title Case xs bold, LEFT-aligned, chip tight
        {"type": "box", "layout": "horizontal", "spacing": "sm",
         "alignItems": "center", "contents": [
             {"type": "text", "text": topic_text,
              "size": "xs", "weight": "bold", "color": topic_fg, "flex": 0},
             _chip(ev.direction_label, dir_bg, dir_fg),
        ]},
        # Title — only one size larger than the summary, bold
        {"type": "text", "text": title, "weight": "bold", "size": "md",
         "wrap": True, "color": "#111827", "margin": "md"},
    ]
    if summary:
        body_contents.append({"type": "text", "text": summary, "size": "sm",
                              "wrap": True, "color": "#1F2937", "margin": "md"})
    # Footer: time + source link (small, last) — separator gives visual gap
    body_contents.append({"type": "separator", "margin": "lg"})
    body_contents.append(_source_link(src_name, age, article_url))

    impact_label, _, _ = score_to_impact(score)
    return {
        "type": "bubble", "size": "kilo",
        "header": _header(label, impact_label, color),
        "body": {"type": "box", "layout": "vertical", "spacing": "sm",
                 "contents": body_contents, "paddingAll": "16px"},
    }


def breaking_bubble(ev: Event, score: float, kw_cfg: dict[str, Any]) -> dict[str, Any]:
    return _event_bubble("⚡ Breaking", COLOR["breaking"], ev, score, kw_cfg)


def alert_bubble(ev: Event, score: float, kw_cfg: dict[str, Any]) -> dict[str, Any]:
    return _event_bubble("🔔 Alert", COLOR["alert"], ev, score, kw_cfg)


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

    return {
        "type": "box", "layout": "vertical", "spacing": "xs", "margin": "md",
        "contents": [
            # Title row: fixed-width impact pill + bold title
            {"type": "box", "layout": "horizontal", "spacing": "sm",
             "alignItems": "center", "contents": [
                 _impact_pill_small(score),
                 {"type": "text", "text": title, "size": "sm",
                  "wrap": True, "color": "#111827", "flex": 1, "weight": "bold"},
            ]},
            # Meta row: time + source-as-link (source name itself opens URL)
            _source_link(src_name, age, url),
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
        "header": _header("📰 Latest News Update",
                          f"{slot} ICT · {total_events} event(s)",
                          COLOR["digest"]),
        "body": {"type": "box", "layout": "vertical", "spacing": "sm",
                 "contents": sections, "paddingAll": "16px"},
    }


# ---------- health ----------

def eod_recap_bubble(stats: dict[str, Any], date_label: str) -> dict[str, Any]:
    """End-of-day recap. `stats` shape:
       {breaking_n, alert_n, digest_events_n, calendar_pre_n,
        calendar_post_n, top_topics: list[(topic, count, max_score)]}
    """
    rows: list[dict[str, Any]] = [
        {"type": "box", "layout": "horizontal", "contents": [
            {"type": "text", "text": f"⚡ {stats.get('breaking_n', 0)} Breaking",
             "size": "sm", "weight": "bold", "color": "#DC2626", "flex": 1},
            {"type": "text", "text": f"🔔 {stats.get('alert_n', 0)} Alert",
             "size": "sm", "weight": "bold", "color": "#D97706", "flex": 1, "align": "end"},
        ]},
        {"type": "box", "layout": "horizontal", "margin": "sm", "contents": [
            {"type": "text", "text": f"📰 {stats.get('digest_events_n', 0)} Digest events",
             "size": "sm", "color": "#374151", "flex": 1},
        ]},
        {"type": "separator", "margin": "lg"},
        {"type": "text", "text": "CALENDAR", "size": "xs", "color": "#9CA3AF",
         "weight": "bold", "margin": "md"},
        {"type": "box", "layout": "horizontal", "contents": [
            {"type": "text", "text": f"⏰ {stats.get('calendar_pre_n', 0)} Pre-Release",
             "size": "sm", "color": "#374151", "flex": 1},
            {"type": "text", "text": f"📊 {stats.get('calendar_post_n', 0)} Post-Release",
             "size": "sm", "color": "#374151", "flex": 1, "align": "end"},
        ]},
    ]
    top_topics = stats.get("top_topics") or []
    if top_topics:
        rows.append({"type": "separator", "margin": "lg"})
        rows.append({"type": "text", "text": "TOP TOPICS", "size": "xs",
                     "color": "#9CA3AF", "weight": "bold", "margin": "md"})
        for topic, count, max_score in top_topics[:6]:
            bg, fg = _topic_chip_color(topic)
            rows.append({
                "type": "box", "layout": "horizontal", "spacing": "sm",
                "alignItems": "center", "margin": "sm",
                "contents": [
                    _chip(topic, bg, fg),
                    {"type": "text", "text": f"{count} events",
                     "size": "xs", "color": "#6B7280", "flex": 1, "margin": "sm"},
                    {"type": "text", "text": f"max {max_score:.1f}",
                     "size": "xs", "color": "#9CA3AF", "flex": 0, "align": "end"},
                ],
            })
    return {
        "type": "bubble", "size": "kilo",
        "header": _header("🌙 End of Day", date_label, "#1F2937"),
        "body": {"type": "box", "layout": "vertical", "spacing": "sm",
                 "paddingAll": "16px", "contents": rows},
    }


def health_recovered_bubble(recoveries: list[tuple[str, str]]) -> dict[str, Any]:
    """Compact green bubble: one line per recovered feed."""
    lines: list[dict[str, Any]] = []
    for sid, wtype in recoveries[:15]:
        src = SOURCE_NAMES.get(sid, sid)
        msg = WARNING_MESSAGES.get(wtype, wtype)
        lines.append({
            "type": "text", "text": f"📡 {src} · {msg}",
            "size": "xs", "color": "#374151", "wrap": True, "margin": "xs",
        })
    return {
        "type": "bubble", "size": "kilo",
        "header": _header("✅ Recovered", str(len(recoveries)), "#059669"),
        "body": {"type": "box", "layout": "vertical", "spacing": "xs",
                 "paddingAll": "12px", "contents": lines},
    }


def health_bubble(warnings: list[tuple[str, str]]) -> dict[str, Any]:
    """Compact gray bubble: one line per warning."""
    lines: list[dict[str, Any]] = []
    for sid, wtype in warnings[:15]:
        src = SOURCE_NAMES.get(sid, sid)
        msg = WARNING_MESSAGES.get(wtype, wtype)
        lines.append({
            "type": "text", "text": f"📡 {src} · {msg}",
            "size": "xs", "color": "#374151", "wrap": True, "margin": "xs",
        })
    return {
        "type": "bubble", "size": "kilo",
        "header": _header("⚠️ Health Check", str(len(warnings)), COLOR["health"]),
        "body": {"type": "box", "layout": "vertical", "spacing": "xs",
                 "paddingAll": "12px", "contents": lines},
    }


# ---------- calendar ----------

def _impact_color(impact: str) -> tuple[str, str]:
    """Background / foreground colors for impact pill in calendar."""
    return {
        "High":    ("#DC2626", "#FFFFFF"),
        "Medium":  ("#D97706", "#FFFFFF"),
        "Low":     ("#6B7280", "#FFFFFF"),
        "Holiday": ("#7C3AED", "#FFFFFF"),
    }.get(impact, ("#6B7280", "#FFFFFF"))


def _impact_pill_calendar(impact: str) -> dict[str, Any]:
    """Fixed-width pill displaying the impact level (sized for 'High')."""
    bg, fg = _impact_color(impact)
    return {
        "type": "box", "layout": "vertical",
        "backgroundColor": bg, "cornerRadius": "4px",
        "width": "52px",
        "paddingTop": "2px", "paddingBottom": "2px",
        "flex": 0,
        "contents": [{"type": "text", "text": impact, "size": "xxs",
                       "color": fg, "weight": "bold", "align": "center"}],
    }


def calendar_day_bubble(
    events: list[CalEvent],
    date_label: str,
    xau_snapshot: tuple[float, float] | None = None,   # (last, day_pct)
    dxy_snapshot: tuple[float, float] | None = None,
) -> dict[str, Any] | None:
    """One long bubble listing today's events chronologically.

    Optional `xau_snapshot` / `dxy_snapshot` render a market-pulse band
    at the top of the body so the user sees gold / dollar levels alongside
    the day's release schedule.
    """
    if not events:
        return None
    body_contents: list[dict[str, Any]] = []

    # Price snapshot strip
    if xau_snapshot or dxy_snapshot:
        cells = []
        for label, snap in (("XAU", xau_snapshot), ("DXY", dxy_snapshot)):
            if not snap:
                continue
            last, pct = snap
            color = "#059669" if pct > 0 else "#DC2626" if pct < 0 else "#374151"
            sign = "+" if pct > 0 else ""
            cells.append({
                "type": "box", "layout": "vertical", "flex": 1,
                "contents": [
                    {"type": "text", "text": label, "size": "xxs", "color": "#9CA3AF"},
                    {"type": "text", "text": f"${last:,.2f}", "size": "sm",
                     "weight": "bold", "color": "#111827"},
                    {"type": "text", "text": f"{sign}{pct:.2f}%", "size": "xxs",
                     "color": color},
                ],
            })
        if cells:
            body_contents.append({
                "type": "box", "layout": "horizontal", "spacing": "md",
                "contents": cells,
            })
            body_contents.append({"type": "separator", "margin": "md"})

    for ev in events:
        body_contents.append({
            "type": "box", "layout": "horizontal", "spacing": "md",
            "alignItems": "center", "margin": "md",
            "contents": [
                {"type": "text", "text": ev.hhmm_ict, "size": "sm",
                 "weight": "bold", "color": "#111827", "flex": 0},
                _impact_pill_calendar(ev.impact),
                {"type": "text", "text": f"  {ev.country}  ", "size": "xs",
                 "weight": "bold", "color": "#374151", "flex": 0},
                {"type": "text", "text": ev.title, "size": "sm",
                 "wrap": True, "color": "#111827", "flex": 1},
            ],
        })
    return {
        "type": "bubble", "size": "giga",
        "header": _header("📅 Economic Calendar", date_label, COLOR["digest"]),
        "body": {"type": "box", "layout": "vertical", "spacing": "sm",
                 "contents": body_contents, "paddingAll": "16px"},
    }


def _verdict_word(verdict: str | None) -> str:
    """Reduce 'Bearish gold — strong jobs...' to just 'BEARISH'."""
    if not verdict:
        return "NEUTRAL"
    v = verdict.upper()
    if "BULLISH" in v:
        return "BULLISH"
    if "BEARISH" in v:
        return "BEARISH"
    return "NEUTRAL"


def post_release_bubble(
    event: CalEvent,
    impact: dict[str, str],
    actual_text: str | None = None,
    surprise: str | None = None,
    verdict: str | None = None,
    xau_return_pct: float | None = None,
) -> dict[str, Any]:
    """Released-news bubble — title-led, no time field, single-word verdict.

    Modes:
      - Without FRED: directional ↑/↓ guide (when actual_text is None).
      - With FRED:    Actual / Forecast / Previous row + surprise emoji +
                      one-word Gold Impact (BULLISH / BEARISH / NEUTRAL).
    """
    header_color, _ = _impact_color(event.impact)
    body_contents: list[dict[str, Any]] = [
        # Title leads (newspaper-style headline)
        {"type": "text", "text": event.title, "size": "md", "weight": "bold",
         "wrap": True, "color": "#111827"},
        # Impact pill + country row
        {"type": "box", "layout": "horizontal", "spacing": "sm",
         "alignItems": "center", "margin": "sm",
         "contents": [
             _impact_pill_calendar(event.impact),
             {"type": "text", "text": event.country,
              "size": "xs", "weight": "bold", "color": "#374151", "flex": 0},
        ]},
    ]

    if actual_text:
        # 3-col data strip
        body_contents.append({
            "type": "box", "layout": "horizontal", "margin": "lg",
            "contents": [
                {"type": "text", "text": "Actual",   "size": "xxs", "color": "#9CA3AF", "flex": 1},
                {"type": "text", "text": "Forecast", "size": "xxs", "color": "#9CA3AF", "flex": 1, "align": "center"},
                {"type": "text", "text": "Previous", "size": "xxs", "color": "#9CA3AF", "flex": 1, "align": "end"},
            ],
        })
        body_contents.append({
            "type": "box", "layout": "horizontal",
            "contents": [
                {"type": "text", "text": actual_text, "size": "md", "weight": "bold",
                 "color": "#111827", "flex": 1},
                {"type": "text", "text": event.forecast or "-", "size": "sm",
                 "color": "#374151", "flex": 1, "align": "center"},
                {"type": "text", "text": event.previous or "-", "size": "sm",
                 "color": "#374151", "flex": 1, "align": "end"},
            ],
        })
        body_contents.append({"type": "separator", "margin": "lg"})
        if surprise:
            emoji = {"beat": "🟢", "miss": "🔴", "in-line": "⚪"}.get(surprise, "")
            body_contents.append({
                "type": "text", "text": f"{emoji} {surprise.upper()}",
                "size": "sm", "weight": "bold",
                "color": "#374151", "margin": "md",
            })
        # Single-word verdict — no trailing rationale punchline
        body_contents.append({
            "type": "text", "text": f"Gold Impact: {_verdict_word(verdict)}",
            "size": "sm", "weight": "bold",
            "color": "#111827", "margin": "xs",
        })
        # Live XAU reaction (price-feed Phase 3 — when available)
        if xau_return_pct is not None:
            color = "#059669" if xau_return_pct > 0 else "#DC2626" if xau_return_pct < 0 else "#6B7280"
            sign = "+" if xau_return_pct > 0 else ""
            body_contents.append({
                "type": "text",
                "text": f"XAU reacted {sign}{xau_return_pct:.2f}% in the next 5 min",
                "size": "xs", "color": color, "margin": "sm",
            })
    else:
        # Directional-only path
        body_contents.append({
            "type": "text",
            "text": f"Forecast: {event.forecast or '-'}  ·  Previous: {event.previous or '-'}",
            "size": "xs", "color": "#374151", "margin": "lg",
        })
        body_contents.append({"type": "separator", "margin": "lg"})
        body_contents.extend([
            {"type": "text", "text": "Gold Impact (directional)",
             "size": "xs", "color": "#9CA3AF", "weight": "bold", "margin": "md"},
            {"type": "text", "text": "↑ Higher → " + impact["higher_is"],
             "size": "xs", "color": "#374151", "margin": "xs"},
            {"type": "text", "text": "↓ Lower  → " + impact["lower_is"],
             "size": "xs", "color": "#374151"},
        ])

    return {
        "type": "bubble", "size": "kilo",
        "header": _header("📊 Released News", "", header_color),
        "body": {"type": "box", "layout": "vertical", "spacing": "sm",
                 "paddingAll": "16px", "contents": body_contents},
    }


def weekly_preview_bubble(
    events: list[CalEvent],
    effects: dict[str, dict[str, str]],
    week_label: str,
) -> dict[str, Any] | None:
    """One long bubble — preview of next week's events, sectioned by day.

    `effects` maps event_id → forecast_vs_previous_effect dict, used to
    render the per-event 🟢 / 🔴 / 🟡 indicator.
    """
    if not events:
        return None
    from collections import OrderedDict
    by_day: OrderedDict[str, list[CalEvent]] = OrderedDict()
    for ev in sorted(events, key=lambda e: e.dt_utc):
        key = ev.dt_ict.strftime("%a %d %b")
        by_day.setdefault(key, []).append(ev)

    sections: list[dict[str, Any]] = []
    for i, (day_label, day_events) in enumerate(by_day.items()):
        if i > 0:
            sections.append({"type": "separator", "margin": "lg"})
        sections.append({
            "type": "box", "layout": "horizontal", "margin": "lg",
            "alignItems": "center",
            "contents": [
                {"type": "text", "text": day_label.upper(), "size": "sm",
                 "color": "#374151", "weight": "bold", "flex": 1},
                {"type": "text", "text": f"{len(day_events)} event(s)",
                 "size": "xs", "color": "#9CA3AF", "align": "end", "flex": 0},
            ],
        })
        for ev in day_events:
            eff = effects.get(ev.event_id) or {"emoji": "🟡"}
            sections.append({
                "type": "box", "layout": "horizontal", "spacing": "md",
                "alignItems": "center", "margin": "md",
                "contents": [
                    {"type": "text", "text": ev.hhmm_ict, "size": "sm",
                     "weight": "bold", "color": "#111827", "flex": 0},
                    _impact_pill_calendar(ev.impact),
                    {"type": "text", "text": f"  {ev.country}  ", "size": "xs",
                     "weight": "bold", "color": "#374151", "flex": 0},
                    {"type": "text", "text": ev.title, "size": "sm",
                     "wrap": True, "color": "#111827", "flex": 1},
                    {"type": "text", "text": eff["emoji"], "size": "md",
                     "flex": 0, "align": "end"},
                ],
            })

    return {
        "type": "bubble", "size": "giga",
        "header": _header("📅 Week Ahead", week_label, COLOR["digest"]),
        "body": {"type": "box", "layout": "vertical", "spacing": "sm",
                 "contents": sections, "paddingAll": "16px"},
    }


def pre_release_bubble(event: CalEvent, minutes_to_release: int,
                       impact: dict[str, str] | None = None,
                       effect: dict[str, str] | None = None) -> dict[str, Any]:
    """Pre-release bubble — title-led, 3-column Forecast/Previous/Effect.

    Effect emoji (🟢/🔴/🟡) reflects what the market is pricing in based on
    just the forecast vs previous comparison (no actual yet).
    """
    header_color, _ = _impact_color(event.impact)
    eff = effect or {"emoji": "🟡", "label": "n/a"}

    body_contents: list[dict[str, Any]] = [
        # Title (lead the bubble)
        {"type": "text", "text": event.title, "weight": "bold", "size": "md",
         "wrap": True, "color": "#111827"},
        # Impact pill + country row
        {"type": "box", "layout": "horizontal", "spacing": "sm",
         "alignItems": "center", "margin": "sm",
         "contents": [
             _impact_pill_calendar(event.impact),
             {"type": "text", "text": event.country, "size": "xs",
              "weight": "bold", "color": "#374151", "flex": 0},
        ]},
        # 3-column header
        {"type": "box", "layout": "horizontal", "margin": "lg",
         "contents": [
             {"type": "text", "text": "Forecast", "size": "xxs",
              "color": "#9CA3AF", "flex": 1},
             {"type": "text", "text": "Previous", "size": "xxs",
              "color": "#9CA3AF", "flex": 1, "align": "center"},
             {"type": "text", "text": "Effect", "size": "xxs",
              "color": "#9CA3AF", "flex": 1, "align": "end"},
        ]},
        # 3-column values
        {"type": "box", "layout": "horizontal",
         "contents": [
             {"type": "text", "text": event.forecast or "-", "size": "sm",
              "weight": "bold", "color": "#111827", "flex": 1},
             {"type": "text", "text": event.previous or "-", "size": "sm",
              "color": "#374151", "flex": 1, "align": "center"},
             {"type": "text", "text": eff["emoji"], "size": "md",
              "flex": 1, "align": "end"},
        ]},
    ]
    if impact:
        body_contents.extend([
            {"type": "separator", "margin": "lg"},
            {"type": "text", "text": "Gold Impact (directional)",
             "size": "xs", "color": "#9CA3AF", "weight": "bold", "margin": "md"},
            {"type": "text", "text": "↑ Higher → " + impact["higher_is"],
             "size": "xs", "color": "#374151", "margin": "xs"},
            {"type": "text", "text": "↓ Lower  → " + impact["lower_is"],
             "size": "xs", "color": "#374151"},
        ])
    return {
        "type": "bubble", "size": "kilo",
        "header": _header("⏰ Pre-Release", f"T-{minutes_to_release}min", header_color),
        "body": {"type": "box", "layout": "vertical", "spacing": "sm",
                 "paddingAll": "16px", "contents": body_contents},
    }


# ---------- alt text ----------

def alt_text_for_event(label: str, ev: Event, score: float) -> str:
    return _trim(f"{label} {score:.1f} {ev.representative_title}", 380)
