"""LINE Flex Message builders.

Design:
- BREAKING / ALERT = single bubble (one event = one push).
- DIGEST           = carousel; one bubble PER TOPIC, listing top events in that topic.
- HEALTH           = single bubble (compact warning list).

Color tokens (white text on solid header):
  BREAKING #DC2626   ALERT #D97706   DIGEST #2563EB   HEALTH #6B7280
"""
from __future__ import annotations

from typing import Any

from .dedup import Event

COLOR = {
    "breaking": "#DC2626",
    "alert":    "#D97706",
    "digest":   "#2563EB",
    "health":   "#6B7280",
}

CARRIER_MAX_BUBBLES = 5    # we self-cap carousel < LINE's 12 hard limit for compact scroll
TOPIC_TITLE_LIMIT   = 90
SUMMARY_LIMIT       = 180


def _trim(s: str, n: int) -> str:
    s = (s or "").strip()
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def _map_name(text: str, name_map: dict[str, str]) -> str:
    out = text
    for en, th in (name_map or {}).items():
        idx = out.lower().find(en.lower())
        if idx >= 0:
            out = out[: idx + len(en)] + f" ({th})" + out[idx + len(en):]
            break
    return out


def _header(title: str, score_label: str, color: str) -> dict[str, Any]:
    return {
        "type": "box", "layout": "horizontal",
        "backgroundColor": color,
        "paddingAll": "12px",
        "contents": [
            {"type": "text", "text": title, "color": "#FFFFFF", "weight": "bold", "size": "md", "flex": 1},
            {"type": "text", "text": score_label, "color": "#FFFFFF", "size": "sm", "align": "end"},
        ],
    }


def _chip(text: str, bg: str = "#E5E7EB", fg: str = "#111827") -> dict[str, Any]:
    return {
        "type": "box", "layout": "vertical",
        "backgroundColor": bg, "cornerRadius": "12px",
        "paddingStart": "8px", "paddingEnd": "8px",
        "paddingTop": "2px", "paddingBottom": "2px",
        "contents": [{"type": "text", "text": text, "size": "xs", "color": fg}],
    }


def _chip_row(items: list[tuple[str, str, str]]) -> dict[str, Any]:
    return {
        "type": "box", "layout": "horizontal", "spacing": "sm",
        "contents": [_chip(t, bg, fg) for t, bg, fg in items],
    }


def _topic_chip_color(topic: str) -> tuple[str, str]:
    palette = {
        "inflation":   ("#FEE2E2", "#991B1B"),
        "jobs":        ("#FEF3C7", "#92400E"),
        "rate_policy": ("#DBEAFE", "#1E40AF"),
        "geopolitics": ("#FCE7F3", "#9D174D"),
        "usd_yields":  ("#E0E7FF", "#3730A3"),
        "gold_flow":   ("#FEF9C3", "#854D0E"),
    }
    return palette.get(topic, ("#E5E7EB", "#111827"))


def _direction_chip_color(direction: str) -> tuple[str, str]:
    return {
        "hawkish":  ("#FECACA", "#7F1D1D"),
        "dovish":   ("#BBF7D0", "#14532D"),
        "risk_off": ("#FED7AA", "#7C2D12"),
        "risk_on":  ("#BAE6FD", "#0C4A6E"),
        "neutral":  ("#E5E7EB", "#374151"),
    }.get(direction, ("#E5E7EB", "#374151"))


def _event_bubble(label: str, color: str, ev: Event, score: float, kw_cfg: dict[str, Any]) -> dict[str, Any]:
    nm = kw_cfg.get("name_map", {}) or {}
    title = _trim(_map_name(ev.representative_title, nm), 140)
    summary = _trim(ev.representative_summary, SUMMARY_LIMIT)
    sources = ", ".join(ev.source_list)
    topic_bg, topic_fg = _topic_chip_color(ev.topic_bucket)
    dir_bg, dir_fg = _direction_chip_color(ev.direction_label)

    body_contents: list[dict[str, Any]] = [
        {"type": "text", "text": title, "weight": "bold", "size": "md", "wrap": True, "color": "#111827"},
    ]
    if summary:
        body_contents.append({"type": "text", "text": summary, "size": "sm", "wrap": True, "color": "#374151", "margin": "md"})
    body_contents.append(_chip_row([
        (ev.topic_bucket, topic_bg, topic_fg),
        (ev.entity,        "#E5E7EB", "#111827"),
        (ev.direction_label, dir_bg, dir_fg),
    ]))
    body_contents.append({
        "type": "text", "text": f"📡 {sources}",
        "size": "xs", "color": "#6B7280", "margin": "sm", "wrap": True,
    })

    bubble: dict[str, Any] = {
        "type": "bubble",
        "size": "kilo",
        "header": _header(label, f"score {score:.1f}", color),
        "body": {"type": "box", "layout": "vertical", "spacing": "md", "contents": body_contents, "paddingAll": "14px"},
    }
    article_url = next((i.url for i in ev.items if i.url), "")
    if article_url:
        bubble["footer"] = {
            "type": "box", "layout": "vertical", "paddingAll": "10px",
            "contents": [{
                "type": "button", "style": "secondary", "height": "sm",
                "action": {"type": "uri", "label": "Read article", "uri": article_url[:1000]},
            }],
        }
    return bubble


def breaking_bubble(ev: Event, score: float, kw_cfg: dict[str, Any]) -> dict[str, Any]:
    return _event_bubble("⚡ BREAKING", COLOR["breaking"], ev, score, kw_cfg)


def alert_bubble(ev: Event, score: float, kw_cfg: dict[str, Any]) -> dict[str, Any]:
    return _event_bubble("🔔 ALERT", COLOR["alert"], ev, score, kw_cfg)


def digest_carousel(
    events: list[Event],
    scores: dict[str, float],
    slot: str,
    kw_cfg: dict[str, Any],
) -> dict[str, Any] | None:
    """Group events by topic_bucket, one bubble per topic, ranked within topic."""
    if not events:
        return None
    nm = kw_cfg.get("name_map", {}) or {}
    groups: dict[str, list[Event]] = {}
    for ev in events:
        groups.setdefault(ev.topic_bucket, []).append(ev)
    sorted_topics = sorted(
        groups.keys(),
        key=lambda t: -max(scores.get(e.event_id, 0) for e in groups[t]),
    )[:CARRIER_MAX_BUBBLES]

    bubbles: list[dict[str, Any]] = []
    total_events = 0
    for topic in sorted_topics:
        evs = sorted(groups[topic], key=lambda e: -scores.get(e.event_id, 0))[:5]
        total_events += len(evs)
        topic_bg, topic_fg = _topic_chip_color(topic)
        body: list[dict[str, Any]] = [
            {"type": "box", "layout": "horizontal", "contents": [
                _chip(topic, topic_bg, topic_fg),
                {"type": "text", "text": f"{len(evs)} event(s)", "size": "xs", "color": "#6B7280", "align": "end", "gravity": "center"},
            ]},
            {"type": "separator", "margin": "md"},
        ]
        for ev in evs:
            score = scores.get(ev.event_id, 0)
            title = _trim(_map_name(ev.representative_title, nm), TOPIC_TITLE_LIMIT)
            src = ", ".join(ev.source_list[:3])
            body.append({
                "type": "box", "layout": "vertical", "margin": "md", "spacing": "xs",
                "contents": [
                    {"type": "box", "layout": "horizontal", "contents": [
                        {"type": "text", "text": f"{score:.1f}", "size": "sm", "weight": "bold", "color": "#111827", "flex": 0},
                        {"type": "text", "text": title, "size": "sm", "wrap": True, "color": "#111827", "margin": "sm", "flex": 1},
                    ]},
                    {"type": "text", "text": f"📡 {src}", "size": "xxs", "color": "#6B7280"},
                ],
            })
        bubbles.append({
            "type": "bubble", "size": "kilo",
            "header": _header(f"📰 Digest {slot} ICT", f"{len(evs)} in {topic}", COLOR["digest"]),
            "body": {"type": "box", "layout": "vertical", "contents": body, "paddingAll": "14px"},
        })
    return {"type": "carousel", "contents": bubbles}


def health_bubble(warnings: list[tuple[str, str]]) -> dict[str, Any]:
    lines = [{
        "type": "text",
        "text": f"• {sid}: {wtype}",
        "size": "sm", "color": "#374151", "wrap": True, "margin": "xs",
    } for sid, wtype in warnings[:15]]
    return {
        "type": "bubble", "size": "kilo",
        "header": _header("⚠️ HEALTH", f"{len(warnings)} warning(s)", COLOR["health"]),
        "body": {"type": "box", "layout": "vertical", "spacing": "xs", "paddingAll": "14px", "contents": lines},
    }


def alt_text_for_event(label: str, ev: Event, score: float) -> str:
    return _trim(f"{label} {score:.1f} {ev.representative_title}", 380)
