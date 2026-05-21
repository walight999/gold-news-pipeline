"""CJK-leak rejection — Google Translate sometimes passes Chinese names
through from Reuters-style sources; the cleaner must catch that."""
from __future__ import annotations

from src.translator import _clean_translation, _has_cjk, _patch_names


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
    assert _has_cjk("ウエดะ") is True
    assert _has_cjk("윤석열") is True
    assert _has_cjk("เงินเฟ้อ") is False
    assert _has_cjk("CPI hot") is False
    assert _has_cjk("") is False
    assert _has_cjk(None) is False


def test_patch_names_basic():
    """Post-process glossary catches Google Translate leaks (Claude already
    produces these correctly via the in-prompt glossary)."""
    assert "ทรัมป์" in _patch_names("Trump กล่าวว่าจะขึ้นภาษี")
    assert "ทรัมป์" in _patch_names("Donald J. Trump เซ็นกฎหมาย")
    assert "ปูติน" in _patch_names("Putin ส่งสัญญาณหยุดยิง")
    assert "สี จิ้นผิง" in _patch_names("Xi Jinping เยือนรัสเซีย")
    assert "พาวเวลล์" in _patch_names("Powell ส่งสัญญาณท่าทีอดทน")
    assert "ลาการ์ด" in _patch_names("Lagarde แถลงเรื่อง CPI")
    assert "เซเลนสกี" in _patch_names("Zelensky urges aid")
    assert "เนทันยาฮู" in _patch_names("Netanyahu ปฏิเสธข้อเสนอ")


def test_patch_names_word_boundary():
    """Word boundary — don't mangle compound words like Trumpets."""
    # 'Trumpets' must NOT become 'ทรัมป์ets' — \b requires the boundary
    # after the name. Our regex uses \bTrump\b so 'Trumpets' is safe.
    assert "Trumpets" in _patch_names("Trumpets sound")


def test_patch_names_idempotent():
    """Running on already-correct Thai output is a no-op."""
    pure = "ทรัมป์เซ็นภาษีนำเข้าใหม่"
    assert _patch_names(pure) == pure


def test_clean_applies_name_patch():
    """The full cleaner runs name patching too."""
    assert "ทรัมป์" in _clean_translation("Trump กล่าวว่า CPI สูง")
