"""Notification previews lead with Thai content, not an English title + score."""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from src.calendar import CalEvent
from src.line_flex import alt_text_for_digest, alt_text_for_event, alt_text_for_release


def _ev(title="Fed's Waller says cuts are on the table"):
    return SimpleNamespace(representative_title=title)


def test_event_alt_prefers_thai_headline():
    t = alt_text_for_event("⚡ BREAKING", _ev(), 4.7, "วอลเลอร์ชี้เฟดอาจลดดอกเบี้ย")
    assert t == "⚡ BREAKING วอลเลอร์ชี้เฟดอาจลดดอกเบี้ย"


def test_event_alt_falls_back_to_english_and_score():
    assert alt_text_for_event("🔔 ALERT", _ev("X"), 3.2, None) == "🔔 ALERT 3.2 X"
    assert alt_text_for_event("🔔 ALERT", _ev("X"), 3.2, "   ") == "🔔 ALERT 3.2 X"


def test_digest_alt_top_headline_and_count():
    t = alt_text_for_digest("12:30", ["ทองพุ่งหลัง CPI", "ดอลลาร์อ่อน", ""])
    assert t == "📰 12:30 · ทองพุ่งหลัง CPI (+1 ข่าว)"


def test_digest_alt_single_and_degraded():
    assert alt_text_for_digest("08:30", ["ข่าวเดียว"]) == "📰 08:30 · ข่าวเดียว"
    assert alt_text_for_digest("08:30", [], degraded=True) == "📰 News Update 08:30 [โหมดสำรอง]"


def test_release_alt_includes_print_and_verdict():
    ev = CalEvent(event_id="x", title="Core CPI m/m", country="USD", impact="High",
                  forecast="0.3%", previous="0.2%",
                  dt_utc=datetime(2026, 9, 11, 12, 30, tzinfo=timezone.utc))
    t = alt_text_for_release(ev, "0.5%", "🔴 Bearish gold")
    assert t == "📊 USD Core CPI m/m 0.5% vs คาด 0.3% · 🔴 Bearish gold"
    t2 = alt_text_for_release(ev, None, "⚪ Neutral — print matched forecast")
    assert t2 == "📊 USD Core CPI m/m · ⚪ Neutral"
