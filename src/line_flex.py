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
from .news_alert import MarketAlert
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
    "fed":                  "Federal Reserve",
    "bls":                  "BLS",
    "ecb":                  "ECB",
    "treasury":             "US Treasury",
    "bbc_world":            "BBC",
    "aljazeera":            "Al Jazeera",
    "cnbc":                 "CNBC",
    "marketwatch":          "MarketWatch",
    "forexlive":            "ForexLive",
    "fxstreet":             "FXStreet",
    "kitco":                "Kitco",
    "investing_cb":         "Investing CB",
    "investing_commodities":"Investing Commodities",
    "investing_general":    "Investing.com",
    "benzinga":             "Benzinga",
    "_pipeline_heartbeat":  "Pipeline",
    "_ff_scraper":          "FF HTML Scraper",
    "_classifier_health":   "Classifier",
    "_workflow_failures":   "GitHub Actions",
    "_line_push":           "LINE API",
    "yahoo_finance":        "Yahoo Finance",
}

# Health warning -> human-readable English text.
WARNING_MESSAGES: dict[str, str] = {
    "http_errors_streak":          "HTTP errors (3 consecutive failures)",
    "tier0_event_day_no_success":  "Tier-0 fetch failed during event window (>15 min)",
    "tier1_no_success":            "No successful fetch in 60+ min",
    "tier2_no_item":               "No new items in 30+ min",
    "watchdog_silence":            "Pipeline silent — cron may have stopped firing",
    "watchdog_no_items":           "All sources returned 0 items for hours — scraper/network down?",
    "ff_scraper_dead":             "FF HTML scrape returned 0 events 3× in a row — Cloudflare/HTML changed?",
    "classifier_degraded":         "Classifier fallback rate >30% — Claude key invalid or API down?",
    "workflow_failure":            "GitHub Actions workflow failed in last 24h — check Actions tab",
    "line_push_failing":           "LINE push failing 5× in a row — token expired or channel disabled?",
    "line_quota_high":             "LINE free-tier usage above 80% this month — consider Light plan",
    # source_noisy:<source_id> is dynamic — handled by _format_warning.
}


def _format_warning(sid: str, wtype: str) -> str:
    """Human-readable label for a warning row. Handles dynamic
    `source_noisy:<source>` warnings that aren't in the static table."""
    if wtype.startswith("source_noisy:"):
        source = wtype.split(":", 1)[1]
        return f"Source '{source}' >90% reject rate — likely noise"
    return WARNING_MESSAGES.get(wtype, wtype)


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
        "type": "text", "text": f"{src_name}" + (" ↗" if url else ""),
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
            "type": "text", "text": age,
            "size": "xxs", "color": "#6B7280", "flex": 0, "align": "end",
        })
    return {"type": "box", "layout": "horizontal",
            "alignItems": "center", "contents": contents}


def _event_bubble(label: str, color: str, ev: Event, score: float, kw_cfg: dict[str, Any],
                  alert: MarketAlert | None = None,
                  title_th: str | None = None,
                  summary_th: str | None = None) -> dict[str, Any]:
    """Breaking / Alert body.

    Preferred input is a `MarketAlert` from news_alert.classify_and_rewrite(),
    which carries a structured Thai headline + bullets + impact line that
    were tailored for a mobile trading-alert card (≤90 char headline,
    ≤120 char bullets, hawkish/dovish/risk_on/risk_off tone tag).

    Legacy `title_th` + `summary_th` strings still accepted for callers
    that haven't been migrated yet. English fallback when neither is set.

    Sized `giga` (768px) to match Economic Calendar so text doesn't get
    cut off and all bubble types read at the same width.
    """
    en_title = _trim(ev.representative_title, 200)
    en_summary = _trim(ev.representative_summary, SUMMARY_LIMIT)

    if alert and alert.should_send and alert.headline_th:
        display_title = alert.headline_th
        body_lines: list[str] = list(alert.body_th or [])
        impact_line = alert.impact_th
        category_text = alert.category
        tone_label = alert.tone
    else:
        display_title = title_th.strip() if title_th else en_title
        body_lines = [summary_th.strip()] if summary_th else ([en_summary] if en_summary else [])
        impact_line = None
        category_text = ev.topic_bucket.replace("_", " ").title()
        tone_label = ev.direction_label

    src_name = _source_label(ev.source_list)
    age = _ago_label(_earliest_ts(ev))
    article_url = _pick_article_url(ev.items)
    _, topic_fg = _topic_chip_color(ev.topic_bucket)
    dir_bg, dir_fg = _direction_chip_color(tone_label)

    body_contents: list[dict[str, Any]] = [
        {"type": "box", "layout": "horizontal", "spacing": "sm",
         "alignItems": "center", "contents": [
             {"type": "text", "text": category_text,
              "size": "xs", "weight": "bold", "color": topic_fg, "flex": 0},
             _chip(tone_label, dir_bg, dir_fg),
        ]},
        {"type": "text", "text": display_title, "weight": "bold", "size": "md",
         "wrap": True, "color": "#111827", "margin": "md"},
    ]
    # Bullets — render each as its own xs gray line. Limit to 3 bullets.
    for bullet in body_lines[:3]:
        if not bullet:
            continue
        body_contents.append({
            "type": "text", "text": f"• {bullet}",
            "size": "sm", "wrap": True, "color": "#1F2937", "margin": "sm",
        })
    if impact_line:
        body_contents.append({"type": "separator", "margin": "md"})
        body_contents.append({
            "type": "text", "text": f"💡 {impact_line}",
            "size": "xs", "wrap": True, "color": "#6B7280", "margin": "sm",
            "weight": "bold",
        })
    body_contents.append({"type": "separator", "margin": "lg"})
    body_contents.append(_source_link(src_name, age, article_url))

    impact_label, _, _ = score_to_impact(score)
    return {
        "type": "bubble", "size": "giga",
        "header": _header(label, impact_label, color),
        "body": {"type": "box", "layout": "vertical", "spacing": "sm",
                 "contents": body_contents, "paddingAll": "16px"},
    }


def breaking_bubble(ev: Event, score: float, kw_cfg: dict[str, Any],
                    alert: MarketAlert | None = None,
                    title_th: str | None = None,
                    summary_th: str | None = None) -> dict[str, Any]:
    return _event_bubble("⚡ Breaking", COLOR["breaking"], ev, score, kw_cfg,
                          alert=alert, title_th=title_th, summary_th=summary_th)


def alert_bubble(ev: Event, score: float, kw_cfg: dict[str, Any],
                 alert: MarketAlert | None = None,
                 title_th: str | None = None,
                 summary_th: str | None = None) -> dict[str, Any]:
    return _event_bubble("🔔 Alert", COLOR["alert"], ev, score, kw_cfg,
                          alert=alert, title_th=title_th, summary_th=summary_th)


# ---------- digest ----------

def _digest_event_row(ev: Event, score: float, kw_cfg: dict[str, Any],
                      alert: MarketAlert | None = None,
                      title_th: str | None = None,
                      summary_th: str | None = None) -> dict[str, Any]:
    """Compact event row for the digest carousel.

    Prefers MarketAlert (headline_th + ≤3 bullets) when available. Falls
    back to legacy title_th + summary_th strings, and finally to the
    English title when no translation is set.

    Layout:
      {Thai headline — bold sm}
        • {bullet 1 — xxs gray}
        • {bullet 2 — xxs gray}
      ForexLive · 12m ago                          Read ↗
    """
    en_title = _trim(ev.representative_title, TOPIC_TITLE_LIMIT)
    if alert and alert.should_send and alert.headline_th:
        display_title = alert.headline_th
        bullets = list(alert.body_th or [])[:2]
    else:
        display_title = title_th.strip() if title_th else en_title
        bullets = [summary_th.strip()] if summary_th else []
    src_name = _source_label(ev.source_list, max_n=2)
    age = _ago_label(_earliest_ts(ev))
    url = _pick_article_url(ev.items)

    contents: list[dict[str, Any]] = [
        {"type": "text", "text": display_title, "size": "sm",
         "wrap": True, "color": "#111827", "weight": "bold"},
    ]
    for b in bullets:
        if not b:
            continue
        contents.append({
            "type": "text", "text": f"• {_trim(b, 200)}",
            "size": "xxs", "color": "#374151", "wrap": True, "margin": "sm",
        })
    contents.append(_source_link(src_name, age, url))

    return {"type": "box", "layout": "vertical", "spacing": "xs",
            "margin": "md", "contents": contents}


def _build_digest_bubble(
    chunk: list[Event],
    scores: dict[str, float],
    slot: str,
    kw_cfg: dict[str, Any],
    translations: dict[str, dict[str, str | None]] | None,
    alerts: dict[str, MarketAlert] | None,
    header_label: str,
    header_sub: str,
) -> dict[str, Any]:
    """One bubble with its own topic grouping for the events in `chunk`."""
    groups: dict[str, list[Event]] = {}
    for ev in chunk:
        groups.setdefault(ev.topic_bucket, []).append(ev)
    sorted_topics = sorted(
        groups.keys(),
        key=lambda t: -max(scores.get(e.event_id, 0) for e in groups[t]),
    )
    sections: list[dict[str, Any]] = []
    for i, topic in enumerate(sorted_topics):
        evs = sorted(groups[topic], key=lambda e: -scores.get(e.event_id, 0))
        if i > 0:
            sections.append({"type": "separator", "margin": "lg"})
        section_max_score = max(scores.get(e.event_id, 0) for e in evs)
        sections.append({
            "type": "box", "layout": "horizontal", "margin": "lg",
            "alignItems": "center",
            "contents": [
                {"type": "text", "text": topic.upper(), "size": "sm",
                 "color": "#374151", "weight": "bold", "flex": 1},
                _impact_pill_small(section_max_score),
            ],
        })
        for ev in evs:
            tr = (translations or {}).get(ev.event_id, {})
            alert = (alerts or {}).get(ev.event_id)
            sections.append(_digest_event_row(
                ev, scores.get(ev.event_id, 0), kw_cfg,
                alert=alert,
                title_th=tr.get("title_th") if tr else None,
                summary_th=tr.get("summary_th") if tr else None,
            ))
    return {
        "type": "bubble", "size": "giga",
        "header": _header(header_label, header_sub, COLOR["digest"]),
        "body": {"type": "box", "layout": "vertical", "spacing": "sm",
                 "contents": sections, "paddingAll": "16px"},
    }


# Above this many events, split the digest into a 2-bubble carousel.
DIGEST_SPLIT_THRESHOLD = 5


def digest_carousel(
    events: list[Event],
    scores: dict[str, float],
    slot: str,
    kw_cfg: dict[str, Any],
    translations: dict[str, dict[str, str | None]] | None = None,
    alerts: dict[str, MarketAlert] | None = None,
) -> dict[str, Any] | None:
    """Build the Latest News Update.

    ≤ 5 events: single giga bubble.
    > 5 events: 2-bubble carousel split by score order. Header sub-label
                shows ONLY the slot time (e.g. "05:30 ICT") — the "N/M
                events" count was noise that didn't help the reader.
    """
    if not events:
        return None
    ranked = sorted(events, key=lambda e: -scores.get(e.event_id, 0))
    n = len(ranked)

    if n <= DIGEST_SPLIT_THRESHOLD:
        return _build_digest_bubble(
            ranked, scores, slot, kw_cfg, translations, alerts,
            header_label="📰 Latest News Update",
            header_sub=f"{slot} ICT",
        )

    mid = (n + 1) // 2
    chunks = [ranked[:mid], ranked[mid:]]
    bubbles = []
    for i, chunk in enumerate(chunks, start=1):
        bubbles.append(_build_digest_bubble(
            chunk, scores, slot, kw_cfg, translations, alerts,
            header_label=f"📰 News Update {i}/{len(chunks)}",
            header_sub=f"{slot} ICT",
        ))
    return {"type": "carousel", "contents": bubbles}


# ---------- health ----------

def eod_recap_bubble(stats: dict[str, Any], date_label: str) -> dict[str, Any]:
    """End-of-day recap. Header carries the date that the recap is FOR
    (the day that just ended), not today's wall-clock date — the recap
    fires at 22:00 ICT so they are the same day, but bundling the date
    into the title removes a UI ambiguity (the previous right-aligned
    date looked like "now" when it actually meant "for yesterday").

    Top Topics section: dropped the "max 5.0" / "max 4.0" debug numbers
    (they were raw scorer outputs that didn't translate to anything the
    reader cared about) and moved the event count to the right.

    `stats` shape:
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
    # Classifier breakdown — only if the counters have any activity.
    cl_total = int(stats.get("classifier_total", 0))
    if cl_total > 0:
        cl_kept = int(stats.get("classifier_kept", 0))
        cl_rejected = int(stats.get("classifier_rejected", 0))
        cl_fallback = int(stats.get("classifier_fallback", 0))
        keep_pct = (cl_kept / cl_total * 100) if cl_total else 0
        rows.append({"type": "separator", "margin": "lg"})
        rows.append({"type": "text", "text": "CLASSIFIER", "size": "xs",
                     "color": "#9CA3AF", "weight": "bold", "margin": "md"})
        rows.append({"type": "box", "layout": "horizontal", "contents": [
            {"type": "text", "text": f"✓ {cl_kept} kept ({keep_pct:.0f}%)",
             "size": "sm", "color": "#059669", "flex": 1},
            {"type": "text", "text": f"✗ {cl_rejected} rejected",
             "size": "sm", "color": "#6B7280", "flex": 1, "align": "end"},
        ]})
        if cl_fallback > 0:
            fb_pct = cl_fallback / cl_total * 100
            color = "#DC2626" if fb_pct >= 30 else "#9CA3AF"
            rows.append({"type": "text",
                         "text": f"⚠ {cl_fallback} Claude fallback ({fb_pct:.0f}%)",
                         "size": "xs", "color": color, "margin": "xs"})
        # Monthly Claude token usage (input + output) — visibility for
        # cost tracking. claude-haiku-4-5 pricing ~$1/1M input + $5/1M
        # output (approx), so 1M tokens ≈ $1-5 ballpark.
        mt_in = int(stats.get("month_tokens_in", 0) or 0)
        mt_out = int(stats.get("month_tokens_out", 0) or 0)
        if mt_in or mt_out:
            month_lbl = stats.get("month_label", "") or "this month"
            rows.append({"type": "text",
                         "text": f"🤖 Claude {month_lbl}: {mt_in:,}↑ / {mt_out:,}↓ tokens",
                         "size": "xxs", "color": "#9CA3AF", "margin": "xs"})
    top_topics = stats.get("top_topics") or []
    if top_topics:
        rows.append({"type": "separator", "margin": "lg"})
        rows.append({"type": "text", "text": "TOP TOPICS", "size": "xs",
                     "color": "#9CA3AF", "weight": "bold", "margin": "md"})
        for topic, count, _max_score in top_topics[:6]:
            bg, fg = _topic_chip_color(topic)
            rows.append({
                "type": "box", "layout": "horizontal", "spacing": "sm",
                "alignItems": "center", "margin": "sm",
                "contents": [
                    _chip(topic, bg, fg),
                    {"type": "filler"},
                    {"type": "text", "text": f"{count} events",
                     "size": "xs", "color": "#6B7280", "flex": 0, "align": "end"},
                ],
            })
    title = f"🌙 Daily Recap for {date_label}" if date_label else "🌙 Daily Recap"
    return {
        "type": "bubble", "size": "giga",
        "header": _header(title, "", "#1F2937"),
        "body": {"type": "box", "layout": "vertical", "spacing": "sm",
                 "paddingAll": "16px", "contents": rows},
    }


def health_recovered_bubble(recoveries: list[tuple[str, str]]) -> dict[str, Any]:
    """Compact green bubble: one line per recovered feed."""
    lines: list[dict[str, Any]] = []
    for sid, wtype in recoveries[:15]:
        src = SOURCE_NAMES.get(sid, sid)
        msg = _format_warning(sid, wtype)
        lines.append({
            "type": "text", "text": f"📡 {src} · {msg}",
            "size": "xs", "color": "#374151", "wrap": True, "margin": "xs",
        })
    return {
        "type": "bubble", "size": "giga",
        "header": _header("✅ Recovered", str(len(recoveries)), "#059669"),
        "body": {"type": "box", "layout": "vertical", "spacing": "xs",
                 "paddingAll": "12px", "contents": lines},
    }


def health_bubble(warnings: list[tuple[str, str]]) -> dict[str, Any]:
    """Compact gray bubble: one line per warning."""
    lines: list[dict[str, Any]] = []
    for sid, wtype in warnings[:15]:
        src = SOURCE_NAMES.get(sid, sid)
        msg = _format_warning(sid, wtype)
        lines.append({
            "type": "text", "text": f"📡 {src} · {msg}",
            "size": "xs", "color": "#374151", "wrap": True, "margin": "xs",
        })
    return {
        "type": "bubble", "size": "giga",
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


def _direction_color(direction: str) -> tuple[str, str, str]:
    """Map direction label → (bg, fg, arrow). Single source of truth
    used by both XAU pill and the generalized currency pill."""
    direction = (direction or "neutral").lower()
    if "bull" in direction or direction == "up":
        return "#059669", "#FFFFFF", "↑"
    if "bear" in direction or direction == "down":
        return "#DC2626", "#FFFFFF", "↓"
    return "#D97706", "#FFFFFF", "≈"


def _currency_direction_pill(currency: str, direction: str,
                              flex: int = 1) -> dict[str, Any]:
    """Colored "{currency} {arrow}" pill. Used in the 3-pill row for
    pre / post-release bubbles (ECU + counter + XAU).

    flex=1 by default so 3 pills share the row width equally; pass
    flex=0 to make the pill hug its text (used in compact calendar
    rows that already have F:/P: labels alongside)."""
    bg, fg, arrow = _direction_color(direction)
    label = (currency or "?").upper()
    return {
        "type": "box", "layout": "vertical",
        "backgroundColor": bg, "cornerRadius": "4px",
        "paddingStart": "6px", "paddingEnd": "6px",
        "paddingTop": "4px", "paddingBottom": "4px",
        "flex": flex,
        "contents": [{"type": "text", "text": f"{label} {arrow}",
                       "size": "sm", "color": fg, "weight": "bold",
                       "align": "center"}],
    }


def _xau_direction_pill(label: str) -> dict[str, Any]:
    """Compact XAU-only pill kept for the calendar_day / weekly_preview
    rows where a 3-pill block per row would crowd the layout. Behavior
    matches `_currency_direction_pill('XAU', label, flex=0)`."""
    bg, fg, arrow = _direction_color(label)
    return {
        "type": "box", "layout": "vertical",
        "backgroundColor": bg, "cornerRadius": "4px",
        "paddingStart": "8px", "paddingEnd": "8px",
        "paddingTop": "2px", "paddingBottom": "2px",
        "flex": 0,
        "contents": [{"type": "text", "text": f"XAU {arrow}",
                       "size": "xxs", "color": fg, "weight": "bold",
                       "align": "center"}],
    }


def _xau_direction_pill_from_effect(effect: dict[str, Any] | None) -> dict[str, Any]:
    """Convert a `forecast_vs_previous_effect()` dict (which has the
    legacy emoji + label) into the pill tag. Single helper so callers
    can pass the same effect dict they were using before."""
    label = (effect or {}).get("label", "neutral")
    return _xau_direction_pill(label)


def _impact_pills_row(pills: list[tuple[str, str]]) -> dict[str, Any]:
    """3-pill horizontal row: (currency, direction) tuples → equal-flex
    colored pills. Used at the bottom of pre/post-release bubbles."""
    return {
        "type": "box", "layout": "horizontal", "spacing": "sm",
        "margin": "lg",
        "contents": [_currency_direction_pill(c, d, flex=1) for c, d in pills],
    }


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


def _fmt_value(v: float, prefix: str = "", decimals: int = 2) -> str:
    return f"{prefix}{v:,.{decimals}f}"


def _price_cell(label: str, snap: tuple[float, float] | None,
                value_fmt) -> dict[str, Any] | None:
    """One column of the price strip. `value_fmt(last)` returns the
    display string (e.g. "$4,542.40" or "฿35.21").

    Returns None when `snap` is None — user feedback 2026-05-23: "ถ้าจะ
    ไม่มี currency ก็ไม่มีให้หมดเลย" — empty cells with "(no data)"
    placeholders looked inconsistent next to live cells. Caller filters
    out None and renders only the cells with real data, so the strip
    width adjusts compactly instead of carrying dead air.

    Every text inside is `align: center` so the columns visually line up
    regardless of value width ($4,510.50 vs $99.32 vs ฿32.64).
    """
    if not snap:
        return None
    last, pct = snap
    color = "#059669" if pct > 0 else "#DC2626" if pct < 0 else "#374151"
    sign = "+" if pct > 0 else ""
    return {
        "type": "box", "layout": "vertical", "flex": 1,
        "contents": [
            {"type": "text", "text": label, "size": "xxs",
             "color": "#9CA3AF", "align": "center"},
            {"type": "text", "text": value_fmt(last), "size": "sm",
             "weight": "bold", "color": "#111827", "align": "center"},
            {"type": "text", "text": f"{sign}{pct:.2f}%", "size": "xxs",
             "color": color, "align": "center"},
        ],
    }


def _forecast_previous_inline(forecast: str, previous: str) -> dict[str, Any] | None:
    """Right-aligned 'F: -0.6% | Pre: 0.7%' block. Returns None when
    both values are blank (so a row doesn't show a stray bar)."""
    f = (forecast or "").strip()
    p = (previous or "").strip()
    if not f and not p:
        return None
    parts: list[str] = []
    if f:
        parts.append(f"F: {f}")
    if p:
        parts.append(f"Pre: {p}")
    return {
        "type": "text", "text": " | ".join(parts),
        "size": "xxs", "color": "#6B7280", "flex": 0, "align": "end",
        "wrap": False,
    }


def calendar_day_bubble(
    events: list[CalEvent],
    date_label: str,
    xau_snapshot: tuple[float, float] | None = None,   # (last, day_pct)
    dxy_snapshot: tuple[float, float] | None = None,
    hui_snapshot: tuple[float, float] | None = None,
    gld_snapshot: tuple[float, float] | None = None,
    thb_snapshot: tuple[float, float] | None = None,
) -> dict[str, Any] | None:
    """One long bubble listing today's events chronologically.

    Price strip: XAU | DXY | HUI | GLD | USDTHB. Any snapshot that's
    None is skipped (off-hours / API flake). Strip uses up to 5 cells
    so the bubble width carries real signal instead of empty space.

    Per-event row layout:
      [HH:MM] [Impact] [Country] [Event title]              F: X | Pre: Y
    Forecast / Previous are right-aligned gray so the eye reads time +
    name on the left and surprise context on the right. Skipped when
    both values are blank.
    """
    if not events:
        return None
    body_contents: list[dict[str, Any]] = []

    # Price snapshot strip (XAU spot in $, DXY index, HUI gold-miners,
    # GLD SPDR ETF price, USD/THB).
    # "SPDR" instead of the ticker "GLD" because the SPDR Gold Trust
    # name reads more clearly to most traders than the raw symbol.
    price_specs = (
        ("XAU", xau_snapshot, lambda v: _fmt_value(v, "$")),
        ("DXY", dxy_snapshot, lambda v: _fmt_value(v, "")),
        ("HUI", hui_snapshot, lambda v: _fmt_value(v, "")),
        ("SPDR", gld_snapshot, lambda v: _fmt_value(v, "$")),
        ("USDTHB", thb_snapshot, lambda v: _fmt_value(v, "฿")),
    )
    # Render only cells that have data — user prefers consistent
    # all-real-data formatting over fixed-width with placeholders.
    cells = [
        c for c in (_price_cell(lbl, snap, fmt) for lbl, snap, fmt in price_specs)
        if c is not None
    ]
    if cells:
        body_contents.append({
            "type": "box", "layout": "horizontal", "spacing": "md",
            "contents": cells,
        })
        body_contents.append({"type": "separator", "margin": "md"})

    from .calendar import forecast_vs_previous_effect
    for ev in events:
        # Left side (flex=1): time + impact + country + title.
        # Right side (flex=0): vertical stack — F: x / P: y on top,
        # XAU direction pill below. The pill is the dominant signal so
        # it gets the colored treatment; F/P is gray sub-text.
        left = {
            "type": "box", "layout": "horizontal", "spacing": "sm",
            "alignItems": "center", "flex": 1,
            "contents": [
                {"type": "text", "text": ev.hhmm_ict, "size": "sm",
                 "weight": "bold", "color": "#111827", "flex": 0},
                _impact_pill_calendar(ev.impact),
                {"type": "text", "text": f" {ev.country} ", "size": "xs",
                 "weight": "bold", "color": "#374151", "flex": 0},
                {"type": "text", "text": ev.title, "size": "sm",
                 "wrap": True, "color": "#111827", "flex": 1},
            ],
        }
        right_contents: list[dict[str, Any]] = []
        fp_block = _forecast_previous_inline(ev.forecast, ev.previous)
        if fp_block:
            right_contents.append(fp_block)
        effect = forecast_vs_previous_effect(ev)
        right_contents.append(_xau_direction_pill_from_effect(effect))
        right = {
            "type": "box", "layout": "vertical", "flex": 0,
            "spacing": "xs", "contents": right_contents,
        }
        body_contents.append({
            "type": "box", "layout": "horizontal", "spacing": "md",
            "alignItems": "center", "margin": "md",
            "contents": [left, right],
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
    effect: dict[str, str] | None = None,
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
        # 3-col data strip — all centered for equal visual spacing.
        body_contents.append({
            "type": "box", "layout": "horizontal", "margin": "lg",
            "contents": [
                {"type": "text", "text": "Actual",   "size": "xxs", "color": "#9CA3AF", "flex": 1, "align": "center"},
                {"type": "text", "text": "Forecast", "size": "xxs", "color": "#9CA3AF", "flex": 1, "align": "center"},
                {"type": "text", "text": "Previous", "size": "xxs", "color": "#9CA3AF", "flex": 1, "align": "center"},
            ],
        })
        body_contents.append({
            "type": "box", "layout": "horizontal",
            "contents": [
                {"type": "text", "text": actual_text, "size": "md", "weight": "bold",
                 "color": "#111827", "flex": 1, "align": "center"},
                {"type": "text", "text": event.forecast or "-", "size": "sm",
                 "color": "#374151", "flex": 1, "align": "center"},
                {"type": "text", "text": event.previous or "-", "size": "sm",
                 "color": "#374151", "flex": 1, "align": "center"},
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
        # 3-pill currency impact row (ECU + counter + XAU) using the
        # ACTUAL print as the comparison baseline (actual vs forecast =
        # surprise direction). All 3 pills derive from the same diff so
        # they're internally consistent — no need for a FRED-verdict
        # XAU override hack.
        from .calendar import event_impact_pills
        pills = event_impact_pills(event, actual_text=actual_text)
        body_contents.append({
            "type": "text", "text": "Currency Impact (POST)",
            "size": "xxs", "color": "#9CA3AF", "weight": "bold",
            "margin": "md",
        })
        body_contents.append(_impact_pills_row(pills))
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
        # No-FRED path — show F:/P: as an inline label and the 3-pill
        # currency impact row underneath. Matches pre-release layout.
        from .calendar import event_impact_pills
        body_contents.append({
            "type": "box", "layout": "horizontal", "margin": "lg",
            "contents": [
                {"type": "text", "text": "Actual ——", "size": "xs",
                 "color": "#9CA3AF", "flex": 1, "align": "start"},
                {"type": "text",
                 "text": f"F: {event.forecast or '-'} / P: {event.previous or '-'}",
                 "size": "xs", "color": "#6B7280", "flex": 1, "align": "end"},
            ],
        })
        body_contents.append({
            "type": "text", "text": "Currency Impact (POST)",
            "size": "xxs", "color": "#9CA3AF", "weight": "bold",
            "margin": "md",
        })
        body_contents.append(_impact_pills_row(event_impact_pills(event)))

    return {
        "type": "bubble", "size": "giga",
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
                    _xau_direction_pill_from_effect(eff),
                ],
            })

    # Per user 2026-05-23: bundle the date range into the title and
    # drop the right-aligned sub-label. "📅 Week Ahead (25/5/26 – 29/5/26)"
    # reads in one glance rather than splitting attention between
    # the left title and right date.
    title = f"📅 Week Ahead ({week_label})" if week_label else "📅 Week Ahead"
    return {
        "type": "bubble", "size": "giga",
        "header": _header(title, "", COLOR["digest"]),
        "body": {"type": "box", "layout": "vertical", "spacing": "sm",
                 "contents": sections, "paddingAll": "16px"},
    }


def pre_release_bubble(event: CalEvent, minutes_to_release: int,
                       impact: dict[str, str] | None = None,
                       effect: dict[str, str] | None = None) -> dict[str, Any]:
    """Pre-release bubble — title-led, Forecast/Previous row + 3-pill
    Currency Impact row.

    The 3-pill design (ECU + counter + XAU) was requested 2026-05-23 —
    actionable for currency traders because they see at a glance how
    BOTH the event's currency AND its counter are positioned, not just
    the XAU side.
    """
    from .calendar import event_impact_pills
    header_color, _ = _impact_color(event.impact)

    body_contents: list[dict[str, Any]] = [
        {"type": "text", "text": event.title, "weight": "bold", "size": "md",
         "wrap": True, "color": "#111827"},
        {"type": "box", "layout": "horizontal", "spacing": "sm",
         "alignItems": "center", "margin": "sm",
         "contents": [
             _impact_pill_calendar(event.impact),
             {"type": "text", "text": event.country, "size": "xs",
              "weight": "bold", "color": "#374151", "flex": 0},
        ]},
        # Forecast / Previous on the right-hand side, compact
        {"type": "box", "layout": "horizontal", "margin": "lg",
         "contents": [
             {"type": "text", "text": "Actual ——", "size": "xs",
              "color": "#9CA3AF", "flex": 1, "align": "start"},
             {"type": "text",
              "text": f"F: {event.forecast or '-'} / P: {event.previous or '-'}",
              "size": "xs", "color": "#6B7280", "flex": 1, "align": "end"},
        ]},
        # Section label + 3-pill row
        {"type": "text", "text": "Currency Impact (PRE)",
         "size": "xxs", "color": "#9CA3AF", "weight": "bold",
         "margin": "md"},
        _impact_pills_row(event_impact_pills(event)),
    ]
    # Header sub-label dropped — bubble timing in LINE already conveys
    # "this is happening soon"; "T-Xmin" was just noise.
    return {
        "type": "bubble", "size": "giga",
        "header": _header("⏰ Pre-Release", "", header_color),
        "body": {"type": "box", "layout": "vertical", "spacing": "sm",
                 "paddingAll": "16px", "contents": body_contents},
    }


# ---------- alt text ----------

def alt_text_for_event(label: str, ev: Event, score: float) -> str:
    return _trim(f"{label} {score:.1f} {ev.representative_title}", 380)
