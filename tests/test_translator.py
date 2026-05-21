"""CJK-leak rejection — Google Translate sometimes passes Chinese names
through from Reuters-style sources; the cleaner must catch that."""
from __future__ import annotations

from src.translator import _clean_translation, _has_cjk


def test_clean_keeps_pure_thai():
    out = "เพาเวลล์ส่งสัญญาณท่าทีอดทน เปิดประตูให้ลดดอกเบี้ย"
    assert _clean_translation(out) == out


def test_clean_strips_whitespace():
    assert _clean_translation("  เงินเฟ้อสูงขึ้น  \n") == "เงินเฟ้อสูงขึ้น"


def test_clean_rejects_empty_and_none():
    assert _clean_translation(None) is None
    assert _clean_translation("") is None
    assert _clean_translation("   ") is None


def test_clean_rejects_chinese_in_thai_output():
    """Google leak: '习近平 ของจีนให้คำมั่นว่าจะกระตุ้นเศรษฐกิจ'."""
    assert _clean_translation("习近平 ของจีน") is None


def test_clean_rejects_japanese_in_thai_output():
    """Japanese kana leak."""
    assert _clean_translation("ผู้ว่า BoJ ウエダ เตือนเรื่องเยน") is None


def test_clean_rejects_korean_in_thai_output():
    """Korean hangul leak."""
    assert _clean_translation("ประธาน 윤석열 ของเกาหลีใต้") is None


def test_clean_accepts_english_inline_terms():
    """We DO allow English finance terms inline (CPI, Fed, DXY etc.)."""
    text = "Fed กล่าวว่า CPI ยังสูง ดอกเบี้ยอาจอยู่นาน"
    assert _clean_translation(text) == text


def test_has_cjk_detection():
    assert _has_cjk("习近平") is True
    assert _has_cjk("ウエダ") is True
    assert _has_cjk("윤석열") is True
    assert _has_cjk("เงินเฟ้อ") is False
    assert _has_cjk("CPI hot") is False
    assert _has_cjk("") is False
    assert _has_cjk(None) is False
