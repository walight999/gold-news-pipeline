"""Released-number tweets for @tradetongkam (2026-10-11).

When a high-impact USD release prints, calendar_check already has the actual,
the forecast/previous, the spot $ move since the print and the learned typical
15-min range. This posts those FACTS to X as one tweet per release minute
(CPI m/m + core + y/y printed together = one tweet), ~1.5-2.5 min after the
print (release_pinger cadence) — the speed + data edge no Thai gold account has.

Deterministic template, no LLM: numbers must be exact and the post goes out
unreviewed. Deliberately NO gold direction call: the calendar verdict's live
accuracy is not yet good enough to publish (5/12 graded, 2026-08-27..10-09).

Dedup + log in the squawk_log tab (`fs_id` = "rel:<release minute>"), so these
posts also count toward the news autopost day/session/hour quotas. Own daily
cap on top. Best-effort: never raises into calendar_check.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Any, Callable

from . import social_feed, tweet_writer
from .squawk_mirror import MIRROR_HEADERS, MIRROR_TAB, current_session, DEFAULT_SESSIONS
from .utils_time import iso_utc, now_utc, parse_iso, to_ict

log = logging.getLogger("x_release")

# Short Thai names for the releases that move gold; anything else keeps its
# ForexFactory title.
NAME_TH = {
    "non-farm employment change": "NFP (การจ้างงานนอกภาคเกษตร)",
    "unemployment rate": "อัตราว่างงาน",
    "unemployment claims": "ผู้ขอรับสวัสดิการว่างงานรายสัปดาห์",
    "average hourly earnings m/m": "ค่าจ้างรายชั่วโมง m/m",
    "cpi m/m": "CPI m/m",
    "cpi y/y": "CPI y/y",
    "core cpi m/m": "Core CPI m/m",
    "core cpi y/y": "Core CPI y/y",
    "ppi m/m": "PPI m/m",
    "core ppi m/m": "Core PPI m/m",
    "core pce price index m/m": "Core PCE m/m",
    "retail sales m/m": "ยอดค้าปลีก m/m",
    "core retail sales m/m": "ยอดค้าปลีกพื้นฐาน m/m",
    "adp non-farm employment change": "ADP การจ้างงานเอกชน",
    "ism manufacturing pmi": "ISM ภาคการผลิต",
    "ism services pmi": "ISM ภาคบริการ",
    "advance gdp q/q": "GDP เบื้องต้น q/q",
    "federal funds rate": "ดอกเบี้ยนโยบาย Fed",
    "jolts job openings": "JOLTS ตำแหน่งงานว่าง",
}

SURPRISE_TH = {"beat": "สูงกว่าคาด", "miss": "ต่ำกว่าคาด", "in-line": "ตามคาด"}

DEFAULTS = {"enabled": True, "countries": ["USD"], "impacts": ["High"],
            "cap_per_day": 6, "max_prints": 4}


def _name(title: str) -> str:
    return NAME_TH.get((title or "").strip().lower(), (title or "").strip())


def _typical_range_line(country: str, title: str) -> str | None:
    """'ปกติข่าวนี้ทองขยับ $a–$b ใน 15 นาที' from the learned history, or None."""
    try:
        from .release_stats import series_cells, typical_range_usd
        rng = typical_range_usd(series_cells(country, title, "XAU").get(15))
    except Exception:  # noqa: BLE001 — decoration only
        log.exception("x_release: typical range lookup failed")
        return None
    if not rng:
        return None
    return f"ปกติข่าวนี้ทองขยับ ${rng[0]:,.0f}–${rng[1]:,.0f} ใน 15 นาที"


def build_tweet(prints: list[dict[str, Any]], xau_move: dict | None,
                release_dt: datetime, tags: str | None = None,
                range_line: str | None = None) -> str | None:
    """One tweet for one release minute. `prints`: dicts with country, title,
    actual, forecast, previous, surprise. None when no print has an actual."""
    prints = [p for p in prints if str(p.get("actual") or "").strip()]
    if not prints:
        return None
    hhmm = to_ict(release_dt).strftime("%H:%M")
    lines: list[str] = []
    if len(prints) == 1:
        p = prints[0]
        lines.append(f"{_name(p['title'])} สหรัฐฯ ออกแล้ว {p['actual']}")
        ctx = []
        if str(p.get("forecast") or "").strip():
            ctx.append(f"คาด {p['forecast']}")
        if str(p.get("previous") or "").strip():
            ctx.append(f"ก่อนหน้า {p['previous']}")
        verdict = SURPRISE_TH.get(str(p.get("surprise") or ""))
        if ctx:
            lines.append(" · ".join(ctx) + (f" ({verdict})" if verdict else ""))
    else:
        lines.append(f"ตัวเลขสหรัฐฯ {hhmm} น. ออกแล้ว")
        for p in prints:
            fc = f" คาด {p['forecast']}" if str(p.get("forecast") or "").strip() else ""
            verdict = SURPRISE_TH.get(str(p.get("surprise") or ""))
            lines.append(f"{_name(p['title'])} {p['actual']}{fc}" + (f" {verdict}" if verdict else ""))
    if xau_move and xau_move.get("usd") is not None:
        usd = float(xau_move["usd"])
        mins = max(1, int(xau_move.get("minutes") or 0))
        lines.append(f"ทองหลังตัวเลขออก {mins} นาที: {'+' if usd >= 0 else '-'}${abs(usd):,.1f}")
    if range_line:
        lines.append(range_line)
    tags = tags or tweet_writer.pick_tags(" ".join(p["title"] for p in prints))
    body = "\n".join(lines)
    tweet = body + "\n" + tags
    while len(tweet) > tweet_writer.TWEET_LIMIT and len(lines) > 1:
        lines.pop()          # drop the least important trailing line, never mid-line
        tweet = "\n".join(lines) + "\n" + tags
    return tweet if len(tweet) <= tweet_writer.TWEET_LIMIT else None


def _x_ready() -> bool:
    return all(os.environ.get(k) for k in
               ("X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_TOKEN_SECRET"))


def post_release_minutes(store, items: list[dict[str, Any]], cfg: dict[str, Any] | None = None,
                         *, poster: Callable[[str], str] = social_feed.x_post,
                         now: datetime | None = None, require_creds: bool = True) -> int:
    """Post one tweet per release minute. `items`: calendar_check's released
    cards — dicts with `ev` (CalEvent), `actual`, `surprise`, `xau_move`.
    Returns tweets posted. Never raises."""
    cfg = {**DEFAULTS, **(cfg or {})}
    if not cfg.get("enabled") or not items:
        return 0
    if require_creds and not _x_ready():
        log.info("x_release: X creds not set — skipping")
        return 0
    countries = {c.upper() for c in cfg["countries"]}
    impacts = {i.lower() for i in cfg["impacts"]}
    groups: dict[datetime, list[dict[str, Any]]] = {}
    for it in items:
        ev = it["ev"]
        if (ev.country or "").upper() not in countries or (ev.impact or "").lower() not in impacts:
            continue
        if not str(it.get("actual") or "").strip():
            continue
        groups.setdefault(ev.dt_utc, []).append(it)
    if not groups:
        return 0

    now = now or now_utc()
    try:
        _, rows = store.read_feed(MIRROR_TAB)
    except Exception:  # noqa: BLE001
        log.exception("x_release: read squawk_log failed")
        return 0   # can't dedup ⇒ don't risk double-posting
    seen = {str(r.get("fs_id") or "").strip() for r in rows}
    _, _, day_start = current_session(now, DEFAULT_SESSIONS)
    today = sum(1 for r in rows if str(r.get("fs_id") or "").startswith("rel:")
                and str(r.get("posted") or "").strip()
                and (parse_iso(str(r.get("ts_utc") or "")) or day_start) >= day_start)

    posted = 0
    for dt, group in sorted(groups.items()):
        fid = f"rel:{dt.strftime('%Y-%m-%dT%H:%MZ')}"
        if fid in seen:
            continue
        if today + posted >= int(cfg["cap_per_day"]):
            log.info("x_release: daily cap %s reached", cfg["cap_per_day"])
            break
        prints = [{"country": g["ev"].country, "title": g["ev"].title,
                   "actual": g.get("actual"), "forecast": g["ev"].forecast,
                   "previous": g["ev"].previous, "surprise": g.get("surprise")}
                  for g in group][: int(cfg["max_prints"])]
        move = next((g.get("xau_move") for g in group if g.get("xau_move")), None)
        rng = next((r for r in (_typical_range_line(p["country"], p["title"]) for p in prints) if r), None)
        tweet = build_tweet(prints, move, dt, range_line=rng)
        if not tweet:
            continue
        try:
            url = poster(tweet)
        except Exception:  # noqa: BLE001 — one failed post must not stop calendar_check
            log.exception("x_release: X post failed %s", fid)
            continue
        posted += 1
        seen.add(fid)
        log.info("x_release: POSTED %s | %s", url, " ".join(tweet.split())[:150])
        src = "; ".join(f"{p['title']} {p['actual']} (f {p['forecast']})" for p in prints)
        row = [iso_utc(now), to_ict(now).strftime("%Y-%m-%d %H:%M:%S"),
               fid, src[:280], tweet, url or "posted"]
        try:
            store.append_feed(MIRROR_TAB, MIRROR_HEADERS, [row])
        except Exception:  # noqa: BLE001
            log.exception("x_release: log append failed (tweet WAS posted: %s)", url)
    return posted
