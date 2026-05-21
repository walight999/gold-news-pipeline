"""Time helpers. Everything stored UTC; display ICT."""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone

ICT = timezone(timedelta(hours=7))
UTC = timezone.utc


def now_utc() -> datetime:
    return datetime.now(UTC)


def now_ict() -> datetime:
    return datetime.now(ICT)


def to_ict(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(ICT)


def to_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def iso_utc(dt: datetime | None) -> str:
    if dt is None:
        return ""
    return to_utc(dt).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        s2 = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s2)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)
    except (ValueError, TypeError):
        return None


def time_bucket(dt: datetime, minutes: int) -> str:
    """Return a deterministic bucket label for a 15m / Nm window in UTC.
    Used as part of event cluster_key."""
    dt = to_utc(dt)
    epoch = int(dt.timestamp())
    width = minutes * 60
    bucket_start = (epoch // width) * width
    bucket_dt = datetime.fromtimestamp(bucket_start, UTC)
    return bucket_dt.strftime("%Y%m%dT%H%M")


def is_active_session(dt: datetime | None = None) -> bool:
    """London open (14:00 ICT) → next-day NY close (04:00 ICT)."""
    dt_ict = to_ict(dt) if dt else now_ict()
    t = dt_ict.time()
    return t >= time(14, 0) or t < time(4, 0)


def is_weekend_ict(dt: datetime | None = None) -> bool:
    """True if `dt` (default: now) falls on Saturday or Sunday in ICT.
    Markets close at Sat 04:00 ICT and reopen Mon 04:00 ICT, but for
    simplicity we treat the entire ICT-day Sat + Sun as off-hours."""
    dt_ict = to_ict(dt) if dt else now_ict()
    return dt_ict.weekday() in (5, 6)  # Mon=0 ... Sat=5, Sun=6


def freshness_factor(anchor_utc: datetime, ref_utc: datetime | None = None) -> float:
    """0–3m=1.0 | 3–10m=0.6 | 10–30m=0.3 | >30m=0.1"""
    ref = ref_utc or now_utc()
    age_min = (to_utc(ref) - to_utc(anchor_utc)).total_seconds() / 60.0
    if age_min < 0:
        return 1.0
    if age_min <= 3:
        return 1.0
    if age_min <= 10:
        return 0.6
    if age_min <= 30:
        return 0.3
    return 0.1


def within_digest_slot(slots_ict: list[str], window_min: int, dt: datetime | None = None) -> str | None:
    """Return the slot label (e.g. '13:30') if now_ict is within ±window_min of any slot, else None."""
    dt_ict = to_ict(dt) if dt else now_ict()
    for slot in slots_ict:
        hh, mm = (int(x) for x in slot.split(":"))
        slot_dt = dt_ict.replace(hour=hh, minute=mm, second=0, microsecond=0)
        delta = abs((dt_ict - slot_dt).total_seconds()) / 60.0
        if delta <= window_min:
            return slot
    return None


def digest_sent_key(slot: str, dt: datetime | None = None) -> str:
    dt_ict = to_ict(dt) if dt else now_ict()
    return f"{dt_ict.strftime('%Y-%m-%d')}_{slot}"
