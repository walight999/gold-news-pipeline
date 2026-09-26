"""Thai Gold Traders Association (สมาคมค้าทองคำ) 96.5% bar price.

The number Thai gold traders quote first. Source is the association site's own
JSON endpoint (the Next.js front-end fetches it client-side):

    GET https://www.goldtraders.or.th/api/GoldPrices/Latest?readjson=false
    → {"asTime": "2026-09-26T09:05:00", "bL_BuyPrice": 67650.0,
       "bL_SellPrice": 67850.0, "priceChangeFromPrevDayLast": -50.0, ...}

`asTime` is Bangkok local time (no offset). Best-effort: any failure returns
None and the card simply omits the row.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

import httpx

from .utils_time import ICT

log = logging.getLogger(__name__)

GTA_LATEST_URL = "https://www.goldtraders.or.th/api/GoldPrices/Latest?readjson=false"


@dataclass(frozen=True)
class ThaiBarPrice:
    buy: float            # association buys from the public (รับซื้อ)
    sell: float           # association sells to the public (ขายออก)
    change_day: float     # baht vs previous day's last announcement
    as_of_ict: datetime | None


def parse_latest(d: dict) -> ThaiBarPrice | None:
    try:
        buy = float(d["bL_BuyPrice"])
        sell = float(d["bL_SellPrice"])
    except (KeyError, TypeError, ValueError):
        return None
    if buy <= 0 or sell <= 0:
        return None
    try:
        change = float(d.get("priceChangeFromPrevDayLast") or 0.0)
    except (TypeError, ValueError):
        change = 0.0
    as_of = None
    raw = str(d.get("asTime") or "").strip()
    if raw:
        try:
            as_of = datetime.fromisoformat(raw)
            if as_of.tzinfo is None:
                as_of = as_of.replace(tzinfo=ICT)
        except ValueError:
            as_of = None
    return ThaiBarPrice(buy=buy, sell=sell, change_day=change, as_of_ict=as_of)


def fetch_latest(timeout: float = 10.0) -> ThaiBarPrice | None:
    try:
        with httpx.Client(timeout=timeout,
                          headers={"User-Agent": "Mozilla/5.0 gold-news-pipeline/1.0"}) as c:
            r = c.get(GTA_LATEST_URL, follow_redirects=True)
        r.raise_for_status()
        return parse_latest(r.json())
    except Exception as e:  # noqa: BLE001 — optional decoration, never blocks a card
        log.warning("thai bar price fetch failed: %s", e)
        return None
