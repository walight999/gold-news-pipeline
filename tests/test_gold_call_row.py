"""Breaking/alert cards carry a structured gold call + XAU at send time."""
from __future__ import annotations

import json

from src.line_flex import _gold_call_row
from src.news_alert import MarketAlert, _alert_from_text


def _texts(row):
    return [c["text"] for c in row["contents"]]


def test_bias_confidence_and_price():
    a = MarketAlert(action="keep", headline_th="h", gold_bias="bullish", gold_confidence="high")
    row = _gold_call_row(a, (2650.4, 0.42))
    assert _texts(row) == ["🟢 หนุนทอง · มั่นใจสูง", "XAU 2,650.4 (+0.42% วันนี้)"]


def test_negative_change_and_no_confidence():
    a = MarketAlert(action="keep", headline_th="h", gold_bias="bearish")
    row = _gold_call_row(a, (2600.0, -1.5))
    assert _texts(row) == ["🔴 กดดันทอง", "XAU 2,600.0 (-1.50% วันนี้)"]


def test_nothing_to_show_returns_none():
    assert _gold_call_row(MarketAlert(action="keep", headline_th="h"), None) is None
    assert _gold_call_row(None, None) is None


def test_price_only_when_no_bias():
    row = _gold_call_row(MarketAlert(action="keep", headline_th="h"), (2650.0, 0.1))
    assert _texts(row) == ["XAU 2,650.0 (+0.10% วันนี้)"]


def test_parse_normalises_bias_fields():
    raw = json.dumps({"action": "keep", "headline_th": "หัวข่าว",
                      "gold_bias": " Bullish ", "gold_confidence": "extreme"},
                     ensure_ascii=False)
    a = _alert_from_text(raw)
    assert a.gold_bias == "bullish" and a.gold_confidence == ""


def test_cache_roundtrip_keeps_bias():
    a = MarketAlert(action="keep", headline_th="h", gold_bias="mixed", gold_confidence="low")
    b = MarketAlert.from_json(a.to_json())
    assert (b.gold_bias, b.gold_confidence) == ("mixed", "low")
