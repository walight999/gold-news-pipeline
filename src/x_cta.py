"""Growth CTA tweets for @tradetongkam (phase 2, 2026-10-12).

Twice a week (config days, after `hour_ict`) one tweet invites followers into
an owned channel: the free Telegram news bot, and — once a signup page URL is
configured — the Gold Week Ahead email. Alternates between the two by ISO week
so the feed doesn't repeat itself. The link costs $0.20/post on X (vs $0.015
without), so this stays at ~2 posts/week.

Piggybacks run_once (the 5-min news cron). Dedup via sent_log `cta:<ICT date>`.
Env-gated on the X creds, best-effort, never raises.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Any, Callable

from . import social_feed
from .utils_time import iso_utc, now_utc, to_ict

log = logging.getLogger("x_cta")

ROUTE = "x_cta"
DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "days": [0, 3],                 # Mon, Thu (ICT weekday, Mon=0)
    "hour_ict": 9,                  # earliest ICT hour to post
    "until_hour_ict": 21,
    "telegram_url": "https://t.me/FinisitNews_bot?start=x",
    "signup_url": "",               # Gold Week Ahead page; empty = Telegram only
}

TELEGRAM_TEXTS = [
    "ตัวเลขเศรษฐกิจสหรัฐฯ ออกปุ๊บ รู้ทันทีว่าออกเท่าไร เทียบคาดการณ์ และทองขยับไปกี่ดอลลาร์ "
    "รับฟรีผ่าน Telegram\n{url}",
    "ข่าวที่ขยับทองจริง คัดแล้ว ไม่ต้องไล่อ่านเอง ส่งตรงเข้า Telegram ฟรี ตั้งเวลาตามโซนที่อยู่ได้\n{url}",
]
EMAIL_TEXTS = [
    "ทุกเสาร์ส่งสรุปตัวเลขสหรัฐฯ สัปดาห์หน้าที่มีผลกับทอง พร้อมช่วงที่ทองมักแกว่งหลังข่าวแต่ละตัว "
    "รับฟรีทางอีเมล\n{url}",
    "วางแผนเทรดทองล่วงหน้าทั้งสัปดาห์ ข่าวไหนออกวันไหน เวลาไทย ปกติทองขยับเท่าไร สรุปให้ทุกเสาร์ทางอีเมล ฟรี\n{url}",
]
TAGS = "#ทองวันนี้ #ทองคำ"


def pick_text(cfg: dict[str, Any], now: datetime) -> str | None:
    """Alternate Telegram / email by ISO week (email only when signup_url set);
    the text variant rotates by week too."""
    ict = to_ict(now)
    week = ict.isocalendar()[1]
    idx = cfg["days"].index(ict.weekday()) if ict.weekday() in cfg["days"] else 0
    use_email = bool(cfg.get("signup_url")) and (week + idx) % 2 == 1
    if use_email:
        url, texts = cfg["signup_url"], EMAIL_TEXTS
    else:
        url, texts = cfg.get("telegram_url"), TELEGRAM_TEXTS
    if not url:
        return None
    return texts[(week // 2) % len(texts)].format(url=url) + "\n" + TAGS


def maybe_post(store, cfg: dict[str, Any] | None = None, *,
               poster: Callable[[str], str] = social_feed.x_post,
               now: datetime | None = None, require_creds: bool = True) -> bool:
    cfg = {**DEFAULTS, **(cfg or {})}
    now = now or now_utc()
    ict = to_ict(now)
    if not cfg.get("enabled") or ict.weekday() not in cfg["days"]:
        return False
    if not int(cfg["hour_ict"]) <= ict.hour < int(cfg["until_hour_ict"]):
        return False
    key = f"cta:{ict:%Y-%m-%d}"
    if store.get("sent_log", (key, ROUTE)):
        return False
    if require_creds and not all(os.environ.get(k) for k in (
            "X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_TOKEN_SECRET")):
        return False
    try:
        text = pick_text(cfg, now)
        if not text:
            return False
        url = poster(text)
    except Exception:  # noqa: BLE001 — never affects the news run
        log.exception("x_cta: post failed")
        return False
    store.upsert("sent_log", {"event_id": key, "route_type": ROUTE,
                              "sent_ts": iso_utc(now), "line_status": url or "posted"})
    log.info("x_cta: POSTED %s | %s", url, " ".join(text.split())[:120])
    return True
