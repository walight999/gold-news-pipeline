"""Gold Week Ahead email (growth phase 2, 2026-10-12).

Every weekly_preview run also sends next week's USD releases as a Resend
broadcast to the signup segment: day, ICT time, forecast vs previous and the
learned typical 15-min gold $ range per release. Facts only, no direction call
(same rule as x_release). Readers sign up on the finisit.app page, which adds
them to the Resend segment; Resend handles the unsubscribe link.

Env-gated: RESEND_API_KEY + RESEND_SEGMENT_ID + NEWSLETTER_FROM. Idempotent
per target week via sent_log `newsletter:<monday>`. Never raises.
"""
from __future__ import annotations

import html
import logging
import os
from typing import Any

from .utils_time import iso_utc, now_utc

log = logging.getLogger("newsletter")

API = "https://api.resend.com/broadcasts"
ROUTE = "newsletter"
DAY_TH = ["จันทร์", "อังคาร", "พุธ", "พฤหัสบดี", "ศุกร์", "เสาร์", "อาทิตย์"]
TELEGRAM_URL = "https://t.me/FinisitNews_bot?start=email"


def enabled() -> bool:
    return all(os.environ.get(k) for k in ("RESEND_API_KEY", "RESEND_SEGMENT_ID", "NEWSLETTER_FROM"))


def sent_key(target_mon: str) -> str:
    return f"newsletter:{target_mon}"


def already_sent(store, target_mon: str) -> bool:
    return bool(store.get("sent_log", (sent_key(target_mon), ROUTE)))


def _range(ev) -> str:
    try:
        from .release_stats import series_cells, typical_range_usd
        rng = typical_range_usd(series_cells(ev.country, ev.title, "XAU").get(15))
    except Exception:  # noqa: BLE001 — decoration only
        return ""
    return f"${rng[0]:,.0f}–${rng[1]:,.0f}" if rng else ""


def build(events: list, week_label: str, telegram_url: str = TELEGRAM_URL) -> dict[str, str] | None:
    """{subject, html, text} for the USD releases in `events`, or None if none."""
    from .x_release import _name
    usd = sorted((e for e in events if (e.country or "").upper() == "USD"), key=lambda e: e.dt_utc)
    if not usd:
        return None
    subject = f"Gold Week Ahead {week_label}: ตัวเลขสหรัฐฯ ที่ขยับทองได้"
    rows_html, lines, day = [], [], None
    for e in usd:
        d = e.dt_ict
        if d.date() != day:
            day = d.date()
            head = f"วัน{DAY_TH[d.weekday()]}ที่ {d.day}/{d.month}"
            rows_html.append(f'<tr><td colspan="4" style="padding:14px 0 4px;font-weight:bold">{head}</td></tr>')
            lines.append(f"\n{head}")
        hi = (e.impact or "").lower() == "high"
        name = html.escape(_name(e.title))
        fc = html.escape(e.forecast or "-")
        pv = html.escape(e.previous or "-")
        rng = _range(e) if hi else ""
        rows_html.append(
            f'<tr><td style="padding:2px 8px 2px 0">{d:%H:%M}</td>'
            f'<td style="padding:2px 8px 2px 0">{"<b>" + name + "</b>" if hi else name}</td>'
            f'<td style="padding:2px 8px 2px 0">คาด {fc} / ก่อนหน้า {pv}</td>'
            f'<td style="padding:2px 0;color:#6B7280">{"ทองมักขยับ " + rng + " ใน 15 นาที" if rng else ""}</td></tr>')
        lines.append(f"{d:%H:%M} {_name(e.title)} คาด {e.forecast or '-'} / ก่อนหน้า {e.previous or '-'}"
                     + (f" (ทองมักขยับ {rng} ใน 15 นาที)" if rng else ""))
    tg = html.escape(telegram_url)
    body = (
        '<div style="font-family:Arial,sans-serif;font-size:14px;color:#111;max-width:640px">'
        f"<p>ตัวเลขเศรษฐกิจสหรัฐฯ สัปดาห์หน้า ({html.escape(week_label)}) ที่มีผลต่อราคาทอง "
        "เวลาเป็นเวลาไทย ตัวหนาคือข่าวผลกระทบสูง</p>"
        f'<table style="border-collapse:collapse">{"".join(rows_html)}</table>'
        "<p>ช่วงที่ทองมักขยับคำนวณจากการตอบสนองของราคาทองต่อข่าวเดียวกันในอดีต "
        "ไม่ใช่การคาดการณ์ทิศทาง</p>"
        f'<p>อยากได้ตัวเลขจริงทันทีที่ออก ติดตามผ่าน Telegram ได้ฟรี: <a href="{tg}">{tg}</a></p>'
        '<p style="color:#6B7280;font-size:12px">ข้อมูลเพื่อการศึกษา ไม่ใช่คำแนะนำการลงทุน · '
        "Tradetongkam by Finisit · "
        '<a href="{{{RESEND_UNSUBSCRIBE_URL}}}">ยกเลิกการรับอีเมล</a></p></div>')
    text = (f"ตัวเลขเศรษฐกิจสหรัฐฯ สัปดาห์หน้า ({week_label}) เวลาไทย" + "\n".join(lines)
            + f"\n\nTelegram: {telegram_url}\nข้อมูลเพื่อการศึกษา ไม่ใช่คำแนะนำการลงทุน"
            + "\nยกเลิกการรับอีเมล: {{{RESEND_UNSUBSCRIBE_URL}}}")
    return {"subject": subject, "html": body, "text": text}


def send_week_ahead(store, events: list, target_mon: str, week_label: str,
                    *, http_post=None) -> bool:
    """Create + send the broadcast once per week. True when sent now."""
    if not enabled() or already_sent(store, target_mon):
        return False
    try:
        msg = build(events, week_label, os.environ.get("NEWSLETTER_TELEGRAM_URL") or TELEGRAM_URL)
        if not msg:
            log.info("newsletter: no USD events for %s — nothing to send", target_mon)
            return False
        if http_post is None:
            import httpx
            http_post = httpx.post
        r = http_post(API, headers={"Authorization": f"Bearer {os.environ['RESEND_API_KEY']}"},
                      json={"segment_id": os.environ["RESEND_SEGMENT_ID"],
                            "from": os.environ["NEWSLETTER_FROM"],
                            "subject": msg["subject"], "html": msg["html"], "text": msg["text"],
                            "name": f"Gold Week Ahead {target_mon}", "send": True},
                      timeout=30)
        if r.status_code >= 300:
            log.warning("newsletter: Resend HTTP %s: %s", r.status_code, r.text[:300])
            return False
        store.upsert("sent_log", {"event_id": sent_key(target_mon), "route_type": ROUTE,
                                  "sent_ts": iso_utc(now_utc()), "line_status": r.status_code})
        log.info("newsletter: Gold Week Ahead %s sent (%s)", target_mon, r.text[:120])
        return True
    except Exception:  # noqa: BLE001 — never block weekly_preview
        log.exception("newsletter: send failed")
        return False
