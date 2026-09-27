"""สมาคมค้าทองคำ bar price: parse the association JSON + render the card row."""
from __future__ import annotations

from src.line_flex import _thai_bar_row
from src.thai_gold import parse_latest

# Captured live 2026-09-27 from /api/GoldPrices/Latest?readjson=false (trimmed).
SAMPLE = {"goldPriceID": 87704, "asTime": "2026-09-26T09:05:00", "bL_BuyPrice": 67650.00,
          "bL_SellPrice": 67850.00, "goldSpot": 4286.00, "bahtPerUSD": 33.42,
          "priceChangeFromPrevDayLast": -50.00}


def test_parse_live_sample():
    b = parse_latest(SAMPLE)
    assert (b.buy, b.sell, b.change_day) == (67650.0, 67850.0, -50.0)
    assert b.as_of_ict.strftime("%d/%m %H:%M") == "26/09 09:05"
    assert b.as_of_ict.utcoffset().total_seconds() == 7 * 3600


def test_parse_rejects_missing_or_zero_prices():
    assert parse_latest({}) is None
    assert parse_latest({"bL_BuyPrice": 0, "bL_SellPrice": 0}) is None
    assert parse_latest({"bL_BuyPrice": "x", "bL_SellPrice": 1}) is None


def test_parse_tolerates_missing_change_and_time():
    b = parse_latest({"bL_BuyPrice": 1, "bL_SellPrice": 2})
    assert b.change_day == 0.0 and b.as_of_ict is None


def test_row_render():
    texts = [c["text"] for c in _thai_bar_row(parse_latest(SAMPLE))["contents"]]
    assert texts == ["ทองแท่งสมาคม", "ขาย 67,850 · ซื้อ 67,650", "▼50", "26/09 09:05"]
    assert _thai_bar_row(None) is None
